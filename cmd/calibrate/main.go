// Command calibrate 是参数标定的 CLI 出口（P0-5，2026-08-31 补齐）：数据
// 飞轮「参数标定」环的人工/作业触发面——读单场景作答事件 → core/datastat
// CTT 统计核 → 实测参数行落 item_param（append-only，D1/D6）。
//
// 用法：
//
//	SCHOOL_DATABASE_URL=postgres://... go run ./cmd/calibrate \
//		-scene practice -min-sample 30
//
// fail-closed 纪律：DSN 未配置（env 空）直接失败；scene 越域（D5 三值域
// practice/diagnosis/measurement）直接失败——结构上不存在跨场景混估路径。
// 幂等：同快照（item_version_id × scope × source × method_version × as_of）
// 重跑时 uq_item_param_identity 冲突行计为 already（不异常穿透、不写重复
// 行）；新数据（更大 as_of）或新 method_version 才产生新行。
//
// 样本门槛：n < min-sample 的题不产参数行（样本不足不伪造参数）。默认 1
// 与冻结实现一致；生产建议 ≥30（CTT 区分度统计意义门槛）。
//
// 产物：stdout JSON 摘要（events/items/written/skipped_small/already/as_of），
// 供 AI 作业链解析；行明细逐条 stderr 人读。
package main

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"os"
	"time"

	"github.com/Cloudbird-Software/AI_Web_School/core/datastat"
	dbgen "github.com/Cloudbird-Software/AI_Web_School/db/gen"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgtype"
	"github.com/jackc/pgx/v5/pgxpool"
)

func main() {
	if err := run(os.Args[1:], os.Stdout, os.Stderr); err != nil {
		_, _ = fmt.Fprintln(os.Stderr, "calibrate 失败:", err)
		os.Exit(1)
	}
}

// calibrateSummary 是 stdout 摘要（作业链解析面）.
type calibrateSummary struct {
	Scene        string `json:"scene"`
	Events       int    `json:"events"`
	Items        int    `json:"items"`
	Written      int    `json:"written"`
	SkippedSmall int    `json:"skipped_small"`
	Already      int    `json:"already"`
	AsOf         string `json:"as_of"`
	DryRun       bool   `json:"dry_run"`
}

func run(args []string, stdout, stderr *os.File) error {
	fs := flag.NewFlagSet("calibrate", flag.ContinueOnError)
	scene := fs.String("scene", "", "标定场景（必填；practice/diagnosis/measurement，D5 禁止跨场景混估）")
	minSample := fs.Int("min-sample", 1, "最小样本量；n<该值的题不产参数行（生产建议 ≥30）")
	dryRun := fs.Bool("dry-run", false, "只统计不落库（预览标定面）")
	envVar := fs.String("env", "SCHOOL_DATABASE_URL", "承载 DSN 的环境变量名（DSN 零明文入仓）")
	if err := fs.Parse(args); err != nil {
		return err
	}
	if !datastat.ValidPurposeScope(*scene) {
		return fmt.Errorf("-scene 越域: %q（合法域 practice/diagnosis/measurement；D5）", *scene)
	}
	if *minSample < 1 {
		return fmt.Errorf("-min-sample 必须 ≥1，得到 %d", *minSample)
	}

	dsn := os.Getenv(*envVar)
	if dsn == "" {
		return fmt.Errorf("环境变量 %s 未配置 DSN（fail-closed：无账本不标定）", *envVar)
	}
	ctx := context.Background()
	pool, err := pgxpool.New(ctx, dsn)
	if err != nil {
		return fmt.Errorf("连接账本失败: %w", err)
	}
	defer pool.Close()
	qs := dbgen.New(pool)

	// 1) 单场景事件取数（SQL 面 WHERE scene= 精确过滤——混估防线第一层）
	rows, err := qs.ListCttResponseRecords(ctx, dbgen.ResponseEventSceneEnum(*scene))
	if err != nil {
		return fmt.Errorf("取作答事件失败: %w", err)
	}
	sum := calibrateSummary{Scene: *scene, Events: len(rows), DryRun: *dryRun}
	if len(rows) == 0 {
		sum.AsOf = time.Time{}.UTC().Format(time.RFC3339)
		return json.NewEncoder(stdout).Encode(sum)
	}
	records := make([]datastat.ResponseRecord, len(rows))
	var asOf time.Time
	for i, r := range rows {
		records[i] = datastat.ResponseRecord{
			ItemVersionID:  r.ItemVersionID,
			StudentAliasID: r.StudentAliasID,
			Correct:        r.Correct,
		}
		if r.CreatedAt.Time.After(asOf) {
			asOf = r.CreatedAt.Time
		}
	}
	sum.AsOf = asOf.UTC().Format(time.RFC3339)

	// 2) CTT 统计核（纯函数面，数值正确性由 datastat 单测钉死）
	stats := datastat.ComputeCtt(records)
	sum.Items = len(stats)

	// 3) 落账（append-only；同快照重跑 = already）
	for _, s := range stats {
		if s.SampleSize < *minSample {
			sum.SkippedSmall++
			_, _ = fmt.Fprintf(stderr, "skip-small  %s n=%d\n", s.ItemVersionID, s.SampleSize) // GO-2 显式弃错
			continue
		}
		params, err := cttParams(s)
		if err != nil {
			return err
		}
		if *dryRun {
			_, _ = fmt.Fprintf(stderr, "dry-run     %s n=%d difficulty=%.4f\n", s.ItemVersionID, s.SampleSize, s.Difficulty) // GO-2 显式弃错
			continue
		}
		paramID, err := newParamID()
		if err != nil {
			return err
		}
		if err := qs.InsertItemParam(ctx, dbgen.InsertItemParamParams{
			ParamID:       paramID,
			ItemVersionID: s.ItemVersionID,
			PurposeScope:  *scene,
			Source:        datastat.CTTSource,
			Params:        params,
			SampleSize:    int32(s.SampleSize),
			MethodVersion: datastat.CTTMethodVersion,
			AsOf:          pgtype.Timestamptz{Time: asOf.UTC(), Valid: true},
		}); err != nil {
			var pgErr *pgconn.PgError
			if errors.As(err, &pgErr) && pgErr.Code == "23505" {
				// 同快照幂等命中：uq_item_param_identity 拒绝——预期行为
				sum.Already++
				_, _ = fmt.Fprintf(stderr, "already     %s as_of=%s\n", s.ItemVersionID, sum.AsOf) // GO-2 显式弃错
				continue
			}
			return fmt.Errorf("落参数行 %s: %w", s.ItemVersionID, err)
		}
		sum.Written++
		_, _ = fmt.Fprintf(stderr, "written     %s n=%d difficulty=%.4f\n", s.ItemVersionID, s.SampleSize, s.Difficulty) // GO-2 显式弃错
	}
	return json.NewEncoder(stdout).Encode(sum)
}

// cttParams 构造 params JSONB（difficulty 必有；discrimination nil 时为
// JSON null——不伪造 0，与冻结实现同形）.
func cttParams(s datastat.ItemCttStats) ([]byte, error) {
	params := map[string]any{"difficulty": s.Difficulty}
	if s.Discrimination != nil {
		params["discrimination"] = *s.Discrimination
	} else {
		params["discrimination"] = nil
	}
	b, err := json.Marshal(params)
	if err != nil {
		return nil, fmt.Errorf("params 序列化失败: %w", err)
	}
	return b, nil
}

// newParamID 生成 param_<随机 hex> 行标识（Python 侧 param_+ULID 同形不同源；
// crypto/rand 熵源不可用时报错而非发可重复 ID）.
func newParamID() (string, error) {
	var b [8]byte
	if _, err := rand.Read(b[:]); err != nil {
		return "", fmt.Errorf("熵源不可用无法生成 param_id: %w", err)
	}
	return "param_" + hex.EncodeToString(b[:]), nil
}
