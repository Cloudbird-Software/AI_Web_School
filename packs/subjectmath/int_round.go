package subjectmath

import (
	"fmt"
	"math/rand/v2"
)

// int_round.go —— 母题④ 整数四舍五入求近似数（数值填空 numeric_blank）。
//
// 语义基准：src/packs subject-math 函数库 round_half_up（half-up 非银行家舍入，
// 小学「四舍五入」约定）；KP 取 content/seeds/math_kp_3-4.yaml 已登记节点
// math.nal.integer.round（整数的近似与估算）。
//
// 参数空间（结构互异的来源，可枚举互异参数点）：
//   v ∈ [100, 9999]（三/四位数）
//   位档 k ∈ {十位, 百位, 千位}（十位/百位要求 v≥100，千位要求 v≥1000）
//   空间规模 = 9900 + 9900 + 9000 = 28800。
//   生成器侧用「数位字符串」实现 half-up；validators.go 的独立验证器用纯
//   算术（余数×2 对半单位）重算——两条实现路径互为防共谋对照。
//
// 干扰语义：数值填空无选项，错误绑定按 conv 母题口径（blank mismatch，
// answer-value-neq-implies-error，R-Q-06）。

const idIntRound = "tpl-sm-int-round-nb"

// roundPlaces 位档表：k 为保留到 10^k 位的幂次（生成器侧口径）。
var roundPlaces = []struct {
	k      int
	label  string // 题面位档词（与 validators.go 验证器自持副本字面一致）
	thresh int64  // 半单位（half-up 的进位阈值）：10^k / 2
	minV   int64  // 该位档的参数值下限
}{
	{k: 1, label: "十位", thresh: 5, minV: 100},
	{k: 2, label: "百位", thresh: 50, minV: 100},
	{k: 3, label: "千位", thresh: 500, minV: 1000},
}

type roundParam struct {
	v  int64
	pi int // 位档下标（入 roundPlaces）
}

type roundGen struct {
	tplMeta
	space []roundParam
}

func newRoundGen() (*roundGen, error) {
	space := make([]roundParam, 0, 28800)
	for pi := range roundPlaces {
		p := &roundPlaces[pi]
		for v := p.minV; v <= 9999; v++ {
			space = append(space, roundParam{v, pi})
		}
	}
	if len(space) < 20000 {
		return nil, fmt.Errorf("母题 %s 参数空间过小：%d", idIntRound, len(space))
	}
	g := &roundGen{
		space: space,
		tplMeta: newTplMeta(idIntRound, "1.0.0", map[string]any{
			"template_id": idIntRound,
			"objective": map[string]any{
				"kp_code":         "math.nal.integer.round",
				"cognitive_level": "understand",
				"gradeband":       "M",
			},
			"slots": map[string]any{
				"value": map[string]any{"type": "int", "range": "[100,9999]", "difficulty_relevant": true},
				"place": map[string]any{"type": "enum", "values": []any{"十位", "百位", "千位"}, "difficulty_relevant": true},
			},
			"variation_axes": map[string]any{
				"place_grid": "3 位档 × 9900/9000 个数值 = 28800 可枚举参数点",
				"boundary":   "含整百/整千（尾数 0）、half-up 半单位（尾数恰 50/500）、连续进位（9999→10000）三类边界",
			},
			"presentation":   map[string]any{"blocks_kind": "text", "stem_variants": 3},
			"answer_program": map[string]any{"expression": "round_half_up(v, place)", "returns": "number"},
			"distractor_rules": []any{
				map[string]any{"rule_type": "blank_mismatch", "error_type_id": "err.round.nearest.mismatch"},
			},
		}),
	}
	if err := Register(g); err != nil {
		return nil, fmt.Errorf("注册母题 %s 失败: %w", idIntRound, err)
	}
	return g, nil
}

var roundStemTemplates = []struct{ prefix string }{
	{""},
	{"求近似数："},
	{"按要求取近似数："},
}

// roundCoreTpl 题干核心句式（与 validators.go 验证器自持正则口径一致：
// {v} ≈ （  ）（四舍五入到X位），{place} 位档词自带「位」字）。
const roundCoreTpl = "{v} ≈ （  ）（四舍五入到{place}）"

// roundHalfUpByDigits 生成器侧 half-up：数位字符串路径（与验证器的算术路径
// 互为独立实现——同值必同果，任何一条实现错都不至于两边陪跑同错）。
// 尾数必须按 k 位**补齐前导零**后取首位：半单位判定看的是恰在保留位下方
// 那一位数字（105→百位：尾数 5 应视作 "05"，首位 0 < 5，舍去）。
func roundHalfUpByDigits(v int64, k int) int64 {
	u := pow10(k)
	rem := v % u
	digits := fmtInt(rem)
	for len(digits) < k {
		digits = "0" + digits
	}
	if digits[0] >= '5' {
		return v - rem + u
	}
	return v - rem
}

func (g *roundGen) Size() int { return len(g.space) }

func (g *roundGen) Instance(index int) (*Instance, error) {
	if err := g.checkIndex(index, len(g.space)); err != nil {
		return nil, err
	}
	p := g.space[index]
	place := roundPlaces[p.pi]

	inner := rand.New(rand.NewPCG(0x5DEECE6D, uint64(index)))
	stemT := roundStemTemplates[inner.IntN(len(roundStemTemplates))]

	vStr := fmtInt(p.v)
	ans := roundHalfUpByDigits(p.v, place.k)
	rem := p.v % pow10(place.k)
	remStr := fmtInt(rem)
	headStr := fmtInt(rem / pow10(place.k-1)) // 省略部分的首位数字
	cmp := "，不满 5，直接舍去"
	if rem >= place.thresh {
		cmp = "，满 5，向前一位进 1"
	}

	tpl := stemT.prefix + roundCoreTpl
	rendered := replaceOnce(tpl, "{v}", vStr)
	rendered = replaceOnce(rendered, "{place}", place.label)

	expl := vStr + " 四舍五入到" + place.label + "：省略的尾数是 " + remStr +
		"，尾数首位 " + headStr + cmp + "，得 " + fmtInt(ans) + "。"

	inst := &Instance{
		TemplateID:        idIntRound,
		TemplateVersionID: g.versionID(),
		Locale:            "zh-CN",
		Objective:         objective("math.nal.integer.round", "understand", "M"),
		InteractionRef: map[string]any{
			"interaction_id":     "numeric_blank",
			"interaction_params": map[string]any{"blank_ids": []any{"b1"}},
		},
		Content: map[string]any{
			"blocks":      []any{textBlock(tpl, rendered)},
			"answer":      map[string]any{"blank_id": "b1", "value": fmtInt(ans)},
			"explanation": expl,
		},
		ScoringRef: map[string]any{
			"scorer_id":     "exact_match",
			"scorer_params": map[string]any{"answer": fmtInt(ans), "blank_id": "b1"},
		},
		ErrorBindings: []map[string]any{{
			"subject":         "blank:b1",
			"error_type_id":   "err.round.nearest.mismatch",
			"confidence_rule": "answer-value-neq-implies-error",
		}},
		Lineage: lineage(g.versionID(), map[string]any{
			"index":       index,
			"value":       p.v,
			"place":       place.label,
			"stem_prefix": stemT.prefix,
		}),
	}
	return inst, nil
}
