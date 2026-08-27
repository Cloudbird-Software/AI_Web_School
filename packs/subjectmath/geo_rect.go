package subjectmath

import (
	"fmt"
	"math/rand/v2"
)

// geo_rect.go —— 母题⑩ 长方形/正方形的周长与面积（数值填空 numeric_blank）。
//
// 语义基准：周长公式 C=(长+宽)×2、C=边长×4；面积公式 S=长×宽、S=边长×边长
// （课标 3-4 图形与几何「周长/面积」）；KP 按形态落 seeds 已登记节点：
//   form=rect-perim → math.geo.rect.perimeter
//   form=rect-area  → math.geo.rect.area
//   form=sq-perim   → math.geo.square.perimeter
//   form=sq-area    → math.geo.square.area
//
// 参数空间（结构互异的来源，可枚举互异参数点）：
//   rect 形态：长 a ∈ [2,30]，宽 b ∈ [1,a-1]（a>b，正方形归 square 形态，
//   两形态空间不相交），435 对 × {周长,面积} 2 量 = 870；
//   square 形态：边长 c ∈ [2,30]，29 × 2 量 = 58；
//   合计 928 可枚举参数点。同一 (a,b) 的周长/面积是两道题（量词与公式都不同），
//   content 不折叠——题干嵌入量词与单位，全域扫描断言背书。
//
// 与 validateGeoRect 零代码共享：验证器从题干重提形状/量词/边长、独立套公式
// 重算，并复核性质断言（长>宽、矩形周长必为偶数、正方形周长必被 4 整除、
// 正方形面积必为完全平方数）——禁止生成器自证。

const idGeoRect = "tpl-sm-geo-nb"

type geoParam struct {
	form string // "rect-perim" / "rect-area" / "sq-perim" / "sq-area"
	a, b int64  // rect: 长与宽（a>b）；square: a=边长，b=0
}

type geoGen struct {
	tplMeta
	space []geoParam
}

// geoFormKP 形态 → 已登记 KP 节点。
var geoFormKP = map[string]string{
	"rect-perim": "math.geo.rect.perimeter",
	"rect-area":  "math.geo.rect.area",
	"sq-perim":   "math.geo.square.perimeter",
	"sq-area":    "math.geo.square.area",
}

func newGeoGen() (*geoGen, error) {
	space := make([]geoParam, 0, 1024)
	for a := int64(2); a <= 30; a++ {
		for b := int64(1); b < a; b++ {
			space = append(space, geoParam{"rect-perim", a, b})
			space = append(space, geoParam{"rect-area", a, b})
		}
	}
	for c := int64(2); c <= 30; c++ {
		space = append(space, geoParam{"sq-perim", c, 0})
		space = append(space, geoParam{"sq-area", c, 0})
	}
	if len(space) < 512 {
		return nil, fmt.Errorf("母题 %s 参数空间过小：%d", idGeoRect, len(space))
	}
	g := &geoGen{
		space: space,
		tplMeta: newTplMeta(idGeoRect, "1.0.0", map[string]any{
			"template_id": idGeoRect,
			"objective": map[string]any{
				"kp_code":         "math.geo.{rect|square}.{perimeter|area}", // 实例按形态落具体 kp
				"cognitive_level": "apply",
				"gradeband":       "M",
			},
			"slots": map[string]any{
				"a": map[string]any{"type": "int", "meaning": "长/边长", "difficulty_relevant": true},
				"b": map[string]any{"type": "int", "meaning": "宽（square 形态缺省）", "difficulty_relevant": true},
			},
			"variation_axes": map[string]any{
				"shape":     []any{"rect", "square"},
				"quantity":  []any{"perimeter", "area"},
				"edge_grid": "rect 长>宽 435 对 + square 边长 29 值，× 2 量 = 928 可枚举参数点",
			},
			"presentation":   map[string]any{"blocks_kind": "text", "stem_variants": 3},
			"answer_program": map[string]any{"expression": "(a+b)*2 / a*b / a*4 / a*a", "returns": "number"},
			"distractor_rules": []any{
				map[string]any{"rule_type": "blank_mismatch", "error_type_id": "err.geo.rect.mismatch"},
			},
		}),
	}
	if err := Register(g); err != nil {
		return nil, fmt.Errorf("注册母题 %s 失败: %w", idGeoRect, err)
	}
	return g, nil
}

var geoStemTemplates = []struct{ prefix string }{
	{""},
	{"算一算："},
	{"想一想："},
}

// geoAnswerAnswer 按形态独立套公式（生成器侧）。
func geoAnswer(form string, a, b int64) int64 {
	switch form {
	case "rect-perim":
		return (a + b) * 2
	case "rect-area":
		return a * b
	case "sq-perim":
		return a * 4
	default: // sq-area
		return a * a
	}
}

func (g *geoGen) Size() int { return len(g.space) }

func (g *geoGen) Instance(index int) (*Instance, error) {
	if err := g.checkIndex(index, len(g.space)); err != nil {
		return nil, err
	}
	p := g.space[index]

	inner := rand.New(rand.NewPCG(0x9E3779B1, uint64(index)))
	stemT := geoStemTemplates[inner.IntN(len(geoStemTemplates))]

	var tpl, expl string
	switch p.form {
	case "rect-perim":
		tpl = stemT.prefix + "一个长方形，长 {a} 厘米，宽 {b} 厘米，它的周长是（  ）厘米。"
		expl = "长方形的周长 =（长＋宽）×2 =（" + fmtInt(p.a) + "＋" + fmtInt(p.b) +
			"）×2 = " + fmtInt(p.a+p.b) + "×2 = " + fmtInt(geoAnswer(p.form, p.a, p.b)) + "（厘米）。"
	case "rect-area":
		tpl = stemT.prefix + "一个长方形，长 {a} 厘米，宽 {b} 厘米，它的面积是（  ）平方厘米。"
		expl = "长方形的面积 = 长×宽 = " + fmtInt(p.a) + "×" + fmtInt(p.b) +
			" = " + fmtInt(geoAnswer(p.form, p.a, p.b)) + "（平方厘米）。"
	case "sq-perim":
		tpl = stemT.prefix + "一个正方形，边长 {a} 厘米，它的周长是（  ）厘米。"
		expl = "正方形的周长 = 边长×4 = " + fmtInt(p.a) + "×4 = " +
			fmtInt(geoAnswer(p.form, p.a, p.b)) + "（厘米）。"
	default: // sq-area
		tpl = stemT.prefix + "一个正方形，边长 {a} 厘米，它的面积是（  ）平方厘米。"
		expl = "正方形的面积 = 边长×边长 = " + fmtInt(p.a) + "×" + fmtInt(p.a) +
			" = " + fmtInt(geoAnswer(p.form, p.a, p.b)) + "（平方厘米）。"
	}

	rendered := replaceOnce(replaceOnce(tpl, "{a}", fmtInt(p.a)), "{b}", fmtInt(p.b))
	blocks := []any{textBlock(tpl, rendered)}

	ans := geoAnswer(p.form, p.a, p.b)
	unit := "厘米"
	if p.form == "rect-area" || p.form == "sq-area" {
		unit = "平方厘米"
	}

	inst := &Instance{
		TemplateID:        idGeoRect,
		TemplateVersionID: g.versionID(),
		Locale:            "zh-CN",
		Objective:         objective(geoFormKP[p.form], "apply", "M"),
		InteractionRef: map[string]any{
			"interaction_id":     "numeric_blank",
			"interaction_params": map[string]any{"blank_ids": []any{"b1"}},
		},
		Content: map[string]any{
			"blocks":      blocks,
			"answer":      map[string]any{"blank_id": "b1", "value": fmtInt(ans), "unit": unit},
			"explanation": expl,
		},
		ScoringRef: map[string]any{
			"scorer_id": "exact_match",
			"scorer_params": map[string]any{
				"answer": fmtInt(ans), "unit": unit, "blank_id": "b1",
			},
		},
		ErrorBindings: []map[string]any{{
			"subject":         "blank:b1",
			"error_type_id":   "err.geo.rect.mismatch",
			"confidence_rule": "answer-value-neq-implies-error",
		}},
		Lineage: lineage(g.versionID(), map[string]any{
			"index":       index,
			"form":        p.form,
			"a":           p.a,
			"b":           p.b,
			"stem_prefix": stemT.prefix,
		}),
	}
	return inst, nil
}
