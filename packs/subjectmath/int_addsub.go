package subjectmath

import (
	"fmt"
	"math/rand/v2"
)

// int_addsub.go —— 母题⑦ 整数进位加法/退位减法（数值填空 numeric_blank）。
//
// 语义基准：src/packs subject-math 函数库 add/sub（整数路径）；KP 取
// content/seeds/math_kp_3-4.yaml 已登记节点 math.nal.integer.add_sub。
//
// 参数空间（结构互异的来源，可枚举互异参数点；锁定课标核心的进位/退位结构）：
//   op=add：a ∈ [100,999]，b ∈ [11,99]，且个位 a%10 + b%10 ≥ 10（个位进位）
//   op=sub：a ∈ [100,999]，b ∈ [11,99]，且个位 a%10 < b%10（个位退位）
//   空间规模 = 36450 + 36450 = 72900。
//   进/退位约束是母题的难度语义（无进退位的口算题不属本参数空间）——验证器
//   把该约束作为性质断言复核，坏空间必被拒。
//
// 题干三变体：两条符号式（算符显式）+ 一条情境式（关键词锚点「一共/还剩」）。
// 验证器判 op 的优先级：算符符号 > 关键词锚点，均缺即 shape 拒绝。
//
// 与 validateIntAddSub 零代码共享：验证器从题干重提两操作数、独立重算
// 和/差、复核进/退位性质与值域——禁止生成器自证。

const idIntAddSub = "tpl-sm-int-addsub-nb"

type addSubParam struct {
	op   byte // '+' / '-'
	a, b int64
}

type addSubGen struct {
	tplMeta
	space []addSubParam
}

func newAddSubGen() (*addSubGen, error) {
	space := make([]addSubParam, 0, 76000)
	for a := int64(100); a <= 999; a++ {
		for b := int64(11); b <= 99; b++ {
			if a%10+b%10 >= 10 {
				space = append(space, addSubParam{'+', a, b})
			}
			if a%10 < b%10 {
				space = append(space, addSubParam{'-', a, b})
			}
		}
	}
	if len(space) < 50000 {
		return nil, fmt.Errorf("母题 %s 参数空间过小：%d", idIntAddSub, len(space))
	}
	g := &addSubGen{
		space: space,
		tplMeta: newTplMeta(idIntAddSub, "1.0.0", map[string]any{
			"template_id": idIntAddSub,
			"objective": map[string]any{
				"kp_code":         "math.nal.integer.add_sub",
				"cognitive_level": "apply",
				"gradeband":       "M",
			},
			"slots": map[string]any{
				"a": map[string]any{"type": "int", "range": "[100,999]", "difficulty_relevant": true},
				"b": map[string]any{"type": "int", "range": "[11,99]", "difficulty_relevant": true},
			},
			"variation_axes": map[string]any{
				"operation": "进位加法 / 退位减法（个位结构锁定）",
				"grid":      "三位数 × 两位数满足进退位约束的有序对，72900 可枚举参数点",
			},
			"presentation":   map[string]any{"blocks_kind": "text", "stem_variants": 3},
			"answer_program": map[string]any{"expression": "a + b 或 a - b", "returns": "number"},
			"distractor_rules": []any{
				map[string]any{"rule_type": "blank_mismatch", "error_type_id": "err.calc.addsub.mismatch"},
			},
		}),
	}
	if err := Register(g); err != nil {
		return nil, fmt.Errorf("注册母题 %s 失败: %w", idIntAddSub, err)
	}
	return g, nil
}

// addSubStemTemplates 符号式（算符显式）与情境式（关键词锚点）。
var addSubStemTemplates = []struct{ tpl string }{
	{"{a} ＋ {b} = （  ）"},
	{"算一算：{a} ＋ {b} = （  ）"},
	{"文具店上午卖出 {a} 张贴纸，下午卖出 {b} 张，全天一共卖出（  ）张贴纸。"},
}

var subStemTemplates = []struct{ tpl string }{
	{"{a} － {b} = （  ）"},
	{"算一算：{a} － {b} = （  ）"},
	{"果园里有 {a} 个苹果，摘走了 {b} 个，还剩（  ）个苹果。"},
}

// renderAddSubStem 填充 {a}/{b} 占位符。
func renderAddSubStem(tpl string, a, b int64) string {
	s := replaceOnce(tpl, "{a}", fmtInt(a))
	return replaceOnce(s, "{b}", fmtInt(b))
}

func (g *addSubGen) Size() int { return len(g.space) }

func (g *addSubGen) Instance(index int) (*Instance, error) {
	if err := g.checkIndex(index, len(g.space)); err != nil {
		return nil, err
	}
	p := g.space[index]

	inner := rand.New(rand.NewPCG(0x7F4A7C15, uint64(index)))

	var stemT struct{ tpl string }
	if p.op == '+' {
		stemT = addSubStemTemplates[inner.IntN(len(addSubStemTemplates))]
	} else {
		stemT = subStemTemplates[inner.IntN(len(subStemTemplates))]
	}
	stem := renderAddSubStem(stemT.tpl, p.a, p.b)
	blocks := []any{textBlock(stemT.tpl, stem)}

	var ans int64
	var expl string
	if p.op == '+' {
		ans = p.a + p.b
		expl = fmtInt(p.a) + " ＋ " + fmtInt(p.b) + "：个位 " + fmtInt(p.a%10) + "＋" +
			fmtInt(p.b%10) + "=" + fmtInt(p.a%10+p.b%10) + "，满十向十位进 1，得 " + fmtInt(ans) + "。"
	} else {
		ans = p.a - p.b
		expl = fmtInt(p.a) + " － " + fmtInt(p.b) + "：个位 " + fmtInt(p.a%10) + " 减 " +
			fmtInt(p.b%10) + " 不够减，向十位借 1 再减，得 " + fmtInt(ans) + "。"
	}

	inst := &Instance{
		TemplateID:        idIntAddSub,
		TemplateVersionID: g.versionID(),
		Locale:            "zh-CN",
		Objective:         objective("math.nal.integer.add_sub", "apply", "M"),
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
			"error_type_id":   "err.calc.addsub.mismatch",
			"confidence_rule": "answer-value-neq-implies-error",
		}},
		Lineage: lineage(g.versionID(), map[string]any{
			"index": index,
			"op":    string(p.op),
			"a":     p.a,
			"b":     p.b,
		}),
	}
	return inst, nil
}
