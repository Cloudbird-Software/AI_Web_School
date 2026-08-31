package subjectenglish

import (
	"fmt"

	"github.com/Cloudbird-Software/AI_Web_School/registry"
)

// TplVocabSpell 是「词汇拼写」母题 id（issue #34 §六确定性档英语行）。
const TplVocabSpell = "tpl-se-vocab-spell"

// spellStemFormat 是题干唯一模板（校验器按同一格式独立反解析：释义、字数、
// 首字母三个槽位缺一不可——模板即契约，双方各写一套解析互证）。
const spellStemFormat = "拼写：表示「%s」的英语单词（%d 个字母，以 %s 开头）：%s＿＿＿＿"

// genVocabSpell 是「词汇拼写」母题：给中文释义+首字母，拼出英语词（字符串
// 填空形态，scoring exact_match）。参数空间 = 词表逐词一点（≤120），结构互异
// 天然成立并由全域扫描断言兜底。
type genVocabSpell struct {
	entry registry.Entry
	spec  map[string]any
	vocab *EnglishVocab
	size  int
}

// newVocabSpellGen 构造；词表规模不足即错误（参数空间是互异性的来源——不足
// 则管线降级失败而非静默重复）。
func newVocabSpellGen(vocab *EnglishVocab) (Generator, error) {
	if vocab == nil || len(vocab.EntryList) < 10 {
		n := 0
		if vocab != nil {
			n = len(vocab.EntryList)
		}
		return nil, fmt.Errorf("词表 %d 条不足以构成稳定拼写题库（≥10）", n)
	}
	g := &genVocabSpell{
		entry: registry.Entry{ID: TplVocabSpell, Version: "1.0.0"},
		vocab: vocab,
		size:  len(vocab.EntryList),
	}
	g.spec = map[string]any{
		"objective":    "按中文释义与首字母拼出英语基础词（确定性档：answer_in_vocab + 首字母/字数/释义三重独立复核）",
		"slots":        []string{"gloss", "letter_count", "initial"},
		"variation":    []string{"answer ∈ vocab.words", "stem 由 gloss/字数/首字母确定性渲染"},
		"presentation": "字符串填空：中文释义 + 字数 + 首字母 → 拼写完整单词",
		"answer":       "answer ∈ vocab.words（exact_match）",
		"distractors":  "无选项（填空形态；错拼由 error_binding eng.vocab.misspell 承接）",
	}
	return g, nil
}

// Entry/Spec/Size 实现 Generator。
func (g *genVocabSpell) Entry() registry.Entry { return g.entry }
func (g *genVocabSpell) Spec() map[string]any  { return g.spec }
func (g *genVocabSpell) Size() int             { return g.size }

// Instance 纯索引函数：index → 词表第 index 词条的拼写题。
func (g *genVocabSpell) Instance(index int) (*Instance, error) {
	if index < 0 || index >= g.size {
		return nil, fmt.Errorf("index %d 超出参数空间 [0,%d)", index, g.size)
	}
	e := g.vocab.EntryList[index]
	initial := e.Word[:1]
	stem := fmt.Sprintf(spellStemFormat, e.Gloss, len(e.Word), initial, initial)
	inst := &Instance{
		TemplateID: g.entry.ID,
		Locale:     "en",
		Objective:  objective("eng.vocab.spell"),
		InteractionRef: map[string]any{
			"interaction_id":     "text_blank",
			"interaction_params": map[string]any{"blank_ids": []any{"b1"}},
		},
		Content: map[string]any{
			"stem":         stem,
			"blocks":       fillBlocks(stem, []string{"b1"}),
			"answer":       e.Word,
			"gloss":        e.Gloss,
			"letter_count": len(e.Word),
			"initial":      initial,
			"source_id":    g.vocab.SourceID,
		},
		ScoringRef: map[string]any{
			"scorer_id":     "exact_match",
			"scorer_params": map[string]any{"answer": e.Word, "blank_id": "b1"},
		},
		ErrorBindings: []map[string]any{
			{
				"subject":         "blank:b1",
				"error_type_id":   "eng.vocab.misspell",
				"confidence_rule": "answer-value-neq-implies-error",
			},
		},
		Lineage: map[string]any{
			"tier": "A",
			// 契约 §5.2：实例判别参数落 params.normalized（公式一 np 输入——
			// 不同实例必须产生不同 item_version_id，内容寻址才成立）。
			"params": map[string]any{"normalized": map[string]any{"index": index}},
		},
	}
	return inst, nil
}
