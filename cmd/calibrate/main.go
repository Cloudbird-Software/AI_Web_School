// Command calibrate 是参数标定 CLI（C 流标定入口；卡 #183）.
//
// 支持 --method irt-2pl：读取作答 JSONL → core/datastat.Calibrate2PL →
// 输出题目 IRT 2PL 参数（区分度 a / 难度 b / Elo 评级 rating）JSON。
//
// 输入 JSONL（每行一条作答记录，三字段即可）：
//
//	{"student_id": "sim_001", "item_id": "item_001", "correct": 1}
//
// correct 取 0/1（客观题）或 [0,1] 之间部分分。
//
// 输出 JSON（逐题一行）：
//
//	{"item_version_id":"item_001","discrimination":1.2,"difficulty":0.3,
//	 "rating":1680.2,"sample_size":40}
//
// DB 取数/落库面（response_event → item_param）在 doc.go 显式留白：本命令以
// 文件 IO 为主路径（与 E 引擎 / pipeline-ipt 的 JSONL 契约对齐），便于离线
// 标定与合成数据回放；DB 接线在服务化波次按现有 sqlc 查询面增量接入。
//
// 分账纪律（宪法 D5）：source 由调用方按「真实/合成」传入
// （--source measured_irt | measured_sim_irt），本命令不复检 scene 字段
// （纯函数核消费已过滤的记录列表）.
package main

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"math"
	"os"
	"time"

	"github.com/Cloudbird-Software/AI_Web_School/core/datastat"
	"github.com/jackc/pgx/v5/pgxpool"
)

// reqRecord 是输入 JSONL 单条作答记录（与 pipeline-irt 规范格式对齐）.
type reqRecord struct {
	StudentID string  `json:"student_id"`
	ItemID    string  `json:"item_id"`
	Correct   float64 `json:"correct"`
}

// outRecord 是输出 JSONL 单题 IRT 2PL 参数.
type outRecord struct {
	ItemVersionID  string  `json:"item_version_id"`
	Discrimination float64 `json:"discrimination"`
	Difficulty     float64 `json:"difficulty"`
	Rating         float64 `json:"rating"`
	SampleSize     int     `json:"sample_size"`
}

func main() {
	var (
		method = flag.String("method", "irt-2pl", "标定方法（irt-2pl）")
		input  = flag.String("input", "", "输入作答 JSONL 路径（缺省 stdin）")
		output = flag.String("output", "", "输出参数 JSONL 路径（缺省 stdout）")
		source = flag.String("source", datastat.IRTSource, "item_param.source 标签（measured_irt|measured_sim_irt）")
		scope  = flag.String("scope", datastat.ScopeMeasurement, "purpose_scope（practice|diagnosis|measurement）")
		dsn    = flag.String("dsn", "", "PG 连接串（提供时落库 item_param；缺省仅文件输出）")
		dryRun = flag.Bool("dry-run", false, "落库前仅打印 SQL 与参数，不写入")
	)
	flag.Parse()

	if *method != "irt-2pl" {
		fatalf("不支持的 --method %q（本卡仅接 irt-2pl）\n", *method)
	}
	if *source != datastat.IRTSource && *source != datastat.IRTSourceSim {
		fatalf("非法 --source %q（合法值 %q|%q）\n", *source, datastat.IRTSource, datastat.IRTSourceSim)
	}
	if !datastat.ValidPurposeScope(*scope) {
		fatalf("非法 --scope %q（合法值 practice|diagnosis|measurement）\n", *scope)
	}

	records, err := readRecords(*input)
	if err != nil {
		fatalf("读取输入失败: %v\n", err)
	}
	if len(records) == 0 {
		fatalf("无作答记录可标定\n")
	}

	params := datastat.Calibrate2PL(records)
	if err := writeParams(*output, params); err != nil {
		fatalf("写入输出失败: %v\n", err)
	}

	if *dsn != "" {
		if err := writeItemParams(*dsn, params, *source, *scope, *dryRun); err != nil {
			fatalf("落库 item_param 失败: %v\n", err)
		}
	}

	fmt.Fprintf(os.Stderr, "✅ calibrate irt-2pl：%d 条作答 → %d 题参数（source=%s, scope=%s）\n",
		len(records), len(params), *source, *scope)
}

// writeItemParams 把标定参数写入 item_param 表（append-only，UNIQUE 身份=
// (item_version_id, purpose_scope, source, method_version, as_of)，重复入账
// 撞唯一约束即报错——调用方须保证 as_of 或方法版本区分）.
//
// 注意：本命令不做 response_event → ResponseRecord 的取数（该取数面在
// doc.go 显式留白，且「正确」判定藏于 scoring_trace JSONB，属评分器面），
// 输入走 JSONL；落库面在此直接以参数化 INSERT 落账，与 item_param 的
// append-only 触发器 trg_item_param_append_only 兼容.
func writeItemParams(dsn string, params []datastat.ItemIrtStats, source, scope string, dryRun bool) error {
	ctx := context.Background()

	// 预构造全部入账行（与执行解耦，便于 dry-run 与测试）.
	type row struct {
		paramID string
		item    string
		params  string
		n       int
	}
	asOf := time.Now().UTC()
	rows := make([]row, 0, len(params))
	for _, p := range params {
		paramsJSON, err := json.Marshal(map[string]float64{
			"difficulty":     round4(p.Difficulty),
			"discrimination": round4(p.Discrimination),
			"rating":         round1(p.Rating),
		})
		if err != nil {
			return fmt.Errorf("序列化 params: %w", err)
		}
		rows = append(rows, row{paramID: "ip_" + newULIDLocal(), item: p.ItemVersionID, params: string(paramsJSON), n: p.SampleSize})
	}

	if dryRun {
		for _, r := range rows {
			fmt.Fprintf(os.Stderr, "[dry-run] INSERT item_param item=%s source=%s scope=%s method=%s as_of=%s params=%s n=%d\n",
				r.item, source, scope, datastat.IRTMethodVersion, asOf.Format(time.RFC3339), r.params, r.n)
		}
		return nil
	}

	pool, err := pgxpool.New(ctx, dsn)
	if err != nil {
		return fmt.Errorf("连接 PG: %w", err)
	}
	defer pool.Close()
	for _, r := range rows {
		_, err = pool.Exec(ctx, `
			INSERT INTO item_param (
				param_id, item_version_id, purpose_scope, source,
				params, sample_size, method_version, as_of
			) VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7, $8)
		`, r.paramID, r.item, scope, source, r.params, r.n, datastat.IRTMethodVersion, asOf)
		if err != nil {
			return fmt.Errorf("INSERT item_param item=%s: %w", r.item, err)
		}
	}
	return nil
}

// newULIDLocal 生成简化的唯一 id 后缀（时间毫秒前缀 + 6 字节随机 hex，
// 与账行 id 的「语义前缀 + 唯一后缀」惯例同形；仅用于 id 唯一性）.
func newULIDLocal() string {
	rnd := make([]byte, 6)
	_, _ = rand.Read(rnd)
	return fmt.Sprintf("%x%s", time.Now().UnixMilli(), hex.EncodeToString(rnd))
}

// readRecords 从路径（或 stdin）读作答 JSONL.
func readRecords(path string) ([]datastat.ResponseRecord, error) {
	var r io.Reader = os.Stdin
	if path != "" {
		f, err := os.Open(path)
		if err != nil {
			return nil, err
		}
		defer func() { _ = f.Close() }() // GO-2 显式弃错：只读句柄，关闭失败不丢数据
		r = f
	}
	dec := json.NewDecoder(r)
	var out []datastat.ResponseRecord
	for {
		var rec reqRecord
		err := dec.Decode(&rec)
		if err == io.EOF {
			break
		}
		if err != nil {
			return nil, fmt.Errorf("JSON 解析: %w", err)
		}
		out = append(out, datastat.ResponseRecord{
			StudentAliasID: rec.StudentID,
			ItemVersionID:  rec.ItemID,
			Correct:        rec.Correct,
		})
	}
	return out, nil
}

// writeParams 把标定结果写成 JSONL（路径或 stdout）.
func writeParams(path string, params []datastat.ItemIrtStats) error {
	var w io.Writer = os.Stdout
	if path != "" {
		f, err := os.Create(path)
		if err != nil {
			return err
		}
		defer func() { _ = f.Close() }() // GO-2 显式弃错：写失败已由 enc.Encode 逐条暴露
		w = f
	}
	enc := json.NewEncoder(w)
	for _, p := range params {
		rec := outRecord{
			ItemVersionID:  p.ItemVersionID,
			Discrimination: round4(p.Discrimination),
			Difficulty:     round4(p.Difficulty),
			Rating:         round1(p.Rating),
			SampleSize:     p.SampleSize,
		}
		if err := enc.Encode(rec); err != nil {
			return err
		}
	}
	return nil
}

// round4 / round1 把浮点收整到 4/1 位小数（输出整洁，不落库）.
func round4(v float64) float64 { return math.Round(v*1e4) / 1e4 }
func round1(v float64) float64 { return math.Round(v*10) / 10 }

func fatalf(format string, a ...any) {
	fmt.Fprintf(os.Stderr, "❌ "+format, a...)
	os.Exit(1)
}
