package subjectlang

import (
	"errors"
	"fmt"
	"strings"

	"github.com/Cloudbird-Software/AI_Web_School/registry"
)

// TplWordRel 是「词义关系（近义/反义选词）」母题 id。
const TplWordRel = "tpl-sl-word-rel-sc"

// wordRelCandCap 是每条关系候选干扰的统一上限（稳定词表序截断，统一容量使
// 参数空间为规整「关系对 × 组合」矩阵，index 低位轮换关系对——小批量覆盖多词）。
const wordRelCandCap = 64

// genWordRel 是「词义关系」单选母题：「与X意思相近/相反的是」四选一。
// 正确项=关系表目标词；干扰=词表其他词（严格排除题词自身与该关系正确集——
// 装载器的角色唯一性已杜绝关系链，正确集恰为目标词）；校验器独立重判。
type genWordRel struct {
	entry registry.Entry
	spec  map[string]any
	rel   *WordRel
	cands [][]string // 每关系对的候选干扰（WordList 稳定序去自身/正确集，截断至 cap）
	size  int        // = 关系对数 × C(cap,3)
}

// newWordRelGen 构造；关系表低于节选下限即错误（fail-closed）。
func newWordRelGen(rel *WordRel) (Generator, error) {
	if rel == nil || len(rel.Entries) < minWordRelPairs {
		return nil, fmt.Errorf("词义关系表未装载或低于下限 %d（fail-closed）", minWordRelPairs)
	}
	g := &genWordRel{
		entry: registry.Entry{ID: TplWordRel, Version: "1.0.0"},
		rel:   rel,
	}
	for _, e := range rel.Entries {
		exclude := map[string]bool{e.Word: true}
		for _, t := range rel.AnswerOf(e.Word, e.Relation) {
			exclude[t] = true // 正确集：同关系目标词绝不入干扰
		}
		var cand []string
		for _, w := range rel.WordList {
			if len(cand) >= wordRelCandCap {
				break
			}
			if !exclude[w] {
				cand = append(cand, w)
			}
		}
		if len(cand) < wordRelCandCap {
			return nil, fmt.Errorf("关系对 %q→%q 候选干扰 %d 不足 %d（词表异常）",
				e.Word, e.Target, len(cand), wordRelCandCap)
		}
		g.cands = append(g.cands, cand)
	}
	g.size = len(rel.Entries) * comb3(wordRelCandCap)
	g.spec = map[string]any{
		"objective":    "近义/反义关系辨词（确定性档：关系表判分 + 校验器独立重判）",
		"slots":        []string{"word", "relation", "target_word", "distractor_1..3", "correct_index"},
		"variation":    []string{"(word,relation) ∈ 关系表", "distractor ∈ 词表∖(题词∪正确集)"},
		"presentation": "四选一：选出与题词意思相近/相反的词",
		"answer":       "correct_index(1..4)，按 index 确定性轮换",
		"distractors":  "词表内非同关系词（正确集零泄漏）",
	}
	return g, nil
}

// Entry/Spec/Size 实现 Generator。
func (g *genWordRel) Entry() registry.Entry { return g.entry }
func (g *genWordRel) Spec() map[string]any  { return g.spec }
func (g *genWordRel) Size() int             { return g.size }

// stemTemplate 题面（关系→句式；校验器按关键词独立反解，双写互证）。
func wordRelStem(relation, word string) string {
	if relation == RelAntonym {
		return fmt.Sprintf("选词：与「%s」意思相反的词是哪一个？", word)
	}
	return fmt.Sprintf("选词：与「%s」意思相近的词是哪一个？", word)
}

// Instance 纯索引函数：index = 关系对序 + 组合序×关系对数（低位轮换关系对）。
func (g *genWordRel) Instance(index int) (*Instance, error) {
	if index < 0 || index >= g.size {
		return nil, fmt.Errorf("index %d 超出参数空间 [0,%d)", index, g.size)
	}
	n := len(g.rel.Entries)
	ei := index % n
	comb := index / n
	e := g.rel.Entries[ei]
	triple, err := combKth3Idx(wordRelCandCap, comb)
	if err != nil {
		return nil, err
	}
	correctIdx := index%4 + 1 // 答案位确定性轮换
	opts := make([]string, 4)
	j := 0
	for pos := 1; pos <= 4; pos++ {
		if pos == correctIdx {
			opts[pos-1] = e.Target
			continue
		}
		opts[pos-1] = g.cands[ei][triple[j]]
		j++
	}
	errBinds := make([]map[string]any, 0, 3)
	for pos := 1; pos <= 4; pos++ {
		if pos == correctIdx {
			continue
		}
		errBinds = append(errBinds, map[string]any{
			"slot":          fmt.Sprintf("distractor_%d", pos),
			"error_type_id": "lang.sem.wrong_relation",
		})
	}
	inst := &Instance{
		TemplateID: g.entry.ID,
		Locale:     "zh-Hans",
		Objective:  objective("lang.sem." + mapRelKp(e.Relation)),
		InteractionRef: map[string]any{
			"interaction_id":     "single_choice",
			"interaction_params": map[string]any{"options": toAnySlice(opts)},
		},
		Content: map[string]any{
			"stem":        wordRelStem(e.Relation, e.Word),
			"blocks":      scBlocks(wordRelStem(e.Relation, e.Word), opts),
			"options":     toAnySlice(opts),
			"answer":      correctIdx,
			"word":        e.Word,
			"relation":    e.Relation,
			"answer_word": e.Target,
			"source_id":   g.rel.SourceID,
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
			"params": map[string]any{"normalized": map[string]any{"index": index, "entry_index": ei, "comb": comb, "correct_index": correctIdx}},
		},
	}
	return inst, nil
}

// mapRelKp 关系→kp 后缀（lang.sem.synonym / lang.sem.antonym）。
func mapRelKp(relation string) string {
	if relation == RelAntonym {
		return "antonym"
	}
	return "synonym"
}

// ── 独立校验器（防共谋）：不读生成器内部状态，只看实例六块 + 关系表重判。──

// 词义关系族哨兵错误（errors.Is 判别类别；细节原因用 %w 包裹）。
var (
	ErrWordRelTpl                 = errors.New("subjectlang: 词义关系母题 id 不符")
	ErrWordRelScorer              = errors.New("subjectlang: 词义关系评分器非 exact_match")
	ErrWordRelInteraction         = errors.New("subjectlang: 词义关系交互形态不符")
	ErrWordRelCrossMisalign       = errors.New("subjectlang: 词义关系 content/scoring 答案不一致")
	ErrWordRelStemMalformed       = errors.New("subjectlang: 词义关系题干缺槽位（题词/关系解析失败）")
	ErrWordRelRelationUnknown     = errors.New("subjectlang: 题干关系不在白名单")
	ErrWordRelOptionInvalid       = errors.New("subjectlang: 词义关系选项形态非法")
	ErrWordRelAnswerNotInVocab    = errors.New("subjectlang: 答案词不在词表")
	ErrWordRelAnswerMismatch      = errors.New("subjectlang: 答案位与答案词不符")
	ErrWordRelRelationMismatch    = errors.New("subjectlang: 题词与答案词无此关系（关系表重判不符）")
	ErrWordRelDistractorInCorrect = errors.New("subjectlang: 干扰词落在正确集（多正确答案）")
)

// WordRelValidator 词义关系独立校验器：
//  1. 模板 id / 评分器 exact_match / 交互 single_choice；
//  2. content 答案与 scoring 答案一致；
//  3. 题干反解析出题词与关系关键词（相近/相反，二者恰居其一）；
//  4. 四选项互异，答案位 1..4 且选项==content.answer_word；
//  5. 题词与答案词均在词表，且关系表确有 (题词,关系)→答案词（地面真值重判）；
//  6. 每个干扰词在词表且不在 (题词,关系) 正确集。
type WordRelValidator struct{ rel *WordRel }

// NewWordRelValidator 构造；关系表为空即错误（fail-closed 落构造期）。
func NewWordRelValidator(rel *WordRel) (*WordRelValidator, error) {
	if rel == nil || len(rel.Entries) == 0 {
		return nil, fmt.Errorf("词义关系表未装载（判定域为零）")
	}
	return &WordRelValidator{rel: rel}, nil
}

// Validate 独立重判一个词义关系实例；nil 即错误。
func (v *WordRelValidator) Validate(inst *Instance) error {
	if inst == nil {
		return fmt.Errorf("实例为 nil")
	}
	if inst.TemplateID != TplWordRel {
		return fmt.Errorf("%w: %q", ErrWordRelTpl, inst.TemplateID)
	}
	if got := inst.InteractionRef["interaction_id"]; got != "single_choice" {
		return fmt.Errorf("%w: %v", ErrWordRelInteraction, got)
	}
	if got := inst.ScoringRef["scorer_id"]; got != "exact_match" {
		return fmt.Errorf("%w: %v", ErrWordRelScorer, got)
	}
	answer, _ := inst.Content["answer"].(int)
	scorerAnswer, _ := inst.ScoringRef["scorer_params"].(map[string]any)["answer"].(int)
	if answer != scorerAnswer {
		return fmt.Errorf("%w: content=%d scoring=%d", ErrWordRelCrossMisalign, answer, scorerAnswer)
	}
	stem, _ := inst.Content["stem"].(string)
	word, relation, err := parseWordRelStem(stem)
	if err != nil {
		return err
	}
	opts, err := optionStrings(inst)
	if err != nil {
		return fmt.Errorf("%w: %v", ErrWordRelOptionInvalid, err)
	}
	if len(opts) != 4 {
		return fmt.Errorf("%w: 选项数 %d", ErrWordRelOptionInvalid, len(opts))
	}
	distinct := map[string]bool{}
	for _, o := range opts {
		if distinct[o] {
			return fmt.Errorf("%w: 选项 %q 重复", ErrWordRelOptionInvalid, o)
		}
		distinct[o] = true
	}
	if answer < 1 || answer > 4 {
		return fmt.Errorf("%w: 答案位 %d 越界", ErrWordRelOptionInvalid, answer)
	}
	answerWord, _ := inst.Content["answer_word"].(string)
	if opts[answer-1] != answerWord {
		return fmt.Errorf("%w: 答案位选项 %q ≠ answer_word %q", ErrWordRelAnswerMismatch, opts[answer-1], answerWord)
	}
	if !v.rel.WordSet[word] {
		return fmt.Errorf("%w: 题词 %q 不在词表", ErrWordRelAnswerNotInVocab, word)
	}
	targets := v.rel.AnswerOf(word, relation)
	if len(targets) == 0 {
		return fmt.Errorf("%w: 词表无 (%q,%s) 关系", ErrWordRelRelationMismatch, word, relation)
	}
	if !containsStr(targets, answerWord) {
		return fmt.Errorf("%w: (%q,%s) 目标 %v ≠ 答案词 %q", ErrWordRelRelationMismatch, word, relation, targets, answerWord)
	}
	correct := map[string]bool{}
	for _, t := range targets {
		correct[t] = true
	}
	for _, o := range opts {
		if o == answerWord {
			continue
		}
		if !v.rel.WordSet[o] {
			return fmt.Errorf("%w: 干扰词 %q 不在词表", ErrWordRelAnswerNotInVocab, o)
		}
		if correct[o] {
			return fmt.Errorf("%w: 干扰词 %q 与题词 %s", ErrWordRelDistractorInCorrect, o, relation)
		}
	}
	return nil
}

// parseWordRelStem 独立反解析题干（与 wordRelStem 双写：按「」与 关系关键词
// 定位——生成器改模板而校验器未同步时立即暴露）。
func parseWordRelStem(stem string) (word, relation string, err error) {
	i := strings.Index(stem, "「")
	j := strings.Index(stem, "」")
	if i < 0 || j <= i {
		return "", "", fmt.Errorf("%w: %q", ErrWordRelStemMalformed, stem)
	}
	word = stem[i+len("「") : j]
	hasSyn := strings.Contains(stem, "意思相近")
	hasAnt := strings.Contains(stem, "意思相反")
	switch {
	case hasSyn && !hasAnt:
		return word, RelSynonym, nil
	case hasAnt && !hasSyn:
		return word, RelAntonym, nil
	default:
		return "", "", fmt.Errorf("%w: 关系关键词不唯一: %q", ErrWordRelRelationUnknown, stem)
	}
}

// containsStr 线性包含判定（关系表目标词集规模=1，无需 set）。
func containsStr(ss []string, v string) bool {
	for _, s := range ss {
		if s == v {
			return true
		}
	}
	return false
}
