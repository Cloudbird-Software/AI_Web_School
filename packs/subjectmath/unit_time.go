package subjectmath

import (
	"fmt"
	"math/rand/v2"
)

// unit_time.go —— 母题⑨ 时间单位换算（数值填空 numeric_blank）。
//
// 语义基准：60 进制时间量纲（1时=60分、1分=60秒，课标 3-4「时间单位」核心
// 进率）；KP 取 content/seeds/math_kp_3-4.yaml 已登记节点 math.nal.quantity.time。
//
// 参数空间（结构互异的来源；复用母题③ conv 骨架：量纲族表 + 有序单位对 +
// 值域 m/10^s + 规范化折叠防护，但进率为 60 非十的幂——整除约束与十进制
// 渲染路径独立实现）：
//   量纲族 = 时间：秒⁰ → 分¹ → 时²，相邻进率 60；有序对 |Δ级|=1
//   （秒↔分、分↔时双向，排除 时↔秒 ×3600 超纲复合进率与同单位）；
//   数值 v = m/10^s，m ∈ [1,999]，s ∈ {0,1}，s=1 要求 10∤m
//   （否则与某 s=0 参数渲染同一数值串——conv 母题实证过的 content 折叠）；
//   缩小方向（÷60）额外要求 3|m：60=2²·3·5，分子不消去因子 3 则结果非
//   十进制有限小数（时间不存无限小数读法）。
//   规模 = 放大 2 向 × 1899 + 缩小 2 向 × 633 = 5064 可枚举参数点。
//
// 生成器侧渲染走「约分 + 分母 2^a·5^b 判定 + 补幂对齐」路径；validators.go
// 的独立验证器走「长除逐位取商」路径——同值必同串，两条实现互为防共谋对照。
//
// 与 validateTimeConv 零代码共享：验证器持有独立副本的时间量纲表与换算
// 实现，从题干文本解析数值与单位、独立重算再比对。

const idTimeConv = "tpl-sm-time-nb"

// timeUnitsGen 生成器侧时间量纲表（validators.go 另有独立副本，禁共享引用）。
// exp60 = 相对秒的 60 的幂次（「1 该单位 = 60^exp 秒」）。
var timeUnitsGen = []struct {
	label string
	exp60 int
}{
	{"秒", 0},
	{"分", 1},
	{"时", 2},
}

type timeParam struct {
	ui int // from 单位下标（入 timeUnitsGen）
	uo int // to 单位下标
	m  int64
	s  int // 小数位数（0/1）
}

type timeGen struct {
	tplMeta
	space []timeParam
}

func newTimeGen() (*timeGen, error) {
	space := make([]timeParam, 0, 8192)
	for i := range timeUnitsGen {
		for j := range timeUnitsGen {
			delta := timeUnitsGen[i].exp60 - timeUnitsGen[j].exp60
			if delta != 1 && delta != -1 {
				continue // 相邻级双向有序对（同单位/跨两级一律不入空间）
			}
			for m := int64(1); m <= 999; m++ {
				for s := 0; s <= 1; s++ {
					// 规范化折叠防护（conv 教训前置）：s=1 且 10|m 的 (m,s)
					// 与某个 s=0 参数渲染同一数值串，入表即破坏 H-W6-1 单射。
					if s == 1 && m%10 == 0 {
						continue
					}
					// 缩小方向（÷60）：结果必须是十进制有限小数——
					// 60·10^s 的因子 3 必须被分子消去，即 3|m。
					if delta == -1 && m%3 != 0 {
						continue
					}
					space = append(space, timeParam{i, j, m, s})
				}
			}
		}
	}
	if len(space) < 4096 {
		return nil, fmt.Errorf("母题 %s 参数空间过小：%d", idTimeConv, len(space))
	}
	g := &timeGen{
		space: space,
		tplMeta: newTplMeta(idTimeConv, "1.0.0", map[string]any{
			"template_id": idTimeConv,
			"objective": map[string]any{
				"kp_code":         "math.nal.quantity.time",
				"cognitive_level": "apply",
				"gradeband":       "M",
			},
			"slots": map[string]any{
				"value":  map[string]any{"type": "decimal", "difficulty_relevant": true},
				"u_from": map[string]any{"type": "unit", "difficulty_relevant": true},
				"u_to":   map[string]any{"type": "unit", "difficulty_relevant": true},
			},
			"variation_axes": map[string]any{
				"ladder":      "秒⁰→分¹→时²，相邻进率 60，双向有序对",
				"direction":   "放大（×60）/缩小（÷60，要求结果为有限小数）",
				"value_shape": []any{"integer", "one-decimal"},
			},
			"presentation":   map[string]any{"blocks_kind": "text", "stem_variants": 3},
			"answer_program": map[string]any{"expression": "value × 60^(exp60(u_from)-exp60(u_to))", "returns": "number"},
			"distractor_rules": []any{
				map[string]any{"rule_type": "blank_mismatch", "error_type_id": "err.conv.time.mismatch"},
			},
		}),
	}
	if err := Register(g); err != nil {
		return nil, fmt.Errorf("注册母题 %s 失败: %w", idTimeConv, err)
	}
	return g, nil
}

var timeStemTemplates = []struct{ prefix string }{
	{""},
	{"在括号里填上合适的数："},
	{"时间单位换算："},
}

// pow60 60 的 n 次幂（n≥0；本母题 n∈{0,1}，防御性支持更高级差）。
func pow60(n int) int64 {
	p := int64(1)
	for i := 0; i < n; i++ {
		p *= 60
	}
	return p
}

// timeShrinkAnswer 生成器侧缩小方向：v=m/10^s ÷ 60 的最短规范十进制串。
// 路径：约分 → 断言既约分母只含 2/5 因子（有限小数）→ 补幂对齐到
// scale=max(a,b) 后交 decString 出规范串。空间构造已保证 3|m，此处断言
// 是防御性复核（构造缺陷就地失败，不放行坏实例）。
func timeShrinkAnswer(m int64, s int) (string, error) {
	den := int64(60) * pow10(s)
	g := gcdI64(m, den)
	num, d := m/g, den/g
	dOrig := d // 既约分母（2^a·5^b 形，下面循环分解）
	a, b := 0, 0
	for ; d%2 == 0; d /= 2 {
		a++
	}
	for ; d%5 == 0; d /= 5 {
		b++
	}
	if d != 1 {
		return "", fmt.Errorf("时间缩小方向出现非有限小数：%d / %d", m, den)
	}
	scale := a
	if b > scale {
		scale = b
	}
	// 10^scale / 2^a·5^b = 2^(scale-a)·5^(scale-b) 为整数，补幂后整除
	return decString(num*(pow10(scale)/dOrig), scale), nil
}

func (g *timeGen) Size() int { return len(g.space) }

func (g *timeGen) Instance(index int) (*Instance, error) {
	if err := g.checkIndex(index, len(g.space)); err != nil {
		return nil, err
	}
	p := g.space[index]
	fromU, toU := timeUnitsGen[p.ui], timeUnitsGen[p.uo]

	valueStr := decString(p.m, p.s)
	delta := fromU.exp60 - toU.exp60 // 放大为正、缩小为负

	var ans string
	var expl string
	if delta > 0 {
		ans = decString(p.m*pow60(delta), p.s) // ×60^Δ：s≤1 时尾零折叠回整数
		expl = "因为 1" + fromU.label + "=60" + toU.label +
			"，所以 " + valueStr + " × 60=" + ans + "。单位是" + toU.label + "。"
	} else {
		var err error
		if ans, err = timeShrinkAnswer(p.m, p.s); err != nil {
			return nil, err
		}
		expl = "因为 1" + toU.label + "=60" + fromU.label +
			"，所以 " + valueStr + " ÷ 60=" + ans + "。单位是" + toU.label + "。"
	}

	inner := rand.New(rand.NewPCG(0xC2B2AE3D, uint64(index)))
	stemT := timeStemTemplates[inner.IntN(len(timeStemTemplates))]

	tpl := stemT.prefix + "{value} {from} = （  ）{to}"
	rendered := replaceOnce(tpl, "{value}", valueStr)
	rendered = replaceOnce(rendered, "{from}", fromU.label)
	rendered = replaceOnce(rendered, "{to}", toU.label)
	blocks := []any{textBlock(tpl, rendered)}

	inst := &Instance{
		TemplateID:        idTimeConv,
		TemplateVersionID: g.versionID(),
		Locale:            "zh-CN",
		Objective:         objective("math.nal.quantity.time", "apply", "M"),
		InteractionRef: map[string]any{
			"interaction_id":     "numeric_blank",
			"interaction_params": map[string]any{"blank_ids": []any{"b1"}},
		},
		Content: map[string]any{
			"blocks":      blocks,
			"answer":      map[string]any{"blank_id": "b1", "value": ans, "unit": toU.label},
			"explanation": expl,
		},
		ScoringRef: map[string]any{
			"scorer_id": "exact_match",
			"scorer_params": map[string]any{
				"answer": ans, "unit": toU.label, "blank_id": "b1",
			},
		},
		ErrorBindings: []map[string]any{{
			"subject":         "blank:b1",
			"error_type_id":   "err.conv.time.mismatch",
			"confidence_rule": "answer-value-neq-implies-error",
		}},
		Lineage: lineage(g.versionID(), map[string]any{
			"index":       index,
			"from_value":  valueStr,
			"from_unit":   fromU.label,
			"to_unit":     toU.label,
			"stem_prefix": stemT.prefix,
		}),
	}
	return inst, nil
}
