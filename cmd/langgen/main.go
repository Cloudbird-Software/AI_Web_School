// Command langgen 是语文轮实例批量生成入口（mathgen 同构）。
//
// 确定性档：langgen -n 20 -out out/langgen/
// 每母题生成 n 个实例 → 全部过独立校验器 → 结构互异断言（唯一率 100%）
// → 写 JSONL（每行一实例，content 摘要含于字段）+ 汇总报告到 stdout。
//
// 半确定档（GO-RW-012/审计 #157 接线）：langgen -reorg 5
// 经 Bus→Target→bamlai 适配器调 GenerateSentenceReorg 出 LLM draft，代码
// 可解性校验全过才成实例；批次台账为进程内 Memory（CLI 形态），生产服务
// 面台账走 pgx Ledger。OPENAI_API_KEY 未注入时 BAML 出站失败即报错退出
// （fail-closed，不产伪行）。
package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"time"

	"github.com/Cloudbird-Software/AI_Web_School/core/ai"
	"github.com/Cloudbird-Software/AI_Web_School/internal/bamlai"
	"github.com/Cloudbird-Software/AI_Web_School/packs/subjectlang"
)

func main() {
	n := flag.Int("n", 20, "每母题实例数")
	out := flag.String("out", "out/langgen/", "JSONL 输出目录")
	reorg := flag.Int("reorg", 0, "句子重组（半确定档 LLM draft）生成条数；0=跳过")
	target := flag.String("target", "lang_sentence_reorg", "总线出站目标名")
	gradeband := flag.String("gradeband", "L", "学段（draft 入参）")
	provider := flag.String("provider", "openai", "D10 台账 Provider")
	model := flag.String("model", "gpt-4o-mini", "D10 台账 Model（应与 baml client 一致）")
	modelVersion := flag.String("model-version", "2024-07-18", "D10 台账 ModelVersion")
	flag.Parse()

	corpus, err := subjectlang.LoadCorpus(
		filepath.Join("content", "sources", "corpus", "manifest.yaml"),
		"demo-common-chars-v1")
	if err != nil {
		fatal(err)
	}

	if *reorg > 0 {
		runReorg(corpus, *reorg, *out, *target, *gradeband, *provider, *model, *modelVersion)
		return
	}

	gens, err := subjectlang.BuiltinGenerators(corpus)
	if err != nil {
		fatal(err)
	}
	if err := os.MkdirAll(*out, 0o755); err != nil {
		fatal(err)
	}

	total, distinct := 0, 0
	seenAll := map[string]string{} // digest → 母题 id（跨母题也应互异）
	for _, g := range gens {
		var sb strings.Builder
		seen := map[string]bool{}
		for i := 0; i < *n; i++ {
			inst, err := g.Instance(i)
			if err != nil {
				fatal(fmt.Errorf("%s Instance(%d): %w", g.Entry().ID, i, err))
			}
			digest, derr := subjectlang.InstanceDigest(inst)
			if derr != nil {
				fatal(derr)
			}
			if seen[digest] {
				fatal(fmt.Errorf("%s Instance(%d) 摘要重复——唯一率破坏（H-W6-1 口径）", g.Entry().ID, i))
			}
			if prev, dup := seenAll[digest]; dup {
				fatal(fmt.Errorf("跨母题摘要重复 %s: %s vs %s", digest[:16], prev, g.Entry().ID))
			}
			seen[digest] = true
			seenAll[digest] = g.Entry().ID
			distinct++
			b, _ := json.Marshal(inst)
			sb.Write(b)
			sb.WriteByte('\n')
			total++
		}
		name := strings.TrimPrefix(g.Entry().ID, "tpl-sl-") + ".jsonl"
		if err := os.WriteFile(filepath.Join(*out, name), []byte(sb.String()), 0o644); err != nil {
			fatal(err)
		}
		fmt.Printf("✅ %s: %d 实例，唯一率 100%%（校验器独立重判全过）\n", g.Entry().ID, *n)
	}
	fmt.Printf("✅ 语文轮批量：%d/%d 合格、唯一率 100%%（%d 摘要全局唯一）→ %s\n",
		total, total, distinct, *out)
}

func fatal(err error) {
	fmt.Fprintln(os.Stderr, "❌", err)
	os.Exit(1)
}

// runReorg 半确定档批量：Bus(RegexRedactor+MemoryLedger) → Target(Caller=
// bamlai 适配器) → BusCaller 注入生成器。LLM 产出的 digest 可能重复——重复
// 跳过并计数（唯一率口径不变：落盘行互异），拒绝即审计行留进程内台账.
func runReorg(corpus *subjectlang.Corpus, n int, out, target, gradeband, provider, model, modelVersion string) {
	bus, err := ai.NewBus(ai.RegexRedactor{}, ai.NewMemoryLedger())
	if err != nil {
		fatal(err)
	}
	err = bus.RegisterTarget(ai.Target{
		Name: target, Modality: ai.ModalityLLM,
		Provider: provider, Model: model, ModelVersion: modelVersion,
		Caller: bamlai.NewSentenceReorgCaller(),
	})
	if err != nil {
		fatal(err)
	}
	caller, err := ai.NewBusCaller(bus, "draft_sentence_reorg")
	if err != nil {
		fatal(err)
	}
	gen, err := subjectlang.NewSentenceReorgGenerator(corpus, caller, target)
	if err != nil {
		fatal(err)
	}

	words := make([]string, 0, len(corpus.Words))
	for w := range corpus.Words {
		words = append(words, w)
	}
	sort.Strings(words)
	if len(words) == 0 {
		fatal(fmt.Errorf("语料词表为空"))
	}

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Minute)
	defer cancel()

	if err := os.MkdirAll(out, 0o755); err != nil {
		fatal(err)
	}
	var sb strings.Builder
	seen := map[string]bool{}
	accepted, dup, rejected := 0, 0, 0
	for i := 0; accepted < n && i < n*10; i++ {
		word := words[i%len(words)]
		inst, err := gen.Draft(ctx, word, gradeband)
		if err != nil {
			rejected++
			fmt.Fprintf(os.Stderr, "⚠️ draft(%s) 拒绝: %v\n", word, err)
			continue
		}
		digest, err := subjectlang.InstanceDigest(inst)
		if err != nil {
			fatal(err)
		}
		if seen[digest] {
			dup++
			continue
		}
		seen[digest] = true
		b, err := json.Marshal(inst)
		if err != nil {
			fatal(err)
		}
		sb.Write(b)
		sb.WriteByte('\n')
		accepted++
	}
	path := filepath.Join(out, "sentence_reorg.jsonl")
	if err := os.WriteFile(path, []byte(sb.String()), 0o644); err != nil {
		fatal(err)
	}
	fmt.Printf("✅ 句子重组：%d 接受 / %d 重复跳过 / %d 拒绝（门与可解性 fail-closed）→ %s\n",
		accepted, dup, rejected, path)
	if accepted == 0 {
		fatal(fmt.Errorf("零接受——检查 LLM 端点与凭据（fail-closed，不产伪行）"))
	}
}
