package subjectmath

import (
	"fmt"
	"math/rand/v2"
	"strings"
)

// int_mul.go —— 母题①整数乘法（单选）。
//
// 参数空间（结构互异的来源，可枚举互异参数点）：
//   a ∈ [102, 989]（三位数被乘数）
//   b ∈ [2, 9]（一位数乘数；排除 0/1 退化题）
//   空间规模 = 888 × 8 = 7104。
// 题干呈现 3 种变体（直接式/倍数语义/情境应用），由索引派生随机流挑选——
// 同参数点在不同变体下也是不同 content，进一步拉开结构差异。
//
// 干扰项规则（确定性表达式，绑定错误类型，R-Q-06 干扰项即错误映射）：
//   r1 多乘一个：a×(b+1)     err.calc.mul.extra-factor
//   r2 少乘一个：a×(b-1)     err.calc.mul.less-factor
//   r3 操作数加一：(a+1)×b   err.calc.mul.off-by-one-operand
//   r4 数位颠倒：rev(a)×b    err.calc.mul.digit-swap
// 规则域保证候选为正整数；候选与正解的碰撞、候选互撞在装配时过滤，
// 过滤不足（理论不可达）返回错误而非放行坏实例。
//
// 与 validators.go 的 validateIntMul 零代码共享：验证器只看实例文本，
// 自行提取操作数、重算乘积再逐项比对——禁止生成器自证。

const (
	idIntMul     = "tpl-sm-int-mul-sc"
	numOptionCnt = 4 // 单选题固定四个选项（A-D）
)

// intMulGen 母题①：嵌入 tplMeta 获得 Entry/Spec/versionID 能力。
type intMulGen struct {
	tplMeta
	space [][2]int64 // 每个索引一组 (a,b)；构造期展开
}

func newIntMulGen() (*intMulGen, error) {
	space := make([][2]int64, 0, 888*8)
	for a := int64(102); a <= 989; a++ {
		for b := int64(2); b <= 9; b++ {
			space = append(space, [2]int64{a, b})
		}
	}
	g := &intMulGen{
		space: space,
		tplMeta: newTplMeta(idIntMul, "1.0.0", map[string]any{
			"template_id": idIntMul,
			"objective": map[string]any{
				"kp_code":         "math.nal.integer.mul",
				"cognitive_level": "apply",
				"gradeband":       "M",
			},
			"slots": map[string]any{
				"a": map[string]any{"type": "int", "range": "[102,989]", "difficulty_relevant": true},
				"b": map[string]any{"type": "int", "range": "[2,9]", "difficulty_relevant": true},
			},
			"variation_axes": map[string]any{
				"operands_grid": "888×8 可枚举参数点",
				"presentation":  "3 种题干变体（直接式/倍数语义/情境应用）",
			},
			"presentation":   map[string]any{"blocks_kind": "text", "stem_variants": 3},
			"answer_program": map[string]any{"expression": "a * b", "returns": "number"},
			"distractor_rules": []any{
				map[string]any{"rule_type": "deterministic", "expression": "a*(b+1)", "error_type_id": "err.calc.mul.extra-factor"},
				map[string]any{"rule_type": "deterministic", "expression": "a*(b-1)", "error_type_id": "err.calc.mul.less-factor"},
				map[string]any{"rule_type": "deterministic", "expression": "(a+1)*b", "error_type_id": "err.calc.mul.off-by-one-operand"},
				map[string]any{"rule_type": "deterministic", "expression": "reverse(a)*b", "error_type_id": "err.calc.mul.digit-swap"},
			},
		}),
	}
	if err := Register(g); err != nil {
		return nil, fmt.Errorf("注册母题 %s 失败: %w", idIntMul, err)
	}
	return g, nil
}

func (g *intMulGen) Size() int { return len(g.space) }

// revDigits 十进制数位倒置（纯函数；回文数情形由候选池规则替换兜底）。
func revDigits(a int64) int64 {
	var r int64
	for a > 0 {
		r = r*10 + a%10
		a /= 10
	}
	return r
}

// 题干三变体：template 为占位形态（契约 blocks.template 栏），rendered 由
// renderStem 填充；explainTail 提供变体专属的解析收尾。
var mulStemTemplates = []struct {
	tpl         string
	explainTail string
}{
	{tpl: "{a} × {b} = ？", explainTail: ""},
	{tpl: "算一算：{b} 的 {a} 倍是多少？", explainTail: "（即求 {a} 个 {b} 相加的和）"},
	{tpl: "水果店运来 {a} 箱苹果，每箱 {b} 千克。苹果一共重多少千克？", explainTail: "（箱数×每箱千克数=总千克数）"},
}

// renderStem 把 {a}/{b} 占位符替换为实参（恒等渲染，渲染即内容的一部分）。
func renderStem(tpl string, a, b int64) string {
	s := strings.ReplaceAll(tpl, "{a}", fmtInt(a))
	return strings.ReplaceAll(s, "{b}", fmtInt(b))
}

func (g *intMulGen) Instance(index int) (*Instance, error) {
	if err := g.checkIndex(index, len(g.space)); err != nil {
		return nil, err
	}
	p := g.space[index]
	a, b := p[0], p[1]

	inner := rand.New(rand.NewPCG(0x6D2B79F5, uint64(index))) // 索引派生流：呈现随机性
	variant := inner.IntN(len(mulStemTemplates))

	product := a * b

	// 干扰项候选池（带错误类型出处）；洗牌后顺序取标签互异的前三个。
	type cand struct {
		label string
		rule  string
	}
	pool := []cand{
		{fmtInt(a * (b + 1)), "err.calc.mul.extra-factor"},
		{fmtInt(a * (b - 1)), "err.calc.mul.less-factor"},
		{fmtInt((a + 1) * b), "err.calc.mul.off-by-one-operand"},
		func() cand {
			if revDigits(a) != a {
				return cand{fmtInt(revDigits(a) * b), "err.calc.mul.digit-swap"}
			}
			return cand{fmtInt((a - 1) * b), "err.calc.mul.off-by-one-operand"}
		}(),
	}
	perm := inner.Perm(len(pool))
	productLabel := fmtInt(product)
	distractors := make([]namedOption, 0, 3)
	taken := map[string]bool{productLabel: true}
	for _, pi := range perm {
		c := pool[pi]
		if taken[c.label] {
			continue
		}
		taken[c.label] = true
		distractors = append(distractors, namedOption{label: c.label, errorType: c.rule})
		if len(distractors) == 3 {
			break
		}
	}
	if len(distractors) < 3 {
		return nil, fmt.Errorf("干扰项池不足：a=%d b=%d 去重后仅 %d 个", a, b, len(distractors))
	}

	opts := append([]namedOption{{label: productLabel}}, distractors...)
	inner.Shuffle(len(opts), func(i, j int) { opts[i], opts[j] = opts[j], opts[i] })

	letters := make([]string, len(opts))
	answerIdx := -1
	stem := mulStemTemplates[variant]
	blocks := make([]any, 0, len(opts)+1)
	blocks = append(blocks, textBlock(stem.tpl, renderStem(stem.tpl, a, b)))
	for i, o := range opts {
		let := string(rune('A' + i))
		letters[i] = let
		blocks = append(blocks, textBlock(let+". {"+let+"}", let+". "+o.label))
		if o.errorType == "" {
			answerIdx = i
		}
	}
	if answerIdx < 0 {
		return nil, fmt.Errorf("正解项丢失：a=%d b=%d", a, b)
	}
	ansLet := letters[answerIdx]

	optionParams := make([]any, 0, len(opts))
	errBinds := make([]map[string]any, 0, len(opts)-1)
	for i, o := range opts {
		optionParams = append(optionParams, map[string]any{"id": letters[i], "label": o.label})
		if o.errorType != "" {
			errBinds = append(errBinds, map[string]any{
				"subject":         "option:" + letters[i],
				"error_type_id":   o.errorType,
				"confidence_rule": "selected-option-equals-subject",
			})
		}
	}

	inst := &Instance{
		TemplateID:        idIntMul,
		TemplateVersionID: g.versionID(),
		Locale:            "zh-CN",
		Objective:         objective("math.nal.integer.mul", "apply", "M"),
		InteractionRef: map[string]any{
			"interaction_id":     "single_choice",
			"interaction_params": map[string]any{"options": optionParams},
		},
		Content: map[string]any{
			"blocks": blocks,
			"answer": map[string]any{"letter": ansLet, "value": productLabel},
			"explanation": "因为 " + fmtInt(a) + " × " + fmtInt(b) + " = " + productLabel +
				renderStem(stem.explainTail, a, b) + "，所以选 " + ansLet + "。",
		},
		ScoringRef: map[string]any{
			"scorer_id":     "exact_match",
			"scorer_params": map[string]any{"answer": productLabel},
		},
		ErrorBindings: errBinds,
		Lineage: lineage(g.versionID(), map[string]any{
			"index": index, "a": a, "b": b, "stem_variant": variant,
		}),
	}
	return inst, nil
}
