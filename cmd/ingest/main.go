// Command ingest 是内容入账链路（审计卡 #156）的入口：读取 mathgen /
// langgen / enggen 生成器产出的 JSONL（packs/subjectmath.Record 形态：嵌入
// *Instance + space_index + content_digest），逐条过摘要对表与校验门，门过者
// 在一个显式事务内入账（item / item_version / gate_certificate / gate_run →
// core/content PublishService 发布事务），门不过者独立事务留痕后继续。
//
// 用法：
//
//	go run ./cmd/ingest -dsn "postgresql://user:pass@localhost:5432/db" \
//	    -in out/mathgen/ -pack-digest sha256:<学科包摘要>
//
// 学科包（P0-2 起）：按模板 id 前缀自动分派（tpl-sm-→subject-math /
// tpl-sl-→subject-lang / tpl-se-→subject-english）——摘要对表 ① 用各包
// 既有 InstanceDigest 口径、② 用各包模板注册表；-pack-id 显式传入时覆盖。
//
// pack_digest 的口径说明（审计卡 ground truth 结论）：公式一的 pd 输入在
// 仓库内没有学科包摘要的生成点（core/instantiation golden 用 fixture 值；
// mathgen/batch 均不产出 pack 摘要），故此值必须显式传入——绝不凭空造公式；
// engine_digest 有仓库唯一真源（core/instantiation.EngineDigest =
// sha256("1.0.0")），留 flag 仅作覆盖口，缺省即真源。
package main

import (
	"bufio"
	"context"
	"flag"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"sort"
	"strings"

	"github.com/Cloudbird-Software/AI_Web_School/core/instantiation"
	"github.com/jackc/pgx/v5/pgxpool"
)

// 注册表契约文件名（specs/contracts/registries/，contract-watch 冻结面）。
const (
	interactionYAML = "interaction.yaml"
	scorerYAML      = "scorer.yaml"
)

func main() {
	if err := run(); err != nil {
		fmt.Fprintln(os.Stderr, "ingest 失败:", err)
		os.Exit(1)
	}
}

func run() error {
	opts := options{}
	flag.StringVar(&opts.input, "in", "out/mathgen/", "JSONL 文件或目录（目录取其中 *.jsonl）")
	flag.StringVar(&opts.packID, "pack-id", "", "item.pack_id 覆盖（缺省按模板 id 前缀自动分派：tpl-sm-→subject-math / tpl-sl-→subject-lang / tpl-se-→subject-english）")
	flag.StringVar(&opts.packDigest, "pack-digest", "", "公式一 pack_digest（sha256:...，必填——仓库无学科包摘要真源，禁止缺参退化）")
	flag.StringVar(&opts.engineDigest, "engine-digest", instantiation.EngineDigest, "公式一 engine_digest（缺省 core/instantiation.EngineDigest）")
	flag.StringVar(&opts.policyVersion, "policy-version", "1.0", "门策略版本（gate_certificate/gate_run/gate_failure 判定语境）")
	flag.StringVar(&opts.issuedBy, "issued-by", "cmd/ingest", "门证书签发人 / 发布人 id")
	flag.StringVar(&opts.operator, "operator", "cmd/ingest", "lineage.operator（执行入账的操作者）")
	flag.StringVar(&opts.registriesDir, "registries", filepath.Join("specs", "contracts", "registries"), "注册表契约目录")
	dsn := flag.String("dsn", "", "PostgreSQL DSN（postgresql://user:pass@host:port/db），必填")
	flag.Parse()

	if *dsn == "" {
		return fmt.Errorf("-dsn 必填（postgresql://...）")
	}
	if opts.packDigest == "" {
		return fmt.Errorf("-pack-digest 必填（公式一 pd 无仓库真源，缺参即拒绝——绝不凭空造）")
	}
	if !strings.HasPrefix(opts.packDigest, "sha256:") || !strings.HasPrefix(opts.engineDigest, "sha256:") {
		return fmt.Errorf("-pack-digest/-engine-digest 须为 sha256: 前缀的摘要形态")
	}

	// 注册表加载 fail-fast：注册表面残缺时整批实例都没有可证的注册语境。
	interactions, err := loadContractIDs(filepath.Join(opts.registriesDir, interactionYAML), "types")
	if err != nil {
		return err
	}
	scorers, err := loadContractIDs(filepath.Join(opts.registriesDir, scorerYAML), "scorers")
	if err != nil {
		return err
	}

	ctx := context.Background()
	pool, err := pgxpool.New(ctx, *dsn)
	if err != nil {
		return fmt.Errorf("连接数据库失败: %w", err)
	}
	defer pool.Close()

	files, err := jsonlFiles(opts.input)
	if err != nil {
		return err
	}

	runner := NewRunner(opts, pool, interactions, scorers)
	fmt.Println("════════ ingest · 内容入账链路（#156） ════════")
	fmt.Printf("in=%s  pack=%s  pack_digest=%s\n", opts.input, opts.packID, opts.packDigest)
	fmt.Printf("engine_digest=%s  policy=%s  registry: %d interactions / %d scorers\n\n",
		opts.engineDigest, opts.policyVersion, len(interactions), len(scorers))

	sum := runner.runBatch(ctx, os.Stdout, files)

	fmt.Println("\n──────── 入账汇总 ────────")
	fmt.Printf("accepted=%d  rejected=%d  decode-error=%d\n",
		sum.accepted, sum.rejected, sum.decodeErrors)
	fmt.Printf("原因分布: %s\n", sum.reasonLines())
	if sum.accepted == 0 && sum.rejected+sum.decodeErrors > 0 {
		return fmt.Errorf("全部记录未入账（见原因分布）")
	}
	return nil
}

// jsonlFiles 解析输入路径：文件直取；目录收集 *.jsonl 并按名排序（多文件
// 入账顺序确定，重放才可对账）。
func jsonlFiles(input string) ([]string, error) {
	info, err := os.Stat(input)
	if err != nil {
		return nil, fmt.Errorf("读取输入路径失败: %w", err)
	}
	if !info.IsDir() {
		return []string{input}, nil
	}
	entries, err := os.ReadDir(input)
	if err != nil {
		return nil, fmt.Errorf("读取目录 %s 失败: %w", input, err)
	}
	var files []string
	for _, e := range entries {
		if e.IsDir() || !strings.HasSuffix(e.Name(), ".jsonl") {
			continue
		}
		files = append(files, filepath.Join(input, e.Name()))
	}
	if len(files) == 0 {
		return nil, fmt.Errorf("目录 %s 下没有 .jsonl 文件", input)
	}
	sort.Strings(files)
	return files, nil
}

// batchSummary 一批入账的计数与原因分布（accepted/rejected/原因分布——
// 审计卡点名的末尾汇总口径）。
type batchSummary struct {
	accepted     int
	rejected     int
	decodeErrors int
	reasons      map[string]int
}

func newBatchSummary() *batchSummary {
	return &batchSummary{reasons: map[string]int{}}
}

func (s *batchSummary) count(outcome, reason string) {
	switch outcome {
	case outcomeAccepted:
		s.accepted++
	case outcomeRejected:
		s.rejected++
		s.reasons[reason]++
	default:
		s.decodeErrors++
		s.reasons[reason]++
	}
}

// reasonLines 输出原因分布（按计数降序、键升序——汇总面确定性）。
func (s *batchSummary) reasonLines() string {
	if len(s.reasons) == 0 {
		return "-"
	}
	type kv struct {
		k string
		v int
	}
	pairs := make([]kv, 0, len(s.reasons))
	for k, v := range s.reasons {
		pairs = append(pairs, kv{k, v})
	}
	sort.Slice(pairs, func(i, j int) bool {
		if pairs[i].v != pairs[j].v {
			return pairs[i].v > pairs[j].v
		}
		return pairs[i].k < pairs[j].k
	})
	parts := make([]string, 0, len(pairs))
	for _, p := range pairs {
		parts = append(parts, fmt.Sprintf("%s=%d", p.k, p.v))
	}
	return strings.Join(parts, ", ")
}

// runBatch 逐文件逐行入账；记录级失败继续下一条，硬错误（驱动/留痕账故障）
// 立即中止并带回计数（stdout 汇总保留已处理部分的事实）。
func (rn *Runner) runBatch(ctx context.Context, out io.Writer, files []string) *batchSummary {
	sum := newBatchSummary()
	for _, file := range files {
		n, err := rn.runFile(ctx, out, file, sum)
		if err != nil {
			_, _ = fmt.Fprintf(out, "✘ %s 在第 %d 行后中止: %v\n", file, n, err) // stderr 汇总面：写失败无降级通道（GO-2 显式弃错）
			return sum
		}
	}
	return sum
}

// runFile 处理单个 JSONL 文件，返回已成功读到的行号（1 起）。
func (rn *Runner) runFile(ctx context.Context, out io.Writer, file string, sum *batchSummary) (int, error) {
	f, err := os.Open(file)
	if err != nil {
		return 0, fmt.Errorf("打开 %s 失败: %w", file, err)
	}
	defer func() { _ = f.Close() }() // GO-2 显式弃错

	sc := bufio.NewScanner(f)
	sc.Buffer(make([]byte, 0, 64*1024), 16*1024*1024) // 单行含六块内容，放大缓冲
	lineNo := 0
	for sc.Scan() {
		lineNo++
		line := sc.Bytes()
		if len(strings.TrimSpace(string(line))) == 0 {
			continue
		}
		outcome, reason, err := rn.ingestRecord(ctx, line)
		if err != nil {
			return lineNo, fmt.Errorf("第 %d 行: %w", lineNo, err)
		}
		sum.count(outcome, reason)
		mark, detail := "✔ accepted", ""
		switch outcome {
		case outcomeAccepted:
			detail = reason
		case outcomeRejected:
			mark = "✘ rejected"
			detail = reason
		default:
			mark = "✘ decode-error"
			detail = reason
		}
		if detail != "" {
			detail = "（" + detail + "）"
		}
		_, _ = fmt.Fprintf(out, "  %s %s:%d%s\n", mark, filepath.Base(file), lineNo, detail) // GO-2 显式弃错
	}
	if err := sc.Err(); err != nil {
		return lineNo, fmt.Errorf("扫描 %s 失败: %w", file, err)
	}
	return lineNo, nil
}
