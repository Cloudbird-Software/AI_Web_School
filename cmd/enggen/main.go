// Command enggen 是英语轮确定性实例批量生成入口（mathgen/langgen 同构）。
//
// 用法：enggen -n 20 -out out/enggen/
// 母题生成 n 个实例 → 全部过独立校验器 → 结构互异断言（唯一率 100%）
// → 写 JSONL（每行一实例）+ 汇总报告到 stdout。
package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"path/filepath"
	"strings"

	"github.com/Cloudbird-Software/AI_Web_School/packs/subjectenglish"
)

func main() {
	n := flag.Int("n", 20, "每母题实例数")
	out := flag.String("out", "out/enggen/", "JSONL 输出目录")
	flag.Parse()

	vocab, err := subjectenglish.LoadEnglishVocab(
		filepath.Join("content", "sources", "corpus", "manifest.yaml"),
		"eng-basic-vocab-v1")
	if err != nil {
		fatal(err)
	}
	gens, err := subjectenglish.BuiltinGenerators(vocab)
	if err != nil {
		fatal(err)
	}
	spellV, err := subjectenglish.NewVocabSpellValidator(vocab)
	if err != nil {
		fatal(err)
	}
	gramV, err := subjectenglish.NewGramSCValidator(vocab)
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
			// 批量面真跑独立校验器（不是只在测试里跑）：按模板分派，逐实例重判。
			if g.Entry().ID == subjectenglish.TplGramSC {
				if verr := gramV.Validate(inst); verr != nil {
					fatal(fmt.Errorf("%s Instance(%d) 校验器拒绝: %w", g.Entry().ID, i, verr))
				}
			} else if verr := spellV.Validate(inst); verr != nil {
				fatal(fmt.Errorf("%s Instance(%d) 校验器拒绝: %w", g.Entry().ID, i, verr))
			}
			digest, derr := subjectenglish.InstanceDigest(inst)
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
		name := strings.TrimPrefix(g.Entry().ID, "tpl-se-") + ".jsonl"
		if err := os.WriteFile(filepath.Join(*out, name), []byte(sb.String()), 0o644); err != nil {
			fatal(err)
		}
		fmt.Printf("✅ %s: %d 实例，唯一率 100%%（校验器独立重判全过）\n", g.Entry().ID, *n)
	}
	fmt.Printf("✅ 英语轮批量：%d/%d 合格、唯一率 100%%（%d 摘要全局唯一）→ %s\n",
		total, total, distinct, *out)
}

func fatal(err error) {
	fmt.Fprintln(os.Stderr, "❌", err)
	os.Exit(1)
}
