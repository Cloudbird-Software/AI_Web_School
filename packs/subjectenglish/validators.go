package subjectenglish

import (
	"errors"
	"fmt"
	"strconv"
	"strings"
)

// validators.go —— 独立校验器（防共谋）：不读生成器内部状态，只看实例
// content/scoring_ref/interaction_ref，用本文件独立双写的规则实现重判地面真值。
// 与 subjectmath validators 同纪律：每条拒绝都落在显式哨兵类别上，供负例组
// 断言「坏实例必须落在预期哨兵」。

// 哨兵错误（errors.Is 判别类别；细节原因用 %w 包裹）。
var (
	ErrWrongTemplate       = errors.New("subjectenglish: 实例母题 id 不符")
	ErrScorerUnsupported   = errors.New("subjectenglish: 评分器非 exact_match")
	ErrInteractionUnsupp   = errors.New("subjectenglish: 交互形态不符")
	ErrAnswerCrossMisalign = errors.New("subjectenglish: content 答案与 scoring 答案不一致")
	// 词汇拼写族
	ErrSpellStemMalformed = errors.New("subjectenglish: 拼写题干缺槽位（释义/字数/首字母解析失败）")
	ErrSpellNotInVocab    = errors.New("subjectenglish: 答案词不在词表内")
	ErrSpellGlossMismatch = errors.New("subjectenglish: 释义与答案词不符")
	ErrSpellInitialMis    = errors.New("subjectenglish: 首字母提示与答案词不符")
	ErrSpellCountMis      = errors.New("subjectenglish: 字数提示与答案词不符")
	// 语法单选族
	ErrGramFamilyUnknown  = errors.New("subjectenglish: 语法家族不在白名单")
	ErrGramPosMismatch    = errors.New("subjectenglish: 题词词性与家族不符")
	ErrGramStemMalformed  = errors.New("subjectenglish: 语法题干与题词不一致")
	ErrGramOptionsInvalid = errors.New("subjectenglish: 选项形态非法（数量/重复/缺正确项）")
	ErrGramAnswerMismatch = errors.New("subjectenglish: 标注答案与规则重判不符")
)

// VocabSpellValidator 词汇拼写独立校验器：
//  1. 模板 id / 评分器 exact_match / 交互 text_blank；
//  2. content 答案与 scoring 答案一致；
//  3. 题干可反解析出释义、字数、首字母三槽位；
//  4. 答案词在词表内（answer_in_vocab）；
//  5. 首字母提示匹配答案词首字母（initial 匹配释义所在词）；
//  6. 字数提示匹配答案词长度；释义与词表条目一致。
type VocabSpellValidator struct{ vocab *EnglishVocab }

// NewVocabSpellValidator 构造；词表为空即错误（fail-closed 落构造期）。
func NewVocabSpellValidator(v *EnglishVocab) (*VocabSpellValidator, error) {
	if v == nil || len(v.Entries) == 0 {
		return nil, fmt.Errorf("词表未装载或为空（拼写判定域为零）")
	}
	return &VocabSpellValidator{vocab: v}, nil
}

// Validate 独立重判一个拼写实例；nil 即错误。
func (v *VocabSpellValidator) Validate(inst *Instance) error {
	if inst == nil {
		return fmt.Errorf("实例为 nil")
	}
	if inst.TemplateID != TplVocabSpell {
		return fmt.Errorf("%w: %q", ErrWrongTemplate, inst.TemplateID)
	}
	answer, err := spellFields(inst)
	if err != nil {
		return err
	}
	gloss, count, initial, err := parseSpellStem(inst.Content["stem"])
	if err != nil {
		return err
	}
	entry, ok := v.vocab.Entries[answer]
	if !ok {
		return fmt.Errorf("%w: %q", ErrSpellNotInVocab, answer)
	}
	if entry.Gloss != gloss {
		return fmt.Errorf("%w: 答案 %q 表内释义 %q ≠ 题干释义 %q", ErrSpellGlossMismatch, answer, entry.Gloss, gloss)
	}
	if len(answer) != count {
		return fmt.Errorf("%w: 答案 %d 字母 ≠ 提示 %d", ErrSpellCountMis, len(answer), count)
	}
	if answer[:1] != initial {
		return fmt.Errorf("%w: 答案首字母 %q ≠ 提示 %q", ErrSpellInitialMis, answer[:1], initial)
	}
	return nil
}

// spellFields 提取并交叉核对 content 答案 / scoring 答案 / 交互形态。
func spellFields(inst *Instance) (string, error) {
	if got := inst.InteractionRef["interaction_id"]; got != "text_blank" {
		return "", fmt.Errorf("%w: %v", ErrInteractionUnsupp, got)
	}
	if got := inst.ScoringRef["scorer_id"]; got != "exact_match" {
		return "", fmt.Errorf("%w: %v", ErrScorerUnsupported, got)
	}
	answer, _ := inst.Content["answer"].(string)
	if answer == "" {
		return "", fmt.Errorf("content 答案缺失")
	}
	scorerAnswer, _ := inst.ScoringRef["scorer_params"].(map[string]any)["answer"].(string)
	if scorerAnswer != answer {
		return "", fmt.Errorf("%w: content=%q scoring=%q", ErrAnswerCrossMisalign, answer, scorerAnswer)
	}
	return answer, nil
}

// parseSpellStem 独立反解析题干（与 spellStemFormat 双写：校验器不引用格式
// 常量，按文本槽位定位——生成器改模板而校验器未同步时立即暴露）。
func parseSpellStem(stemAny any) (gloss string, count int, initial string, err error) {
	stem, _ := stemAny.(string)
	g1 := strings.Index(stem, "「")
	g2 := strings.Index(stem, "」")
	c1 := strings.Index(stem, "（")
	c2 := strings.Index(stem, " 个字母")
	i1 := strings.Index(stem, "以 ")
	i2 := strings.Index(stem, " 开头")
	if g1 < 0 || g2 <= g1 || c1 < 0 || c2 <= c1 || i1 < 0 || i2 <= i1 {
		return "", 0, "", fmt.Errorf("%w: %q", ErrSpellStemMalformed, stem)
	}
	gloss = stem[g1+len("「") : g2]
	n, perr := strconv.Atoi(stem[c1+len("（") : c2])
	if perr != nil {
		return "", 0, "", fmt.Errorf("%w: 字数槽位非整数: %q", ErrSpellStemMalformed, stem)
	}
	count = n
	initial = stem[i1+len("以 ") : i2]
	return gloss, count, initial, nil
}

// GramSCValidator 语法单选独立校验器：
//  1. 模板 id / 评分器 exact_match / 交互 single_choice；
//  2. content 答案与 scoring 答案一致且答案位在选项界内；
//  3. 家族白名单 + 题词词性与家族相符（article/plural→n，third_person→v）；
//  4. 题干包含题词（渲染一致性）；
//  5. 选项数量/互异；规则双写重判的正确项必须在选项内，标注答案位即其位。
type GramSCValidator struct{ vocab *EnglishVocab }

// NewGramSCValidator 构造；词表为空即错误。
func NewGramSCValidator(v *EnglishVocab) (*GramSCValidator, error) {
	if v == nil || len(v.Entries) == 0 {
		return nil, fmt.Errorf("词表未装载或为空（语法判定域为零）")
	}
	return &GramSCValidator{vocab: v}, nil
}

// Validate 独立重判一个语法单选实例；nil 即错误。
func (v *GramSCValidator) Validate(inst *Instance) error {
	if inst == nil {
		return fmt.Errorf("实例为 nil")
	}
	if inst.TemplateID != TplGramSC {
		return fmt.Errorf("%w: %q", ErrWrongTemplate, inst.TemplateID)
	}
	if got := inst.InteractionRef["interaction_id"]; got != "single_choice" {
		return fmt.Errorf("%w: %v", ErrInteractionUnsupp, got)
	}
	if got := inst.ScoringRef["scorer_id"]; got != "exact_match" {
		return fmt.Errorf("%w: %v", ErrScorerUnsupported, got)
	}
	family, _ := inst.Content["family"].(string)
	word, _ := inst.Content["word"].(string)
	ansInt, ok := inst.Content["answer"].(int)
	if !ok {
		return fmt.Errorf("%w: content 答案非整数索引", ErrGramAnswerMismatch)
	}
	scorerAnswer, ok := inst.ScoringRef["scorer_params"].(map[string]any)["answer"].(int)
	if !ok || scorerAnswer != ansInt {
		return fmt.Errorf("%w: content=%v scoring=%v", ErrAnswerCrossMisalign, ansInt, scorerAnswer)
	}
	switch family {
	case famArticle, famThird, famPlural:
	default:
		return fmt.Errorf("%w: %q", ErrGramFamilyUnknown, family)
	}
	entry, ok := v.vocab.Entries[word]
	if !ok {
		return fmt.Errorf("%w: 题词 %q 不在词表", ErrGramPosMismatch, word)
	}
	wantPos := PosNoun
	if family == famThird {
		wantPos = PosVerb
	}
	if entry.Pos != wantPos {
		return fmt.Errorf("%w: 家族 %q 要求词性 %q，题词 %q 实为 %q",
			ErrGramPosMismatch, family, wantPos, word, entry.Pos)
	}
	stem, _ := inst.Content["stem"].(string)
	if !strings.Contains(stem, word) {
		return fmt.Errorf("%w: 题干不含题词 %q", ErrGramStemMalformed, word)
	}
	optsAny, _ := inst.Content["options"].([]any)
	if ansInt < 1 || ansInt > len(optsAny) {
		return fmt.Errorf("%w: 答案位 %d 超出选项界 [1,%d]", ErrGramAnswerMismatch, ansInt, len(optsAny))
	}
	opts := make([]string, 0, len(optsAny))
	seen := map[string]bool{}
	for _, o := range optsAny {
		s, _ := o.(string)
		if s == "" || seen[s] {
			return fmt.Errorf("%w: 选项空值或重复 %q", ErrGramOptionsInvalid, s)
		}
		seen[s] = true
		opts = append(opts, s)
	}
	var derived string
	switch family {
	case famArticle:
		if len(opts) != 2 {
			return fmt.Errorf("%w: 冠词题应 2 选项，实得 %d", ErrGramOptionsInvalid, len(opts))
		}
		derived = refArticle(word)
	case famThird:
		if len(opts) != 4 {
			return fmt.Errorf("%w: 三单题应 4 选项，实得 %d", ErrGramOptionsInvalid, len(opts))
		}
		derived = refThirdPerson(word)
	case famPlural:
		if len(opts) != 4 {
			return fmt.Errorf("%w: 复数题应 4 选项，实得 %d", ErrGramOptionsInvalid, len(opts))
		}
		derived = refPlural(word)
	}
	if !seen[derived] {
		return fmt.Errorf("%w: 规则重判正确项 %q 不在选项内", ErrGramOptionsInvalid, derived)
	}
	if opts[ansInt-1] != derived {
		return fmt.Errorf("%w: 标注位 %d = %q，规则重判 %q", ErrGramAnswerMismatch, ansInt, opts[ansInt-1], derived)
	}
	return nil
}

// ── 校验器侧规则双写（与 gram_sc.go 的生成器实现结构不同构：此处按末字节
// switch 分派而非后缀枚举——两套实现同规范互证，单边改错即被全域扫描暴露）──

// refArticle 校验器侧 a/an 重判。
func refArticle(noun string) string {
	if len(noun) > 0 {
		switch noun[0] {
		case 'a', 'e', 'i', 'o', 'u':
			return "an"
		}
	}
	return "a"
}

// refThirdPerson 校验器侧三单重判。
func refThirdPerson(verb string) string {
	n := len(verb)
	last := verb[n-1]
	switch last {
	case 's', 'x', 'z', 'o':
		return verb + "es"
	case 'h': // ch/sh：看倒数第二字节
		if n >= 2 && (verb[n-2] == 'c' || verb[n-2] == 's') {
			return verb + "es"
		}
	case 'y':
		if n >= 2 && !isVowelLetter(verb[n-2]) {
			return verb[:n-1] + "ies"
		}
	}
	return verb + "s"
}

// refPlural 校验器侧复数重判（名词域无 o 结尾词，o 分支不存在——两族规则
// 的差异点在本函数显式缺席，而非共享实现里隐式合并）。
func refPlural(noun string) string {
	n := len(noun)
	last := noun[n-1]
	switch last {
	case 's', 'x', 'z':
		return noun + "es"
	case 'h': // ch/sh
		if n >= 2 && (noun[n-2] == 'c' || noun[n-2] == 's') {
			return noun + "es"
		}
	case 'y':
		if n >= 2 && !isVowelLetter(noun[n-2]) {
			return noun[:n-1] + "ies"
		}
	}
	return noun + "s"
}
