package subjectlang

import (
	"fmt"

	"github.com/Cloudbird-Software/AI_Web_School/registry"
)

// Generator/Instance 复用数学轮的学科包管线接口（issue #34 §二：语英轮共用
// 同一根轴——同一入库服务/同一校验门/同一证据链）。
type Generator = subjectmathGenerator
type Instance = subjectmathInstance

// TplCharRecognize 是「字辨认」母题 id（语文确定性档第一题）。
const TplCharRecognize = "tpl-sl-char-rec-sc"

// genCharRecognize 是「字辨认」单选母题（语文确定性档第一题）：
// 目标字 + 3 个同表干扰字 → 选出正确的字。校验器独立重判：
// 渲染文本重提四选项，答案必须在语料表内且与目标一致。
type genCharRecognize struct {
	entry  registry.Entry
	spec   map[string]any
	corpus *Corpus
	size   int
}

// newCharRecognizeGen 从语料构造生成器；表内可组合的四选项空间不足即错误
// （参数空间是结构互异的来源——不足则管线降级失败而非静默重复）。
func newCharRecognizeGen(corpus *Corpus) (Generator, error) {
	// 空间：目标字 n × 干扰三元组 C(n-1,3)（有序采样去重的组合数）。
	n := len(corpus.CharList)
	if n < 5 {
		return nil, fmt.Errorf("字表 %d 字不足以构造稳定四选项（≥5）", n)
	}
	size := n * comb3(n-1)
	if size <= 0 {
		return nil, fmt.Errorf("字表 %d 字的四选项组合空间不足", n)
	}
	g := &genCharRecognize{
		entry:  registry.Entry{ID: TplCharRecognize, Version: "1.0.0"},
		corpus: corpus,
		size:   size,
	}
	g.spec = map[string]any{
		"objective":    "在语料字表内辨认目标汉字（确定性档：char_in_corpus 判定）",
		"slots":        []string{"target_char", "distractor_1", "distractor_2", "distractor_3"},
		"variation":    []string{"target ∈ corpus.chars", "distractors ∈ corpus.chars\\target"},
		"presentation": "四选一：选出下面括号里正确的字",
		"answer":       "correct_index(1..4)",
		"distractors":  "同表干扰（避免不可辨形态差异之外的同字重复）",
	}
	return g, nil
}

// Entry/Spec/Size 实现 Generator。
func (g *genCharRecognize) Entry() registry.Entry { return g.entry }
func (g *genCharRecognize) Spec() map[string]any  { return g.spec }
func (g *genCharRecognize) Size() int             { return g.size }

// Instance 纯索引函数：index → (target, 组合序三元组)。
// 组合序=字典序 C(n-1,3) 枚举，去重目标后稳定可回放。
func (g *genCharRecognize) Instance(index int) (*Instance, error) {
	if index < 0 || index >= g.size {
		return nil, fmt.Errorf("index %d 超出参数空间 [0,%d)", index, g.size)
	}
	n := len(g.corpus.CharList)
	// 空间分解：index = ti*comb3(n-1) + combo（目标字序 × 其余字三元组组合序）
	rest := n - 1
	comb := index % comb3(rest)
	ti := index / comb3(rest)
	if ti >= n {
		return nil, fmt.Errorf("index %d 目标越界（ti=%d≥%d）", index, ti, n)
	}
	target := g.corpus.CharList[ti]
	// 其余字表（去目标）取三元组
	rest3 := make([]string, 0, rest)
	for _, ch := range g.corpus.CharList {
		if ch != target {
			rest3 = append(rest3, ch)
		}
	}
	d1, d2, d3, err := kthComb3(rest3, comb)
	if err != nil {
		return nil, err
	}
	// 答案位（1..4）由 ti 决定（确定性轮换，避免答案恒在同一位置）
	correctIdx := ti%4 + 1
	opts := make([]string, 4)
	j := 0
	for pos := 1; pos <= 4; pos++ {
		if pos == correctIdx {
			opts[pos-1] = target
			continue
		}
		switch j {
		case 0:
			opts[pos-1] = d1
		case 1:
			opts[pos-1] = d2
		case 2:
			opts[pos-1] = d3
		}
		j++
	}
	prompt := fmt.Sprintf("选字：下面四个选项中，哪一个是语料字表里的「%s」？", target)
	blocks := scBlocks(prompt, opts)
	inst := &Instance{
		TemplateID: g.entry.ID,
		Locale:     "zh-Hans",
		Objective:  objective("lang.chr.recognize"),
		InteractionRef: map[string]any{
			"interaction_id":     "single_choice",
			"interaction_params": map[string]any{"options": opts},
		},
		Content: map[string]any{
			"stem":      prompt,
			"blocks":    blocks,
			"options":   toAnySlice(opts),
			"answer":    correctIdx,
			"target":    target,
			"source_id": g.corpus.SourceID,
		},
		ScoringRef: map[string]any{
			"scorer_id":     "exact_match",
			"scorer_params": map[string]any{"answer": correctIdx},
		},
		ErrorBindings: []map[string]any{
			{"slot": "distractor_1", "error_type_id": "lang.chr.confusable"},
			{"slot": "distractor_2", "error_type_id": "lang.chr.confusable"},
			{"slot": "distractor_3", "error_type_id": "lang.chr.confusable"},
		},
		Lineage: map[string]any{
			"tier": "A",
			// 契约 §5.2：实例判别参数落 params.normalized（公式一 np 输入——
			// 不同实例必须产生不同 item_version_id，内容寻址才成立）。
			"params": map[string]any{"normalized": map[string]any{"target_index": ti, "comb": comb, "correct_index": correctIdx}},
		},
	}
	return inst, nil
}

// comb3 返回 C(n,3)。
func comb3(n int) int {
	if n < 3 {
		return 0
	}
	return n * (n - 1) * (n - 2) / 6
}

// kthComb3 字典序第 k 个三元组合（k ∈ [0,C(n,3))）。
// 历史缺陷（#150 审计发现）：块大小误用 C(n-1-a,3)（应为 C(n-1-a,2)），
// k ≥ n-2 时枚举耗尽报错、下游批量越界。修复=委托 combKth3Idx 规范
// unrank（与 pinyin/radical 生成器同一实现，双射性质有测试覆盖）。
func kthComb3(items []string, k int) (string, string, string, error) {
	idx, err := combKth3Idx(len(items), k)
	if err != nil {
		return "", "", "", err
	}
	return items[idx[0]], items[idx[1]], items[idx[2]], nil
}
