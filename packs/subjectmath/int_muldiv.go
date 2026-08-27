package subjectmath

import (
	"fmt"
	"math/rand/v2"
)

// int_muldiv.go —— 母题⑧ 乘除混合运算（数值填空 numeric_blank）。
//
// 语义基准：乘除同级运算从左往右依次计算（课标 3-4 四则运算顺序的同级路径）；
// KP 取 content/seeds/math_kp_3-4.yaml 已登记节点 math.nal.integer.mul_div。
//
// 参数空间（结构互异的来源，可枚举互异参数点；锁定「每步都可口算」的课标结构）：
//   form=MD（先乘后除）：a ∈ [2,20]，b ∈ [2,9]，c ∈ [2,9]，且 c 整除 a×b
//     （中间积 a×b ≤ 180，末步必须整除——不整除即不属本参数空间）；
//   form=DM（先除后乘）：a ∈ [4,99]，b ∈ [2,9]，b 整除 a 且商 k=a÷b ≥ 2，
//     c ∈ [2,9]（商 ≥ 2 排除 ÷b 得 1 的退化题）。
//   两形态不相交（算符次序即形态）；整除性与商约束是母题的难度语义，验证器
//   把它们作为性质断言复核，坏空间必被拒。合计 ≈ 1700 可枚举参数点。
//
// 与 validateIntMulDiv 零代码共享：验证器从题干重提三操作数、按算符次序独立
// 重算、复核整除性与值域——禁止生成器自证。

const idIntMulDiv = "tpl-sm-int-muldiv-nb"

type mulDivParam struct {
	form    byte // 'M' = a×b÷c，'D' = a÷b×c
	a, b, c int64
}

type mulDivGen struct {
	tplMeta
	space []mulDivParam
}

func newMulDivGen() (*mulDivGen, error) {
	space := make([]mulDivParam, 0, 2048)
	// form=MD：a×b÷c，末步整除
	for a := int64(2); a <= 20; a++ {
		for b := int64(2); b <= 9; b++ {
			for c := int64(2); c <= 9; c++ {
				if (a*b)%c == 0 {
					space = append(space, mulDivParam{'M', a, b, c})
				}
			}
		}
	}
	// form=DM：a÷b×c，首步整除且商 ≥ 2
	for b := int64(2); b <= 9; b++ {
		for k := int64(2); k*b <= 99; k++ {
			for c := int64(2); c <= 9; c++ {
				space = append(space, mulDivParam{'D', k * b, b, c})
			}
		}
	}
	if len(space) < 1024 {
		return nil, fmt.Errorf("母题 %s 参数空间过小：%d", idIntMulDiv, len(space))
	}
	g := &mulDivGen{
		space: space,
		tplMeta: newTplMeta(idIntMulDiv, "1.0.0", map[string]any{
			"template_id": idIntMulDiv,
			"objective": map[string]any{
				"kp_code":         "math.nal.integer.mul_div",
				"cognitive_level": "apply",
				"gradeband":       "M",
			},
			"slots": map[string]any{
				"a": map[string]any{"type": "int", "difficulty_relevant": true},
				"b": map[string]any{"type": "int", "difficulty_relevant": true},
				"c": map[string]any{"type": "int", "difficulty_relevant": true},
			},
			"variation_axes": map[string]any{
				"operation_order": []any{"mul_then_div", "div_then_mul"},
				"exactness":       "每步整除（MD: c|a×b；DM: b|a 且商≥2），空间构造期锁定",
			},
			"presentation":   map[string]any{"blocks_kind": "text", "stem_variants": 3},
			"answer_program": map[string]any{"expression": "(a*b)/c 或 (a/b)*c（从左往右）", "returns": "number"},
			"distractor_rules": []any{
				map[string]any{"rule_type": "blank_mismatch", "error_type_id": "err.calc.muldiv.mismatch"},
			},
		}),
	}
	if err := Register(g); err != nil {
		return nil, fmt.Errorf("注册母题 %s 失败: %w", idIntMulDiv, err)
	}
	return g, nil
}

// mulDivStemTemplates 同级运算题干三变体（前缀不含数字——验证器按数字 token
// 数提取操作数的口径依赖这一点）。
var mulDivStemTemplates = []struct{ prefix string }{
	{""},
	{"算一算："},
	{"按要求从左往右依次计算："},
}

func (g *mulDivGen) Size() int { return len(g.space) }

func (g *mulDivGen) Instance(index int) (*Instance, error) {
	if err := g.checkIndex(index, len(g.space)); err != nil {
		return nil, err
	}
	p := g.space[index]

	inner := rand.New(rand.NewPCG(0x85EBCA6B, uint64(index)))
	stemT := mulDivStemTemplates[inner.IntN(len(mulDivStemTemplates))]

	var tpl, expl string
	var ans int64
	if p.form == 'M' {
		mid := p.a * p.b
		ans = mid / p.c
		tpl = stemT.prefix + "{a} × {b} ÷ {c} = （  ）"
		expl = "同级运算从左往右：先算 " + fmtInt(p.a) + " × " + fmtInt(p.b) + "=" +
			fmtInt(mid) + "，再算 " + fmtInt(mid) + " ÷ " + fmtInt(p.c) + "=" + fmtInt(ans) + "。"
	} else {
		k := p.a / p.b
		ans = k * p.c
		tpl = stemT.prefix + "{a} ÷ {b} × {c} = （  ）"
		expl = "同级运算从左往右：先算 " + fmtInt(p.a) + " ÷ " + fmtInt(p.b) + "=" +
			fmtInt(k) + "，再算 " + fmtInt(k) + " × " + fmtInt(p.c) + "=" + fmtInt(ans) + "。"
	}
	rendered := replaceOnce(replaceOnce(replaceOnce(tpl,
		"{a}", fmtInt(p.a)), "{b}", fmtInt(p.b)), "{c}", fmtInt(p.c))
	blocks := []any{textBlock(tpl, rendered)}

	inst := &Instance{
		TemplateID:        idIntMulDiv,
		TemplateVersionID: g.versionID(),
		Locale:            "zh-CN",
		Objective:         objective("math.nal.integer.mul_div", "apply", "M"),
		InteractionRef: map[string]any{
			"interaction_id":     "numeric_blank",
			"interaction_params": map[string]any{"blank_ids": []any{"b1"}},
		},
		Content: map[string]any{
			"blocks":      blocks,
			"answer":      map[string]any{"blank_id": "b1", "value": fmtInt(ans)},
			"explanation": expl,
		},
		ScoringRef: map[string]any{
			"scorer_id":     "exact_match",
			"scorer_params": map[string]any{"answer": fmtInt(ans), "blank_id": "b1"},
		},
		ErrorBindings: []map[string]any{{
			"subject":         "blank:b1",
			"error_type_id":   "err.calc.muldiv.mismatch",
			"confidence_rule": "answer-value-neq-implies-error",
		}},
		Lineage: lineage(g.versionID(), map[string]any{
			"index":       index,
			"form":        string(p.form),
			"a":           p.a,
			"b":           p.b,
			"c":           p.c,
			"stem_prefix": stemT.prefix,
		}),
	}
	return inst, nil
}
