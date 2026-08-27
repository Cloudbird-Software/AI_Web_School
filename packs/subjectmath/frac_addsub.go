package subjectmath

import (
	"fmt"
	"math/rand/v2"
)

// frac_addsub.go —— 母题⑤ 同分母分数加减（单选）。
//
// 语义基准：src/packs subject-math 函数库 fraction_simplify（结果必化简至
// 最简形式）+ add/sub 同分母路径；KP 取 content/seeds/math_kp_3-4.yaml
// 已登记节点 math.nal.fraction.simple_add_sub（同分母分数加减法）。
//
// 参数空间（结构互异的来源，可枚举互异参数点）：
//   d ∈ [3, 12]（课标口径分母上限 12）
//   op=add：有序分子对 (a,b)，a,b ∈ [1,d-1] 且 a+b < d（和保持真分数）
//   op=sub：有序分子对 (a,b)，a > b（差恒为真分数）
//   空间规模 = Σ C(d-1,2) × 2 = 220 + 220 = 440。
//   加法为**有序对**且呈现不交换顺序——(1,8)+(2,8) 与 (2,8)+(1,8) 是两个
//   独立参数点、两份不同 content（若呈现期随机换序会造成跨点摘要折叠）。
//
// 干扰项规则（确定性、按经典迷思绑定错误类型，R-Q-06）：
//   分母也相加：(a+b)/(2d)                  err.add.frac.denominator-added
//   运算符号看错：|a-b|/d 或 (a+b)/d        err.add.frac.sign-slip
//   分子差一：(a+b±1)/d                     err.add.frac.off-by-one
//   候选做值级去重（约分后等值即撞车），不足由全空间兜底序列补齐——
//   凑不齐视为生成缺陷报错上浮，不放行坏实例。
//
// 与 validateFracAddSub 零代码共享：验证器只读题干文本，重提两分数、判
// 同分母、独立重算分子和/差并化简，再逐选项重判——禁止生成器自证。

const idFracAddSub = "tpl-sm-frac-addsub-sc"

// minFracAddSpace 空间下限：30 实例 × 13 倍余量。
const minFracAddSpace = 400

type fracAddParam struct {
	op   byte // '+' / '-'
	d    int64
	a, b int64 // add: a+b<d；sub: a>b
}

type fracAddGen struct {
	tplMeta
	space []fracAddParam
}

func newFracAddGen() (*fracAddGen, error) {
	space := make([]fracAddParam, 0, 512)
	for d := int64(3); d <= 12; d++ {
		for a := int64(1); a < d; a++ {
			for b := int64(1); b < d; b++ {
				if a+b < d {
					space = append(space, fracAddParam{'+', d, a, b})
				}
				if a > b {
					space = append(space, fracAddParam{'-', d, a, b})
				}
			}
		}
	}
	if len(space) < minFracAddSpace {
		return nil, fmt.Errorf("母题 %s 参数空间过小：%d（阈值 %d）", idFracAddSub, len(space), minFracAddSpace)
	}
	g := &fracAddGen{
		space: space,
		tplMeta: newTplMeta(idFracAddSub, "1.0.0", map[string]any{
			"template_id": idFracAddSub,
			"objective": map[string]any{
				"kp_code":         "math.nal.fraction.simple_add_sub",
				"cognitive_level": "apply",
				"gradeband":       "M",
			},
			"slots": map[string]any{
				"f1": map[string]any{"type": "fraction", "difficulty_relevant": true},
				"f2": map[string]any{"type": "fraction", "difficulty_relevant": true},
				"op": map[string]any{"type": "enum", "values": []any{"add", "sub"}, "difficulty_relevant": true},
			},
			"variation_axes": map[string]any{
				"operation":    "加/减双向（加法含同分子对称点）",
				"operand_grid": "d∈[3,12] 的同分母真分数对，440 可枚举参数点",
				"reduction":    "结果含已最简与需化简（如 1/4+1/4=1/2）两类",
			},
			"presentation":   map[string]any{"blocks_kind": "text", "stem_variants": 3},
			"answer_program": map[string]any{"expression": "simplify((a±b)/d)", "returns": "fraction"},
			"distractor_rules": []any{
				map[string]any{"rule_type": "deterministic", "expression": "(a+b)/(2d)", "error_type_id": "err.add.frac.denominator-added"},
				map[string]any{"rule_type": "deterministic", "expression": "|a-b|/d 或 (a+b)/d", "error_type_id": "err.add.frac.sign-slip"},
				map[string]any{"rule_type": "deterministic", "expression": "(a±b±1)/d", "error_type_id": "err.add.frac.off-by-one"},
			},
		}),
	}
	if err := Register(g); err != nil {
		return nil, fmt.Errorf("注册母题 %s 失败: %w", idFracAddSub, err)
	}
	return g, nil
}

// fracAddStemTemplates 全符号题干（算符显式入题干——验证器据此判 op；
// 不做情境变体以避免无算符题干的运算符歧义）。{x}/{y} 保序渲染，不换序。
var fracAddStemTemplates = []struct{ tpl string }{
	{"计算：{x} ＋ {y} = ？"},
	{"口算：{x} ＋ {y} = ？"},
	{"先计算，再把结果化成最简分数：{x} ＋ {y} = ？"},
}

var fracSubStemTemplates = []struct{ tpl string }{
	{"计算：{x} － {y} = ？"},
	{"口算：{x} － {y} = ？"},
	{"先计算，再把结果化成最简分数：{x} － {y} = ？"},
}

func (g *fracAddGen) Size() int { return len(g.space) }

// distractorPool 迷思候选 + 全空间确定性兜底（同 frac_compare 口径）：
// 全部经「最简真分数 + 值级不撞正解/互不撞车」过滤。
func (g *fracAddGen) distractorPool(p fracAddParam, ansKey string) []namedOption {
	addValid := func(pool []namedOption, f fracVal, et string) []namedOption {
		if !f.properReduced() {
			return pool
		}
		if f.key() == ansKey {
			return pool
		}
		for _, q := range pool {
			if q.label == f.display() {
				return pool
			}
		}
		return append(pool, namedOption{label: f.display(), errorType: et})
	}

	pool := make([]namedOption, 0, 8)
	rawNum := p.a + p.b
	if p.op == '-' {
		rawNum = p.a - p.b
	}

	// 分母也参与运算的迷思：和/差放到了 (2d) 上
	pool = addValid(pool, fracVal{rawNum, 2 * p.d}, "err.add.frac.denominator-added")

	// 运算符号看错：加法算成减法 / 减法算成加法
	if p.op == '+' && p.a != p.b {
		slip := p.a - p.b
		if slip < 0 {
			slip = -slip
		}
		pool = addValid(pool, fracVal{slip, p.d}, "err.add.frac.sign-slip")
	}
	if p.op == '-' && p.a+p.b < p.d {
		pool = addValid(pool, fracVal{p.a + p.b, p.d}, "err.add.frac.sign-slip")
	}

	// 分子差一迷思（先加后减的确定性顺序）
	if p.op == '+' {
		pool = addValid(pool, fracVal{rawNum + 1, p.d}, "err.add.frac.off-by-one")
		pool = addValid(pool, fracVal{rawNum - 1, p.d}, "err.add.frac.off-by-one")
	} else {
		pool = addValid(pool, fracVal{rawNum + 1, p.d}, "err.add.frac.off-by-one")
		pool = addValid(pool, fracVal{p.a - p.b - 1, p.d}, "err.add.frac.off-by-one")
	}

	// 全空间兜底：升序枚举最简真分数（确定性顺序，不依赖随机）。
	if len(pool) < 3 {
		for d := int64(2); d <= 12 && len(pool) < 3; d++ {
			for n := int64(1); n < d && len(pool) < 3; n++ {
				pool = addValid(pool, fracVal{n, d}, "err.add.frac.filler")
			}
		}
	}
	return pool
}

func (g *fracAddGen) Instance(index int) (*Instance, error) {
	if err := g.checkIndex(index, len(g.space)); err != nil {
		return nil, err
	}
	p := g.space[index]

	inner := rand.New(rand.NewPCG(0x341B7C7D, uint64(index)))
	stemTplIdx := inner.IntN(3)

	// 结果：分子和/差（真分数），display 走 fracVal 规范化（自动约分）。
	var rawNum int64
	if p.op == '+' {
		rawNum = p.a + p.b
	} else {
		rawNum = p.a - p.b
	}
	ansF := fracVal{rawNum, p.d}
	ansLabel := ansF.display()

	pool := g.distractorPool(p, ansF.key())
	perm := inner.Perm(len(pool))
	taken := map[string]bool{ansF.key(): true}
	distractors := make([]namedOption, 0, 3)
	for _, pi := range perm {
		c := pool[pi]
		if taken[c.label] {
			continue
		}
		taken[c.label] = true
		distractors = append(distractors, c)
		if len(distractors) == 3 {
			break
		}
	}
	if len(distractors) < 3 {
		return nil, fmt.Errorf("分数加减干扰项池不足：op=%c %d/%d %c %d/%d",
			p.op, p.a, p.d, p.op, p.b, p.d)
	}

	var stemT struct{ tpl string }
	if p.op == '+' {
		stemT = fracAddStemTemplates[stemTplIdx]
	} else {
		stemT = fracSubStemTemplates[stemTplIdx]
	}
	x := fmtInt(p.a) + "/" + fmtInt(p.d) // 操作数按原样渲染（同分母练习题操作数不约分）
	y := fmtInt(p.b) + "/" + fmtInt(p.d)
	stem := replaceOnce(replaceOnce(stemT.tpl, "{x}", x), "{y}", y)

	opts := make([]namedOption, 0, numOptionCnt)
	opts = append(opts, namedOption{label: ansLabel})
	opts = append(opts, distractors...)
	inner.Shuffle(len(opts), func(i, j int) { opts[i], opts[j] = opts[j], opts[i] })

	letters := make([]string, len(opts))
	answerIdx := -1
	blocks := make([]any, 0, len(opts)+1)
	blocks = append(blocks, textBlock(stemT.tpl, stem))
	for i, o := range opts {
		let := string(rune('A' + i))
		letters[i] = let
		blocks = append(blocks, textBlock(let+". {"+let+"}", let+". "+o.label))
		if o.label == ansLabel {
			answerIdx = i
		}
	}
	if answerIdx < 0 {
		return nil, fmt.Errorf("正解项丢失：op=%c d=%d index=%d", p.op, p.d, index)
	}
	ansLet := letters[answerIdx]

	reduced := ""
	if ansLabel != fmtInt(rawNum)+"/"+fmtInt(p.d) {
		reduced = " = " + ansLabel
	}
	opSym := "＋"
	if p.op == '-' {
		opSym = "－"
	}
	expl := "同分母分数" + map[byte]string{'+': "相加", '-': "相减"}[p.op] +
		"，分母不变，分子" + map[byte]string{'+': "相加", '-': "相减"}[p.op] +
		"：" + x + " " + opSym + " " + y + " = " + fmtInt(rawNum) + "/" + fmtInt(p.d) +
		reduced + "，所以选 " + ansLet + "。"

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
		TemplateID:        idFracAddSub,
		TemplateVersionID: g.versionID(),
		Locale:            "zh-CN",
		Objective:         objective("math.nal.fraction.simple_add_sub", "apply", "M"),
		InteractionRef: map[string]any{
			"interaction_id":     "single_choice",
			"interaction_params": map[string]any{"options": optionParams},
		},
		Content: map[string]any{
			"blocks":      blocks,
			"answer":      map[string]any{"letter": ansLet, "value": ansLabel},
			"explanation": expl,
		},
		ScoringRef: map[string]any{
			"scorer_id":     "exact_match",
			"scorer_params": map[string]any{"answer": ansLabel},
		},
		ErrorBindings: errBinds,
		Lineage: lineage(g.versionID(), map[string]any{
			"index": index,
			"op":    string(p.op),
			"f1":    x,
			"f2":    y,
		}),
	}
	return inst, nil
}
