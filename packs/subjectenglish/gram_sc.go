package subjectenglish

import (
	"fmt"
	"strings"

	"github.com/Cloudbird-Software/AI_Web_School/registry"
)

// TplGramSC 是「语法单选」母题 id（issue #34 §六确定性档英语行）。
const TplGramSC = "tpl-se-gram-sc"

// 语法家族（题面与校验重判共用同一白名单）。
const (
	famArticle = "article"      // a/an 冠词
	famThird   = "third_person" // 动词第三人称单数
	famPlural  = "plural"       // 名词复数
)

// thirdPersonVerbs 是三单题的动词封闭集：题干句式 "He ____ (v) every morning."
// 语义自洽的不及物/自主子集（表内动词全量并非都能单用，如 know/give 需宾语）。
// 构造期逐一校验「在词表且 pos=v」，fail-closed——词表变动破坏该集即停线。
var thirdPersonVerbs = []string{
	"run", "jump", "sing", "read", "write", "walk", "talk", "play",
	"eat", "drink", "sleep", "smile", "cry", "swim", "dance", "study",
	"work", "cook", "draw", "sit", "stand", "come", "wait", "fly",
	"listen", "learn", "think", "teach", "relax",
}

// gramItem 是一个参数点：家族 × 词。
type gramItem struct {
	family string
	word   string
}

// genGramSC 是「语法单选」母题：a/an、动词第三人称单数、名词复数三类基础
// 规则题，规则确定性生成（正确项由拼写规则推导，干扰项 = 规则混淆形态），
// 校验器独立重判（双写规则实现互证）。参数空间 = Σ 家族词数，逐点互异。
type genGramSC struct {
	entry registry.Entry
	spec  map[string]any
	vocab *EnglishVocab
	items []gramItem
	size  int
}

// newGramSCGen 构造；词性分域规模不足或三单封闭集被词表变动破坏即错误。
func newGramSCGen(vocab *EnglishVocab) (Generator, error) {
	if vocab == nil {
		return nil, fmt.Errorf("词表未装载（fail-closed）")
	}
	if len(vocab.NounList) < 8 || len(vocab.VerbList) < 8 {
		return nil, fmt.Errorf("名词 %d/动词 %d 不足以构造稳定语法题库（各 ≥8）",
			len(vocab.NounList), len(vocab.VerbList))
	}
	// 三单封闭集一致性：任何条目不在词表或词性漂移，立即停线（不留半残生成器）。
	for _, v := range thirdPersonVerbs {
		e, ok := vocab.Entries[v]
		if !ok {
			return nil, fmt.Errorf("三单封闭集动词 %q 不在词表（词表与生成器失配）", v)
		}
		if e.Pos != PosVerb {
			return nil, fmt.Errorf("三单封闭集动词 %q 词性漂移为 %q（应为 v）", v, e.Pos)
		}
	}
	items := make([]gramItem, 0, 2*len(vocab.NounList)+len(thirdPersonVerbs))
	for _, n := range vocab.NounList {
		items = append(items, gramItem{famArticle, n})
	}
	for _, v := range thirdPersonVerbs {
		items = append(items, gramItem{famThird, v})
	}
	for _, n := range vocab.NounList {
		items = append(items, gramItem{famPlural, n})
	}
	g := &genGramSC{
		entry: registry.Entry{ID: TplGramSC, Version: "1.0.0"},
		vocab: vocab,
		items: items,
		size:  len(items),
	}
	g.spec = map[string]any{
		"objective":    "基础语法规则单选（确定性档：a/an、三单、复数——规则确定性推导 + 校验器独立重判）",
		"slots":        []string{"family", "word", "correct_index"},
		"variation":    []string{"family ∈ {article, third_person, plural}", "word ∈ 词性对应分域"},
		"presentation": "单选：a/an 两选一；三单/复数四选一（正确项+规则混淆干扰项）",
		"answer":       "correct_index(1..len(options))",
		"distractors":  "规则混淆形态（+s/+es/y→ies/原形 互为干扰）",
	}
	return g, nil
}

// Entry/Spec/Size 实现 Generator。
func (g *genGramSC) Entry() registry.Entry { return g.entry }
func (g *genGramSC) Spec() map[string]any  { return g.spec }
func (g *genGramSC) Size() int             { return g.size }

// Instance 纯索引函数：index → 家族×词的语法单选实例。答案位由 index 轮换
// （确定性，避免答案恒在同一位置）。
func (g *genGramSC) Instance(index int) (*Instance, error) {
	if index < 0 || index >= g.size {
		return nil, fmt.Errorf("index %d 超出参数空间 [0,%d)", index, g.size)
	}
	it := g.items[index]
	var stem, correct, kp string
	var opts []string
	switch it.family {
	case famArticle:
		// 冠词规则：元音字母开头 → an，否则 a（纯拼写判定，零例外词入集）。
		if isVowelLetter(it.word[0]) {
			correct = "an"
		} else {
			correct = "a"
		}
		opts = []string{"a", "an"}
		stem = fmt.Sprintf("Fill in the blank: I have ___ %s.", it.word)
		kp = "eng.gram.article"
	case famThird:
		correct = gramThirdForm(it.word)
		opts = gramCandidates(it.word)
		stem = fmt.Sprintf("Choose the correct verb form: He ____ (%s) every morning.", it.word)
		kp = "eng.gram.third_person"
	case famPlural:
		correct = gramPluralForm(it.word)
		opts = gramCandidates(it.word)
		stem = fmt.Sprintf("Choose the correct plural form: one %s, two ____.", it.word)
		kp = "eng.gram.plural"
	default:
		return nil, fmt.Errorf("未知语法家族 %q", it.family)
	}
	correctIdx := arrangeCorrect(opts, correct, index)
	errBinds := make([]map[string]any, 0, len(opts)-1)
	for pos := 1; pos <= len(opts); pos++ {
		if pos == correctIdx {
			continue
		}
		errBinds = append(errBinds, map[string]any{
			"slot":          fmt.Sprintf("distractor_%d", pos),
			"error_type_id": "eng.gram.rule_violation",
		})
	}
	inst := &Instance{
		TemplateID: g.entry.ID,
		Locale:     "en",
		Objective:  objective(kp),
		InteractionRef: map[string]any{
			"interaction_id":     "single_choice",
			"interaction_params": map[string]any{"options": toAnySlice(opts)},
		},
		Content: map[string]any{
			"stem":        stem,
			"blocks":      scBlocks(stem, opts),
			"options":     toAnySlice(opts),
			"answer":      correctIdx,
			"answer_form": correct,
			"family":      it.family,
			"word":        it.word,
			"source_id":   g.vocab.SourceID,
		},
		ScoringRef: map[string]any{
			"scorer_id":     "exact_match",
			"scorer_params": map[string]any{"answer": correctIdx},
		},
		ErrorBindings: errBinds,
		Lineage: map[string]any{
			"tier": "A",
			// 契约 §5.2：实例判别参数落 params.normalized（公式一 np 输入——
			// 不同实例必须产生不同 item_version_id，内容寻址才成立）。
			"params": map[string]any{"normalized": map[string]any{"index": index, "family": it.family, "word": it.word}},
		},
	}
	return inst, nil
}

// arrangeCorrect 把正确项轮换到第 (index mod len + 1) 位，其余位按原序回填
// （确定性答案位轮换，避免答案恒在同一位置）。返回正确项位次（1 基）。
func arrangeCorrect(opts []string, correct string, index int) int {
	correctIdx := index%len(opts) + 1
	rest := make([]string, 0, len(opts)-1)
	seenCorrect := false
	for _, o := range opts {
		if o == correct && !seenCorrect {
			seenCorrect = true // 正确项只挪位一次（同形候选理论上不存在，防御性只取首个）
			continue
		}
		rest = append(rest, o)
	}
	j := 0
	for pos := 1; pos <= len(opts); pos++ {
		if pos == correctIdx {
			continue
		}
		opts[pos-1] = rest[j]
		j++
	}
	opts[correctIdx-1] = correct
	return correctIdx
}

// gramThirdForm 生成器侧三单规则：s/x/z/ch/sh/o 结尾 → +es；辅音+y → y→ies；
// 其余 → +s（与校验器 refThirdPerson 独立双写）。
func gramThirdForm(verb string) string {
	if strings.HasSuffix(verb, "s") || strings.HasSuffix(verb, "x") ||
		strings.HasSuffix(verb, "z") || strings.HasSuffix(verb, "ch") ||
		strings.HasSuffix(verb, "sh") || strings.HasSuffix(verb, "o") {
		return verb + "es"
	}
	if y2ies(verb) {
		return verb[:len(verb)-1] + "ies"
	}
	return verb + "s"
}

// gramPluralForm 生成器侧复数规则：词表名词无 o/f 结尾，规则面与三单同形
// （s/x/z/ch/sh → +es；辅音+y → ies；元音+y 及其余 → +s），独立函数防两族
// 规则被无意耦合。
func gramPluralForm(noun string) string {
	if strings.HasSuffix(noun, "s") || strings.HasSuffix(noun, "x") ||
		strings.HasSuffix(noun, "z") || strings.HasSuffix(noun, "ch") ||
		strings.HasSuffix(noun, "sh") {
		return noun + "es"
	}
	if y2ies(noun) {
		return noun[:len(noun)-1] + "ies"
	}
	return noun + "s"
}

// y2ies 判定「辅音字母 + y」结尾（需变 y 为 ies 的规则类）。
func y2ies(w string) bool {
	return len(w) >= 2 && w[len(w)-1] == 'y' && !isVowelLetter(w[len(w)-2])
}

// gramCandidates 构造四候选：+s / +es / ies 形态 / 原形——正确项必在其中，
// 其余即规则混淆干扰项（对任何词表词两两互异）。
func gramCandidates(w string) []string {
	ies := w + "ies"
	if y2ies(w) {
		ies = w[:len(w)-1] + "ies"
	}
	return []string{w + "s", w + "es", ies, w}
}
