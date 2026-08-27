package subjectmath

import (
	"fmt"
	"math/rand/v2"
)

// unit_convert.go —— 母题③单位换算（数值填空 numeric_blank）。
//
// 参数空间（结构互异的来源）：
//   量纲族 ∈ {长度, 质量, 人民币}；同族内允许的单位对 = 进率差 |Δ|∈[1,3]
//   的有序对（覆盖 3-4 年级课标核心进率：km↔m、m↔dm、kg↔g、t↔kg、元↔角…，
//   排除 km↔mm 这类超纲复合进率与 g↔t 跨级跳）；
//   数值 v = m/10^s，m ∈ [1,999]，s ∈ {0,1}（整数与小数一位两种形态）。
//   规模 ≈ 28 个单位对方向 × 1998 个数值 ≈ 5.6 万互异参数点。
//
// 与 validateUnitConvert 零代码共享：验证器持有**独立副本**的量纲表与换算
// 实现（重复常量表是纪律要求——防生成表出错时验证器陪跑同错），从题干文本
// 解析数值与单位、独立重算再比对。答案串必须是最短规范十进制（decString 口径）。

const idUnitConv = "tpl-sm-unit-nb"

type unitDef struct {
	label string // 中文单位名（题面呈现口径）
	exp10 int    // 相对族基单位的 10 的幂次
}

type unitFamily struct {
	name     string
	kpCode   string
	baseUnit string
	units    []unitDef
}

// unitFamiliesGen 生成器侧的量纲表（validators.go 另有独立副本，禁共享引用）。
var unitFamiliesGen = []unitFamily{
	{"length", "math.nal.quantity.length", "米", []unitDef{{"毫米", 0}, {"厘米", 1}, {"分米", 2}, {"米", 3}, {"千米", 6}}},
	{"mass", "math.nal.quantity.mass", "克", []unitDef{{"克", 0}, {"千克", 3}, {"吨", 6}}},
	{"money", "math.nal.quantity.money", "分", []unitDef{{"分", 0}, {"角", 1}, {"元", 2}}},
}

type convParam struct {
	fam int
	ui  int // from 单位下标（入 family.units）
	uo  int // to 单位下标
	m   int64
	s   int // 小数位数（0/1）
}

type convGen struct {
	tplMeta
	space []convParam
}

func newConvGen() (*convGen, error) {
	space := make([]convParam, 0, 64*2000)
	for fi := 0; fi < len(unitFamiliesGen); fi++ {
		fam := &unitFamiliesGen[fi]
		for i := range fam.units {
			for j := range fam.units {
				delta := fam.units[i].exp10 - fam.units[j].exp10
				if delta == 0 || delta < -3 || delta > 3 {
					continue // 同族异向且进率差受限（课标口径）
				}
				for m := int64(1); m <= 999; m++ {
					for s := 0; s <= 1; s++ {
						// 规范化折叠：s=1 且尾数为 0 的 (m,s) 与某个 s=0 参数
						// 渲染出同一数值串（如 (350,1)→"35"≡(35,0)）——入表即造
						// content 折叠破坏 H-W6-1 单射，必须剔除（全空间扫描实证过）。
						if s == 1 && m%10 == 0 {
							continue
						}
						space = append(space, convParam{fi, i, j, m, s})
					}
				}
			}
		}
	}
	if len(space) < 1024 {
		return nil, fmt.Errorf("母题 %s 参数空间过小：%d", idUnitConv, len(space))
	}
	g := &convGen{
		space: space,
		tplMeta: newTplMeta(idUnitConv, "1.0.0", map[string]any{
			"template_id": idUnitConv,
			"objective": map[string]any{
				"kp_code":         "math.nal.quantity.{length|mass|money}", // 实例按量纲族落具体 kp
				"cognitive_level": "apply",
				"gradeband":       "M",
			},
			"slots": map[string]any{
				"value":  map[string]any{"type": "decimal", "difficulty_relevant": true},
				"u_from": map[string]any{"type": "unit", "difficulty_relevant": true},
				"u_to":   map[string]any{"type": "unit", "difficulty_relevant": true},
			},
			"variation_axes": map[string]any{
				"family":      []any{"length", "mass", "money"},
				"direction":   "放大/缩小双向有序对，|Δ|∈[1,3]",
				"value_shape": []any{"integer", "one-decimal"},
			},
			"presentation":   map[string]any{"blocks_kind": "text", "stem_variants": 3},
			"answer_program": map[string]any{"expression": "value * 10^(exp(u_to)-exp(u_from))", "returns": "number"},
			"distractor_rules": []any{
				map[string]any{"rule_type": "blank_mismatch", "error_type_id": "err.conv.unit.mismatch"},
			},
		}),
	}
	if err := Register(g); err != nil {
		return nil, fmt.Errorf("注册母题 %s 失败: %w", idUnitConv, err)
	}
	return g, nil
}

var convStemTemplates = []struct{ prefix string }{
	{""},
	{"在括号里填上合适的数："},
	{"单位换算："},
}

// convertForStem 生成器侧换算：value=m/10^s × 10^Δ 的规范化十进制字符串，
// Δ = exp(from) − exp(to)（exp 是「1 该单位 = 多少基单位」的幂次：
// 大单位往小单位换，数变大；反之为小数）。
func convertForStem(m int64, s, delta int) string {
	if delta >= s {
		return fmtInt(m * pow10(delta-s))
	}
	return decString(m, s-delta)
}

func (g *convGen) Size() int { return len(g.space) }

func (g *convGen) Instance(index int) (*Instance, error) {
	if err := g.checkIndex(index, len(g.space)); err != nil {
		return nil, err
	}
	p := g.space[index]
	fam := &unitFamiliesGen[p.fam]
	fromU, toU := fam.units[p.ui], fam.units[p.uo]

	valueStr := decString(p.m, p.s)
	convDelta := fromU.exp10 - toU.exp10 // 放大为正、缩小为负
	ans := convertForStem(p.m, p.s, convDelta)

	inner := rand.New(rand.NewPCG(0x9E3779B9, uint64(index)))
	stemT := convStemTemplates[inner.IntN(len(convStemTemplates))]

	coreTpl := "{value} {from} = （  ）{to}"
	tpl := stemT.prefix + coreTpl
	rendered := replaceOnce(tpl, "{value}", valueStr)
	rendered = replaceOnce(rendered, "{from}", fromU.label)
	rendered = replaceOnce(rendered, "{to}", toU.label)
	blocks := []any{textBlock(tpl, rendered)}

	var expl string
	if convDelta > 0 {
		rate := fmtInt(pow10(convDelta)) // 大→小：1{from}={rate}{to}
		expl = "因为 1" + fromU.label + "=" + rate + toU.label +
			"，所以 " + valueStr + " × " + rate + "=" + ans + "。单位是" + toU.label + "。"
	} else {
		rate := fmtInt(pow10(-convDelta)) // 小→大：1{to}={rate}{from}
		expl = "因为 1" + toU.label + "=" + rate + fromU.label +
			"，所以 " + valueStr + " ÷ " + rate + "=" + ans + "。单位是" + toU.label + "。"
	}

	inst := &Instance{
		TemplateID:        idUnitConv,
		TemplateVersionID: g.versionID(),
		Locale:            "zh-CN",
		Objective:         objective(fam.kpCode, "apply", "M"),
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
			"error_type_id":   "err.conv.unit.mismatch",
			"confidence_rule": "answer-value-neq-implies-error",
		}},
		Lineage: lineage(g.versionID(), map[string]any{
			"index":       index,
			"family":      fam.name,
			"from_value":  valueStr,
			"from_unit":   fromU.label,
			"to_unit":     toU.label,
			"stem_prefix": stemT.prefix,
		}),
	}
	return inst, nil
}
