package subjectlang

import (
	"errors"
	"fmt"
	"strings"

	"github.com/Cloudbird-Software/AI_Web_School/registry"
)

// TplPinyinChar 是「拼音选字」母题 id（语文确定性档：拼音→汉字辨认）。
const TplPinyinChar = "tpl-sl-pinyin-char-sc"

// 干扰项取材类别（题面近形/同韵优先，不足回落近音；类别同步落到 error_bindings）。
const (
	distrShape = "lang.pinyin.shape_confusion" // 近形字（如 大/太/天）
	distrRhyme = "lang.pinyin.rhyme_confusion" // 同韵母近音字
	distrSound = "lang.pinyin.near_sound"      // 其余近音字
)

// shapeGroups 近形字组（干扰取材域）。成员须在拼音字表内且互不同音——构造期
// 逐组过滤：字表节选变动破坏某组时该组自动收缩/失效，绝不产生脏选项。
var shapeGroups = [][]string{
	{"大", "天", "太"},
	{"木", "本", "术"},
	{"日", "目", "白", "自"},
	{"人", "入", "八"},
	{"土", "士"},
	{"田", "由", "甲", "电"},
	{"午", "牛"},
	{"千", "干", "于"},
	{"乌", "鸟"},
	{"刀", "力"},
	{"己", "已"},
	{"晴", "睛", "清", "请", "情"},
	{"拔", "拨"},
	{"喝", "渴"},
	{"坡", "披", "破", "被"},
	{"底", "低", "纸"},
	{"辛", "幸"},
	{"进", "近"},
	{"园", "圆", "元"},
	{"兔", "免"},
	{"蓝", "篮"},
	{"王", "主", "玉"},
	{"风", "凤"},
	{"晒", "洒"},
}

// pinyinCandCap 是每目标字候选干扰的统一上限（近形→同韵→近音优先截断）。
// 统一容量使参数空间成为规整的「目标字 × 组合」矩阵，index 低位轮换目标字——
// 小批量（langgen -n 20）即可覆盖多个不同汉字而非只在首字内换干扰。
const pinyinCandCap = 64

// genPinyinChar 是「拼音选字」单选母题：题干给带调拼音，选出对应汉字。
// 干扰 = 近形/同韵/近音三类（严格排除同音字——同音即第二正确答案，绝不入选项）；
// 校验器独立重判：从题干重提拼音，用字表复核答案读音与「干扰非同音」。
type genPinyinChar struct {
	entry registry.Entry
	spec  map[string]any
	tab   *PinyinChars
	cands [][]string // 每目标字的候选干扰（近形→同韵→近音，稳定序，去同音去重，截断至 cap）
	cats  [][]string // cands 平行类别（error_bindings 落槽位类别）
	size  int        // = 目标字数 × C(cap,3)
}

// newPinyinCharGen 构造；字表低于节选下限即错误（fail-closed）。
func newPinyinCharGen(tab *PinyinChars) (Generator, error) {
	if tab == nil || len(tab.Entries) < minPinyinChars {
		return nil, fmt.Errorf("拼音字表未装载或低于下限 %d（fail-closed）", minPinyinChars)
	}
	shapeOf := map[string][]string{}
	for _, grp := range shapeGroups {
		for _, ch := range grp {
			if _, ok := tab.ByChar[ch]; !ok {
				continue
			}
			for _, other := range grp {
				if other == ch {
					continue
				}
				if py, ok := tab.ByChar[other]; ok && py != tab.ByChar[ch] {
					shapeOf[ch] = append(shapeOf[ch], other)
				}
			}
		}
	}
	g := &genPinyinChar{
		entry: registry.Entry{ID: TplPinyinChar, Version: "1.0.0"},
		tab:   tab,
	}
	for _, e := range tab.Entries {
		added := map[string]bool{}
		homophone := map[string]bool{}
		for _, c := range tab.Correct[e.Pinyin] {
			homophone[c] = true // 正确集（含目标字自身）：同音即第二正确答案，排除
		}
		var cand, cat []string
		add := func(chars []string, kind string) {
			for _, c := range chars {
				if len(cand) >= pinyinCandCap {
					return
				}
				if homophone[c] || added[c] {
					continue
				}
				added[c] = true
				cand = append(cand, c)
				cat = append(cat, kind)
			}
		}
		add(shapeOf[e.Char], distrShape)
		add(tab.Finals[tab.FinalOf[e.Char]], distrRhyme)
		add(charsOf(tab), distrSound)
		if len(cand) < pinyinCandCap {
			return nil, fmt.Errorf("目标字 %q 候选干扰 %d 不足 %d（字表/正确集异常）",
				e.Char, len(cand), pinyinCandCap)
		}
		g.cands = append(g.cands, cand)
		g.cats = append(g.cats, cat)
	}
	g.size = len(tab.Entries) * comb3(pinyinCandCap)
	g.spec = map[string]any{
		"objective":    "按带调拼音辨认对应汉字（确定性档：char_in_corpus + 拼音复核）",
		"slots":        []string{"pinyin", "target_char", "distractor_1..3", "correct_index"},
		"variation":    []string{"target ∈ pinyin 字表", "distractor ∈ 近形∪同韵∪近音，且 ∉ 同音正确集"},
		"presentation": "四选一：选出读音是指定拼音的字",
		"answer":       "correct_index(1..4)，按 index 确定性轮换",
		"distractors":  "近形/同韵/近音三类，同音字绝对排除（同音=第二正确答案）",
	}
	return g, nil
}

// charsOf 字表稳定序全量字（近音兜底取材域）。
func charsOf(tab *PinyinChars) []string {
	out := make([]string, len(tab.Entries))
	for i, e := range tab.Entries {
		out[i] = e.Char
	}
	return out
}

// Entry/Spec/Size 实现 Generator。
func (g *genPinyinChar) Entry() registry.Entry { return g.entry }
func (g *genPinyinChar) Spec() map[string]any  { return g.spec }
func (g *genPinyinChar) Size() int             { return g.size }

// Instance 纯索引函数：index = 目标字序 + 组合序×目标字数（低位轮换目标字，
// 小批量即可覆盖多字），同 index 同输出。
func (g *genPinyinChar) Instance(index int) (*Instance, error) {
	if index < 0 || index >= g.size {
		return nil, fmt.Errorf("index %d 超出参数空间 [0,%d)", index, g.size)
	}
	n := len(g.tab.Entries)
	ti := index % n
	comb := index / n
	e := g.tab.Entries[ti]
	triple, err := combKth3Idx(len(g.cands[ti]), comb)
	if err != nil {
		return nil, err
	}
	distr := []struct {
		ch   string
		kind string
	}{{g.cands[ti][triple[0]], g.cats[ti][triple[0]]},
		{g.cands[ti][triple[1]], g.cats[ti][triple[1]]},
		{g.cands[ti][triple[2]], g.cats[ti][triple[2]]}}

	correctIdx := index%4 + 1 // 答案位确定性轮换
	opts := make([]string, 4)
	errBinds := make([]map[string]any, 0, 3)
	j := 0
	for pos := 1; pos <= 4; pos++ {
		if pos == correctIdx {
			opts[pos-1] = e.Char
			continue
		}
		opts[pos-1] = distr[j].ch
		errBinds = append(errBinds, map[string]any{
			"slot":          fmt.Sprintf("distractor_%d", pos),
			"error_type_id": distr[j].kind,
		})
		j++
	}
	inst := &Instance{
		TemplateID: g.entry.ID,
		Locale:     "zh-Hans",
		Objective:  objective("lang.pinyin.to_char"),
		InteractionRef: map[string]any{
			"interaction_id":     "single_choice",
			"interaction_params": map[string]any{"options": toAnySlice(opts)},
		},
		Content: map[string]any{
			"stem":      fmt.Sprintf("选字：读音是「%s」的字是哪一个？", e.Pinyin),
			"blocks":    scBlocks(fmt.Sprintf("选字：读音是「%s」的字是哪一个？", e.Pinyin), opts),
			"options":   toAnySlice(opts),
			"answer":    correctIdx,
			"target":    e.Char,
			"pinyin":    e.Pinyin,
			"source_id": g.tab.SourceID,
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
			"params": map[string]any{"normalized": map[string]any{"index": index, "target_index": ti, "comb": comb, "correct_index": correctIdx}},
		},
	}
	return inst, nil
}

// comb2 返回 C(n,2)。
func comb2(n int) int {
	if n < 2 {
		return 0
	}
	return n * (n - 1) / 2
}

// combKth3Idx 字典序第 k 个三元组合的规范 unrank（返回元素下标；与 char_recognize
// 的演示实现独立双写：本实现按 C(n-1-v, r-1) 块大小逐位分解，全域无越界——
// 性质测试覆盖：全组合枚举互异且下标严格递增）。
func combKth3Idx(n, k int) ([3]int, error) {
	if k < 0 || k >= comb3(n) {
		return [3]int{}, fmt.Errorf("组合序 %d 超出 [0,%d)", k, comb3(n))
	}
	var out [3]int
	idx, start := k, 0
	for r := 3; r >= 1; r-- {
		for v := start; v <= n-r; v++ {
			var block int
			switch r {
			case 3:
				block = comb2(n - 1 - v)
			case 2:
				block = n - 1 - v
			default:
				block = 1
			}
			if idx < block {
				out[3-r] = v
				start = v + 1
				break
			}
			idx -= block
		}
	}
	return out, nil
}

// ── 独立校验器（防共谋）：不读生成器内部状态，只看实例六块 + 字表重判地面真值。──

// 拼音选字族哨兵错误（errors.Is 判别类别；细节原因用 %w 包裹）。
var (
	ErrPinyinTpl                   = errors.New("subjectlang: 拼音选字母题 id 不符")
	ErrPinyinScorer                = errors.New("subjectlang: 拼音选字评分器非 exact_match")
	ErrPinyinInteraction           = errors.New("subjectlang: 拼音选字交互形态不符")
	ErrPinyinCrossMisalign         = errors.New("subjectlang: 拼音选字 content/scoring 答案不一致")
	ErrPinyinStemMalformed         = errors.New("subjectlang: 拼音选题干缺拼音槽位")
	ErrPinyinOptionInvalid         = errors.New("subjectlang: 拼音选字选项形态非法")
	ErrPinyinAnswerNotInCorpus     = errors.New("subjectlang: 答案字不在拼音字表")
	ErrPinyinAnswerMismatch        = errors.New("subjectlang: 答案位与目标字不符")
	ErrPinyinReadingMismatch       = errors.New("subjectlang: 答案字表读音与题干拼音不符")
	ErrPinyinDistractorNotInCorpus = errors.New("subjectlang: 干扰字不在拼音字表")
	ErrPinyinHomophoneDistractor   = errors.New("subjectlang: 干扰字与题干同音（正确集泄漏）")
)

// PinyinCharValidator 拼音选字独立校验器：
//  1. 模板 id / 评分器 exact_match / 交互 single_choice；
//  2. content 答案与 scoring 答案一致；
//  3. 题干可反解析出拼音槽位（「」内）；
//  4. 四选项互异，答案位 1..4 且选项==content.target；
//  5. 答案字在字表且其表内读音==题干拼音（拼音复核，独立于生成器）；
//  6. 每个干扰字在字表且读音≠题干拼音（同音泄漏即拒）。
type PinyinCharValidator struct{ tab *PinyinChars }

// NewPinyinCharValidator 构造；字表为空即错误（fail-closed 落构造期）。
func NewPinyinCharValidator(tab *PinyinChars) (*PinyinCharValidator, error) {
	if tab == nil || len(tab.Entries) == 0 {
		return nil, fmt.Errorf("拼音字表未装载（判定域为零）")
	}
	return &PinyinCharValidator{tab: tab}, nil
}

// Validate 独立重判一个拼音选字实例；nil 即错误。
func (v *PinyinCharValidator) Validate(inst *Instance) error {
	if inst == nil {
		return fmt.Errorf("实例为 nil")
	}
	if inst.TemplateID != TplPinyinChar {
		return fmt.Errorf("%w: %q", ErrPinyinTpl, inst.TemplateID)
	}
	if got := inst.InteractionRef["interaction_id"]; got != "single_choice" {
		return fmt.Errorf("%w: %v", ErrPinyinInteraction, got)
	}
	if got := inst.ScoringRef["scorer_id"]; got != "exact_match" {
		return fmt.Errorf("%w: %v", ErrPinyinScorer, got)
	}
	answer, _ := inst.Content["answer"].(int)
	scorerAnswer, _ := inst.ScoringRef["scorer_params"].(map[string]any)["answer"].(int)
	if answer != scorerAnswer {
		return fmt.Errorf("%w: content=%d scoring=%d", ErrPinyinCrossMisalign, answer, scorerAnswer)
	}
	stem, _ := inst.Content["stem"].(string)
	stemPinyin := extractBracketed(stem)
	if stemPinyin == "" {
		return fmt.Errorf("%w: %q", ErrPinyinStemMalformed, stem)
	}
	opts, err := optionStrings(inst)
	if err != nil {
		return fmt.Errorf("%w: %v", ErrPinyinOptionInvalid, err)
	}
	if len(opts) != 4 || opts[0] == "" {
		return fmt.Errorf("%w: 选项数 %d", ErrPinyinOptionInvalid, len(opts))
	}
	distinct := map[string]bool{}
	for _, o := range opts {
		if distinct[o] {
			return fmt.Errorf("%w: 选项 %q 重复", ErrPinyinOptionInvalid, o)
		}
		distinct[o] = true
	}
	if answer < 1 || answer > 4 {
		return fmt.Errorf("%w: 答案位 %d 越界", ErrPinyinOptionInvalid, answer)
	}
	target, _ := inst.Content["target"].(string)
	if opts[answer-1] != target {
		return fmt.Errorf("%w: 答案位选项 %q ≠ target %q", ErrPinyinAnswerMismatch, opts[answer-1], target)
	}
	tablePy, ok := v.tab.ByChar[target]
	if !ok {
		return fmt.Errorf("%w: %q", ErrPinyinAnswerNotInCorpus, target)
	}
	if tablePy != stemPinyin {
		return fmt.Errorf("%w: %q 表内读音 %q ≠ 题干 %q", ErrPinyinReadingMismatch, target, tablePy, stemPinyin)
	}
	for _, o := range opts {
		if o == target {
			continue
		}
		py, ok := v.tab.ByChar[o]
		if !ok {
			return fmt.Errorf("%w: %q", ErrPinyinDistractorNotInCorpus, o)
		}
		if py == stemPinyin {
			return fmt.Errorf("%w: %q 与题干拼音 %q 同音", ErrPinyinHomophoneDistractor, o, stemPinyin)
		}
	}
	return nil
}

// extractBracketed 独立反解析题干拼音（与生成器模板双写：校验器不引用生成器
// 格式代码，按文本槽位定位——生成器改模板而校验器未同步时立即暴露）。
func extractBracketed(stem string) string {
	i := strings.Index(stem, "「")
	j := strings.Index(stem, "」")
	if i < 0 || j <= i {
		return ""
	}
	return stem[i+len("「") : j]
}

// optionStrings 提取 content.options 为字符串切片。
func optionStrings(inst *Instance) ([]string, error) {
	anyOpts, _ := inst.Content["options"].([]any)
	opts := make([]string, 0, len(anyOpts))
	for _, o := range anyOpts {
		s, ok := o.(string)
		if !ok {
			return nil, fmt.Errorf("非字符串选项 %v", o)
		}
		opts = append(opts, s)
	}
	return opts, nil
}
