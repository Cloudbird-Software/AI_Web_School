// Command papergen 是组卷编排的 CLI 出口（审计卡 #148 交付 5）：读蓝图
// JSON 文件 → 连题源 DB（DSN 从环境变量读，fail-closed）→ core/assembly
// 编排全链（编译→装载→曝光过滤→求解→渲染，与 HTTP POST /papers 同一契约）
// → 落 <paper_id>.html + <paper_id>.json → stdout 摘要。
//
// 用法：
//
//	SCHOOL_DATABASE_URL=postgres://... go run ./cmd/papergen \
//		-blueprint bp.json -out out/papergen/
//
// fail-closed 纪律：DSN 未配置（env 空）直接失败——无题源连接绝不产卷，
// 不降级空池、不产出伪制品；编排失败（蓝图非法/池不可行/渲染失败）原样
// 上抛 exit 1。DSN 经环境变量注入（连接串零明文入仓，治理 AR-3）。
//
// 产物与账面：paper_id 与 HTTP 面同一内容寻址（同蓝图同池同种子同 id，
// 可回放可审计）。制品今日只落本地文件——paper/paper_item 落库须与事务性
// 曝光预留同事务提交，挂后续卡；出题草稿入账走 cmd/ingest 既有发布链。
// 卷头 QR 位图在 #152 前恒缺位，编排以哨兵原文如实写入制品 QRSlot.Err 与
// 卷面注释（fail-loud 不伪造），本 CLI 照抄 stderr，不吞不瞒。
package main

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"os"
	"path/filepath"

	"github.com/Cloudbird-Software/AI_Web_School/core/assembly"
	"github.com/Cloudbird-Software/AI_Web_School/core/assembly/paperdb"
	"github.com/jackc/pgx/v5/pgxpool"
)

func main() {
	if err := run(os.Args[1:], os.Stdout, os.Stderr); err != nil {
		_, _ = fmt.Fprintln(os.Stderr, "papergen 失败:", err)
		os.Exit(1)
	}
}

// run 是 CLI 本体（stdout/stderr 注入便于测试；编排路径的 DB 依赖除外，
// 逻辑面尽量纯）。
func run(args []string, stdout, stderr *os.File) error {
	fs := flag.NewFlagSet("papergen", flag.ContinueOnError)
	blueprintPath := fs.String("blueprint", "", "组卷蓝图 JSON 文件路径（必填；契约面 = POST /papers body）")
	outDir := fs.String("out", "out/papergen/", "产物输出目录（<paper_id>.html / <paper_id>.json）")
	envVar := fs.String("env", "SCHOOL_DATABASE_URL", "承载 DSN 的环境变量名（DSN 零明文入仓）")
	if err := fs.Parse(args); err != nil {
		return err
	}
	if *blueprintPath == "" {
		return errors.New("-blueprint 必填（组卷蓝图 JSON 文件）")
	}
	bp, err := loadBlueprint(*blueprintPath)
	if err != nil {
		return err
	}

	// fail-closed：DSN 未配置即失败（编排层对 nil 题源同样拒绝——两级防线
	// 语义一致：无题源绝不伪造卷面）.
	dsn := os.Getenv(*envVar)
	if dsn == "" {
		return fmt.Errorf("环境变量 %s 未配置 DSN（fail-closed：无题源连接不产卷）", *envVar)
	}
	pool, err := pgxpool.New(context.Background(), dsn)
	if err != nil {
		return fmt.Errorf("连接题源失败: %w", err)
	}
	defer pool.Close()

	orch := &assembly.Orchestrator{Source: paperdb.NewItemSource(pool)}
	art, err := orch.Orchestrate(context.Background(), bp, assembly.OrchestrateOptions{})
	if err != nil {
		// 编排错误原样上抛（assembly.ErrInvalidBlueprint / InfeasibleError /
		// 渲染失败——fail-loud，不降级不脱敏给操作者）.
		return err
	}

	if err := os.MkdirAll(*outDir, 0o755); err != nil {
		return fmt.Errorf("创建输出目录 %s 失败: %w", *outDir, err)
	}
	htmlPath := filepath.Join(*outDir, art.Metadata.PaperID+".html")
	jsonPath := filepath.Join(*outDir, art.Metadata.PaperID+".json")
	if err := writeFile(htmlPath, art.HTML); err != nil {
		return err
	}
	artifactJSON, err := json.MarshalIndent(art, "", "  ")
	if err != nil {
		return fmt.Errorf("制品序列化失败: %w", err)
	}
	if err := writeFile(jsonPath, artifactJSON); err != nil {
		return err
	}

	writeSummary(stdout, *art, htmlPath, jsonPath)
	if art.QR.Err != "" {
		// QR 缺位（#152 前现状）照抄 stderr：哨兵原文如实透传，不吞不瞒.
		_, _ = fmt.Fprintf(stderr, "⚠ 卷头 QR 位图缺位（#152 前现状，制品内已如实留痕）: %s\n", art.QR.Err)
	}
	return nil
}

// writeSummary 打印 stdout 摘要（人读面：paper_id / 定位字段 / 摘要 / 产物
// 路径 / 账面如实声明）.
func writeSummary(stdout *os.File, art assembly.PaperArtifact, htmlPath, jsonPath string) {
	m := art.Metadata
	_, _ = fmt.Fprintln(stdout, "════════ papergen · 组卷编排（审计 #148） ════════")
	_, _ = fmt.Fprintf(stdout, "paper_id     : %s\n", m.PaperID)
	_, _ = fmt.Fprintf(stdout, "items        : %d  purpose=%s gradeband=%s pack=%s seed=%d\n",
		m.ItemCount, m.Purpose, m.Gradeband, m.PackID, m.Seed)
	_, _ = fmt.Fprintf(stdout, "blueprint    : %s\n", m.BlueprintDigest)
	_, _ = fmt.Fprintf(stdout, "selection    : %s\n", m.SelectionDigest)
	_, _ = fmt.Fprintf(stdout, "generated_at : %s  snapshot_ref=%s\n", m.GeneratedAt, m.SnapshotRef)
	_, _ = fmt.Fprintf(stdout, "item_ids     : %v\n", m.ItemVersionIDs)
	_, _ = fmt.Fprintf(stdout, "HTML → %s\n", htmlPath)
	_, _ = fmt.Fprintf(stdout, "JSON → %s\n", jsonPath)
	_, _ = fmt.Fprintln(stdout, "（制品未落库：paper/paper_item 与曝光预留同事务写入挂后续卡；出题草稿入账走 cmd/ingest）")
}

// writeFile 落一个产物文件（Write/Close 全链显式弃错——GO-2 门禁）.
func writeFile(path string, data []byte) error {
	f, err := os.Create(path)
	if err != nil {
		return fmt.Errorf("创建 %s 失败: %w", path, err)
	}
	if _, werr := f.Write(data); werr != nil {
		_ = f.Close()
		return fmt.Errorf("写入 %s 失败: %w", path, werr)
	}
	if cerr := f.Close(); cerr != nil {
		return fmt.Errorf("关闭 %s 失败: %w", path, cerr)
	}
	return nil
}
