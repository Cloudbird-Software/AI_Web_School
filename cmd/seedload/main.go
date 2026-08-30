// Command seedload 是知识图谱种子装载入口（审计卡 #149/#159）：把
// content/seeds/*.yaml 幂等装载进 PG（kp_node / kp_edge / relation_type）。
// 装载语义全部复用 core/knowledge.Load（SeedSink 端口），写面为
// core/knowledge.PGSink（sqlc 语句面，INSERT ON CONFLICT DO NOTHING）。
//
// 用法：
//
//	export SCHOOL_DATABASE_URL="postgresql://user:pass@localhost:5432/school"
//	go run ./cmd/seedload                          # 装载 content/seeds/ 全部 *.yaml
//	go run ./cmd/seedload -file content/seeds/math_kp_3-4.yaml -file content/seeds/math_kp_1-2.yaml
//	go run ./cmd/seedload -dry-run                 # 真实查重、不写库（演练计数）
//
// 幂等约定：重复装载同一文件 = 全部 skip（按 (pack_id,dimension,code) 查重
// 节点、(src,dst,rel_type) 查重边、rel_type 查重关系类型），统计面如实反映
// added/skipped——幂等重跑即 skip 统计，绝不产生重复行。
//
// fail-closed：DSN 只从环境变量 SCHOOL_DATABASE_URL 读取，缺失即报错退出，
// 禁止默认连接串/降级放行；种子文件内边引用了未定义且库中不存在的节点
// code 时，装载完成但进程以非零退出（stats.EdgesMissingNode 检测，铁律
// 「如实报错」——已导入部分保留，修种子后重跑即可补齐）。
package main

import (
	"context"
	"flag"
	"fmt"
	"io"
	"os"

	"github.com/Cloudbird-Software/AI_Web_School/core/knowledge"
	"github.com/jackc/pgx/v5/pgxpool"
)

// dsnEnv 种子装载的数据库目标（治理 AR-3 同款口径：凭证只走运行时环境，
// 绝不进仓库/命令行参数）.
const dsnEnv = "SCHOOL_DATABASE_URL"

// seedDimension 知识点种子的固定维度（任务卡验收 #3：所有节点 dimension=kp）.
const seedDimension = "kp"

func main() {
	if err := run(os.Args[1:], os.Getenv, os.Stdout); err != nil {
		fmt.Fprintln(os.Stderr, "seedload 失败:", err)
		os.Exit(1)
	}
}

func run(args []string, getenv func(string) string, out io.Writer) error {
	fs := flag.NewFlagSet("seedload", flag.ContinueOnError)
	var files multiFlag
	fs.Var(&files, "file", "种子 YAML 文件（可重复；与 -dir 叠加，路径去重）")
	dir := fs.String("dir", "content/seeds", "种子目录（扫描其中 *.yaml/*.yml，按文件名排序）")
	dryRun := fs.Bool("dry-run", false, "演练：照常查重但抑制全部写入（added 即「将导入」计数）")
	if err := fs.Parse(args); err != nil {
		return err
	}

	// fail-closed：没有显式数据库目标就不装（禁止默认连接串）。
	dsn := getenv(dsnEnv)
	if dsn == "" {
		return fmt.Errorf("环境变量 %s 未设置（fail-closed：种子装载必须有显式数据库目标，禁止默认连接）", dsnEnv)
	}

	seeds, err := collectSeeds(files, *dir)
	if err != nil {
		return err
	}

	ctx := context.Background()
	pool, err := pgxpool.New(ctx, dsn)
	if err != nil {
		return fmt.Errorf("连接数据库失败: %w", err)
	}
	defer pool.Close()

	var sink knowledge.SeedSink = knowledge.NewPGSink(ctx, pool)
	mode := "apply"
	if *dryRun {
		sink = newDryRunSink(sink)
		mode = "dry-run"
	}

	_, _ = fmt.Fprintf(out, "════════ seedload · 知识图谱种子装载（#149/#159） ════════\n")
	_, _ = fmt.Fprintf(out, "mode=%s  dimension=%s  files=%d\n\n", mode, seedDimension, len(seeds))

	total := &seedTotals{}
	for _, path := range seeds {
		stats, err := knowledge.Load(path, sink, seedDimension)
		if err != nil {
			return fmt.Errorf("装载 %s: %w", path, err)
		}
		if dr, ok := sink.(*dryRunSink); ok {
			// 演练面自证：抑制写入数 = added 统计（漏记即 fail loud）
			if err := dr.verifyAndReset(stats); err != nil {
				return fmt.Errorf("装载 %s: %w", path, err)
			}
		}
		printFileStats(out, path, stats)
		total.merge(stats)
	}
	total.print(out, mode)

	if total.missing > 0 {
		return fmt.Errorf("%d 条边引用了未定义且库中不存在的节点 code（见上方 missing-node；已导入部分保留，修复种子后重跑补齐）", total.missing)
	}
	return nil
}

// printFileStats 单文件装载统计（added/skipped 即幂等重跑证据面）.
func printFileStats(out io.Writer, path string, s *knowledge.SeedLoadStats) {
	_, _ = fmt.Fprintf(out, "── %s（%s @ %s）\n", path, s.PackID, s.GraphReleaseID)
	_, _ = fmt.Fprintf(out, "   relation_types: added=%d skipped=%d\n", s.RelationTypesAdded, s.RelationTypesSkipped)
	_, _ = fmt.Fprintf(out, "   nodes:          added=%d skipped=%d\n", s.NodesAdded, s.NodesSkipped)
	_, _ = fmt.Fprintf(out, "   edges:          added=%d skipped=%d  missing-node=%d\n", s.EdgesAdded, s.EdgesSkipped, s.EdgesMissingNode)
}

// seedTotals 全批汇总（跨文件累计）.
type seedTotals struct {
	files       int
	relAdded    int
	relSkipped  int
	nodeAdded   int
	nodeSkip    int
	edgeAdded   int
	edgeSkipped int
	missing     int
}

func (t *seedTotals) merge(s *knowledge.SeedLoadStats) {
	t.files++
	t.relAdded += s.RelationTypesAdded
	t.relSkipped += s.RelationTypesSkipped
	t.nodeAdded += s.NodesAdded
	t.nodeSkip += s.NodesSkipped
	t.edgeAdded += s.EdgesAdded
	t.edgeSkipped += s.EdgesSkipped
	t.missing += s.EdgesMissingNode
}

// print 末尾汇总；dry-run 下 added 语义改为「将导入」.
func (t *seedTotals) print(out io.Writer, mode string) {
	label := "added"
	if mode == "dry-run" {
		label = "would-add"
	}
	_, _ = fmt.Fprintf(out, "\n──────── 汇总（%d 个种子文件）────────\n", t.files)
	_, _ = fmt.Fprintf(out, "relation_types: %s=%d skipped=%d\n", label, t.relAdded, t.relSkipped)
	_, _ = fmt.Fprintf(out, "nodes:          %s=%d skipped=%d\n", label, t.nodeAdded, t.nodeSkip)
	_, _ = fmt.Fprintf(out, "edges:          %s=%d skipped=%d  missing-node=%d\n", label, t.edgeAdded, t.edgeSkipped, t.missing)
}
