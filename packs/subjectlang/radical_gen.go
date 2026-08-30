package subjectlang

import (
	"errors"
	"fmt"

	"github.com/Cloudbird-Software/AI_Web_School/registry"
)

// TplRadical 是「偏旁归类（给字选偏旁）」母题 id。
const TplRadical = "tpl-sl-radical-sc"

// RadicalSourceBuiltin 是偏旁内置数据的来源标识（非 manifest 语料——纯包内数据）。
const RadicalSourceBuiltin = "builtin-subjectlang-v1"

// minRadicalChars 是参数空间下限（内置数据规模口径：≥100 例字）。
const minRadicalChars = 100

// minRadicalKinds 是偏旁种类下限（干扰项需 ≥3 个他部偏旁，种类过少即无区分度）。
const minRadicalKinds = 8

// RadicalVocab 装载后的偏旁→例字数据（不可变查询面）。
type RadicalVocab struct {
	SourceID    string
	Radicals    []string            // 偏旁稳定序（数据表序）
	Chars       []string            // 例字稳定序（偏旁序 × 组内序）
	CharRadical map[string]string   // 例字 → 唯一偏旁（判分地面真值）
	RadicalSet  map[string]bool     // 偏旁集合（干扰项合法性判定域）
	CharOf      map[string][]string // 偏旁 → 例字集（稳定序）
}

// LoadRadicalVocab 装载内置偏旁数据并做一致性互证。
func LoadRadicalVocab() (*RadicalVocab, error) {
	return buildRadicalVocab(radicalTable)
}

// buildRadicalVocab 从数据行构建（独立于包级表，供负例测试注入坏数据验证防线）。
func buildRadicalVocab(groups []radicalGroup) (*RadicalVocab, error) {
	v := &RadicalVocab{
		SourceID:    RadicalSourceBuiltin,
		CharRadical: map[string]string{},
		RadicalSet:  map[string]bool{},
		CharOf:      map[string][]string{},
	}
	for _, grp := range groups {
		if grp.Radical == "" {
			return nil, fmt.Errorf("偏旁数据出现空偏旁行")
		}
		if len(grp.Chars) == 0 {
			return nil, fmt.Errorf("偏旁 %q 例字为空（判定域为零）", grp.Radical)
		}
		if v.RadicalSet[grp.Radical] {
			return nil, fmt.Errorf("偏旁 %q 重复出现（数据口径破坏）", grp.Radical)
		}
		v.RadicalSet[grp.Radical] = true
		v.Radicals = append(v.Radicals, grp.Radical)
		for _, ch := range grp.Chars {
			if !isSingleRune(ch) {
				return nil, fmt.Errorf("偏旁 %q 例字 %q 非单字", grp.Radical, ch)
			}
			if prev, dup := v.CharRadical[ch]; dup {
				return nil, fmt.Errorf("例字 %q 重复归部（%s 与 %s——一字多部争议字必须不收）", ch, prev, grp.Radical)
			}
			v.CharRadical[ch] = grp.Radical
			v.Chars = append(v.Chars, ch)
			v.CharOf[grp.Radical] = append(v.CharOf[grp.Radical], ch)
		}
	}
	if len(v.Radicals) < minRadicalKinds {
		return nil, fmt.Errorf("偏旁 %d 种低于下限 %d（干扰项无区分度）", len(v.Radicals), minRadicalKinds)
	}
	if len(v.Chars) < minRadicalChars {
		return nil, fmt.Errorf("例字 %d 低于规模下限 %d", len(v.Chars), minRadicalChars)
	}
	return v, nil
}

// genRadical 是「偏旁归类」单选母题：给汉字选偏旁。
// 干扰 = 其他偏旁（数据口径一字一部——任何他部偏旁对本字必错）；答案位确定性
// 轮换；校验器独立重判（字表归部复核 + 干扰偏旁合法性）。
type genRadical struct {
	entry registry.Entry
	spec  map[string]any
	vocab *RadicalVocab
	size  int // = 例字数 × C(偏旁数-1, 3)
}

// newRadicalGen 构造；数据规模不足即错误（fail-closed）。
func newRadicalGen(vocab *RadicalVocab) (Generator, error) {
	if vocab == nil || len(vocab.Chars) < minRadicalChars {
		return nil, fmt.Errorf("偏旁数据未装载或例字低于下限 %d（fail-closed）", minRadicalChars)
	}
	if len(vocab.Radicals) < 5 {
		return nil, fmt.Errorf("偏旁 %d 种不足以构造稳定四选项（≥5）", len(vocab.Radicals))
	}
	g := &genRadical{
		entry: registry.Entry{ID: TplRadical, Version: "1.0.0"},
		vocab: vocab,
		size:  len(vocab.Chars) * comb3(len(vocab.Radicals)-1),
	}
	if g.size <= 0 {
		return nil, fmt.Errorf("偏旁归类参数空间不足")
	}
	g.spec = map[string]any{
		"objective":    "汉字偏旁归类（确定性档：内置一字一部数据 + 校验器归部复核）",
		"slots":        []string{"char", "radical", "distractor_1..3", "correct_index"},
		"variation":    []string{"char ∈ 例字表", "distractor ∈ 偏旁表∖正确偏旁"},
		"presentation": "四选一：选出该字的偏旁",
		"answer":       "correct_index(1..4)，按 index 确定性轮换",
		"distractors":  "他部偏旁（一字一部口径保证必错）",
	}
	return g, nil
}

// Entry/Spec/Size 实现 Generator。
func (g *genRadical) Entry() registry.Entry { return g.entry }
func (g *genRadical) Spec() map[string]any  { return g.spec }
func (g *genRadical) Size() int             { return g.size }

// Instance 纯索引函数：index = 例字序 + 偏旁三元组组合序×例字数（低位轮换例字）。
func (g *genRadical) Instance(index int) (*Instance, error) {
	if index < 0 || index >= g.size {
		return nil, fmt.Errorf("index %d 超出参数空间 [0,%d)", index, g.size)
	}
	n := len(g.vocab.Chars)
	ci := index % n
	comb := index / n
	ch := g.vocab.Chars[ci]
	trueRadical := g.vocab.CharRadical[ch]
	// 干扰池 = 其余偏旁（稳定序去正确部）。
	pool := make([]string, 0, len(g.vocab.Radicals)-1)
	for _, r := range g.vocab.Radicals {
		if r != trueRadical {
			pool = append(pool, r)
		}
	}
	triple, err := combKth3Idx(len(pool), comb)
	if err != nil {
		return nil, err
	}
	correctIdx := index%4 + 1 // 答案位确定性轮换
	opts := make([]string, 4)
	j := 0
	for pos := 1; pos <= 4; pos++ {
		if pos == correctIdx {
			opts[pos-1] = trueRadical
			continue
		}
		opts[pos-1] = pool[triple[j]]
		j++
	}
	errBinds := make([]map[string]any, 0, 3)
	for pos := 1; pos <= 4; pos++ {
		if pos == correctIdx {
			continue
		}
		errBinds = append(errBinds, map[string]any{
			"slot":          fmt.Sprintf("distractor_%d", pos),
			"error_type_id": "lang.chr.wrong_radical",
		})
	}
	inst := &Instance{
		TemplateID: g.entry.ID,
		Locale:     "zh-Hans",
		Objective: map[string]any{
			"kp":        "lang.chr.radical",
			"gradeband": "L",
		},
		InteractionRef: map[string]any{
			"interaction_id":     "single_choice",
			"interaction_params": map[string]any{"options": toAnySlice(opts)},
		},
		Content: map[string]any{
			"stem":      fmt.Sprintf("选偏旁：「%s」字的偏旁是哪一个？", ch),
			"options":   toAnySlice(opts),
			"answer":    correctIdx,
			"char":      ch,
			"radical":   trueRadical,
			"source_id": g.vocab.SourceID,
		},
		ScoringRef: map[string]any{
			"scorer_id":     "exact_match",
			"scorer_params": map[string]any{"answer": correctIdx},
		},
		ErrorBindings: errBinds,
		Lineage: map[string]any{
			"tier":   "A",
			"params": map[string]any{"index": index, "char_index": ci, "comb": comb, "correct_index": correctIdx},
		},
	}
	return inst, nil
}

// ── 独立校验器（防共谋）：不读生成器内部状态，只看实例六块 + 归部数据重判。──

// 偏旁归类族哨兵错误（errors.Is 判别类别；细节原因用 %w 包裹）。
var (
	ErrRadicalTpl             = errors.New("subjectlang: 偏旁母题 id 不符")
	ErrRadicalScorer          = errors.New("subjectlang: 偏旁评分器非 exact_match")
	ErrRadicalInteraction     = errors.New("subjectlang: 偏旁交互形态不符")
	ErrRadicalCrossMisalign   = errors.New("subjectlang: 偏旁 content/scoring 答案不一致")
	ErrRadicalStemMalformed   = errors.New("subjectlang: 偏旁题干缺槽位（例字解析失败）")
	ErrRadicalOptionInvalid   = errors.New("subjectlang: 偏旁选项形态非法")
	ErrRadicalCharNotInVocab  = errors.New("subjectlang: 例字不在偏旁数据")
	ErrRadicalAnswerMismatch  = errors.New("subjectlang: 答案位与正确偏旁不符")
	ErrRadicalRadicalMismatch = errors.New("subjectlang: 正确偏旁与归部数据不符")
	ErrRadicalDistractorBad   = errors.New("subjectlang: 干扰偏旁非法（未知偏旁或恰为本字归部）")
)

// RadicalValidator 偏旁归类独立校验器：
//  1. 模板 id / 评分器 exact_match / 交互 single_choice；
//  2. content 答案与 scoring 答案一致；
//  3. 题干反解析出例字（「」内）；
//  4. 四选项互异，答案位 1..4 且选项==content.radical；
//  5. 例字在数据内，content.radical==归部数据判定（地面真值重判）；
//  6. 每个干扰偏旁是已知偏旁且≠本字归部。
type RadicalValidator struct{ vocab *RadicalVocab }

// NewRadicalValidator 构造；数据为空即错误（fail-closed 落构造期）。
func NewRadicalValidator(vocab *RadicalVocab) (*RadicalValidator, error) {
	if vocab == nil || len(vocab.Chars) == 0 {
		return nil, fmt.Errorf("偏旁数据未装载（判定域为零）")
	}
	return &RadicalValidator{vocab: vocab}, nil
}

// Validate 独立重判一个偏旁归类实例；nil 即错误。
func (v *RadicalValidator) Validate(inst *Instance) error {
	if inst == nil {
		return fmt.Errorf("实例为 nil")
	}
	if inst.TemplateID != TplRadical {
		return fmt.Errorf("%w: %q", ErrRadicalTpl, inst.TemplateID)
	}
	if got := inst.InteractionRef["interaction_id"]; got != "single_choice" {
		return fmt.Errorf("%w: %v", ErrRadicalInteraction, got)
	}
	if got := inst.ScoringRef["scorer_id"]; got != "exact_match" {
		return fmt.Errorf("%w: %v", ErrRadicalScorer, got)
	}
	answer, _ := inst.Content["answer"].(int)
	scorerAnswer, _ := inst.ScoringRef["scorer_params"].(map[string]any)["answer"].(int)
	if answer != scorerAnswer {
		return fmt.Errorf("%w: content=%d scoring=%d", ErrRadicalCrossMisalign, answer, scorerAnswer)
	}
	stem, _ := inst.Content["stem"].(string)
	ch := extractBracketed(stem)
	if ch == "" {
		return fmt.Errorf("%w: %q", ErrRadicalStemMalformed, stem)
	}
	opts, err := optionStrings(inst)
	if err != nil {
		return fmt.Errorf("%w: %v", ErrRadicalOptionInvalid, err)
	}
	if len(opts) != 4 {
		return fmt.Errorf("%w: 选项数 %d", ErrRadicalOptionInvalid, len(opts))
	}
	distinct := map[string]bool{}
	for _, o := range opts {
		if distinct[o] {
			return fmt.Errorf("%w: 选项 %q 重复", ErrRadicalOptionInvalid, o)
		}
		distinct[o] = true
	}
	if answer < 1 || answer > 4 {
		return fmt.Errorf("%w: 答案位 %d 越界", ErrRadicalOptionInvalid, answer)
	}
	radical, _ := inst.Content["radical"].(string)
	if opts[answer-1] != radical {
		return fmt.Errorf("%w: 答案位选项 %q ≠ radical %q", ErrRadicalAnswerMismatch, opts[answer-1], radical)
	}
	trueRadical, ok := v.vocab.CharRadical[ch]
	if !ok {
		return fmt.Errorf("%w: %q", ErrRadicalCharNotInVocab, ch)
	}
	if trueRadical != radical {
		return fmt.Errorf("%w: %q 归部 %q ≠ 标注 %q", ErrRadicalRadicalMismatch, ch, trueRadical, radical)
	}
	for _, o := range opts {
		if o == radical {
			continue
		}
		if !v.vocab.RadicalSet[o] {
			return fmt.Errorf("%w: %q 非已知偏旁", ErrRadicalDistractorBad, o)
		}
		if o == trueRadical {
			return fmt.Errorf("%w: 干扰偏旁 %q 恰为本字归部", ErrRadicalDistractorBad, o)
		}
	}
	return nil
}
