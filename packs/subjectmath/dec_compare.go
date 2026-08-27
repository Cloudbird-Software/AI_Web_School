package subjectmath

import (
	"fmt"
	"math/rand/v2"
	"sort"
)

// dec_compare.go —— 母题⑥ 小数大小比较（单选）。
//
// 语义基准：src/packs subject-math 变量类型 Decimal（规范化：去尾零、值相等
// 判定基于规范化形式——本母题参数空间即构建在该规范化之上）；KP 取
// content/seeds/math_kp_3-4.yaml 已登记节点 math.nal.decimal.compare。
//
// 参数空间（结构互异的来源，可枚举互异参数点）：
//   值域（全部规范化十进制、两两不等值）：
//     一位小数 m/10，m ∈ [1,99] 且 10∤m —— 0.1..9.9 共 90 个；
//     两位小数 m/100，m ∈ [1,199] 且 10∤m —— 0.01..0.99、1.01..1.99 共 180 个
//     （尾零在入表时剔除：0.50 折叠到 0.5 的等值形态绝不入空间）。
//   参数点 = 值升序两两组合：C(270,2) = 36315。
//   尾零剔除 + 值级去重保证「规范化串 ↔ 值」双射——这是 30 实例无折叠的根基
//   （母题③ 单位换算的折叠教训在此前置到空间构造期）。
//
// 干扰项规则（确定性、按经典迷思绑定错误类型）：
//   有效数字邻域：大/小数 mantissa±1          err.cmp.dec.digit-slip
//   尾数位错位互换：末位小数字符对调          err.cmp.dec.digit-confuse
//   兜底：值升序全空间序列填充                err.cmp.dec.filler
//
// 与 validateDecCompare 零代码共享：生成器用交叉相乘定大小，验证器用
// 「补零对齐位数后比整数」——两种精确路径互为防共谋对照。

const idDecCmp = "tpl-sm-dec-cmp-sc"

// decVal 规范化小数：m/10^s，调用方保证 m%10≠0（无尾零）且 m≥1。
type decVal struct {
	m int64
	s int
}

func (d decVal) display() string { return decString(d.m, d.s) }

// smaller 交叉相乘定大小（生成器侧路径；全整数无浮点）。
func (d decVal) smaller(o decVal) bool { return d.m*int64(pow10(o.s)) < o.m*int64(pow10(d.s)) }

// canonical 判断 (m,s) 是否为本母题值域的规范化形态。
func (d decVal) canonical() bool {
	if d.s != 1 && d.s != 2 {
		return false
	}
	max := int64(99)
	if d.s == 2 {
		max = 199
	}
	return d.m >= 1 && d.m <= max && d.m%10 != 0
}

type decParam struct {
	x, y decVal // x < y（构造期定序）
}

type decGen struct {
	tplMeta
	space []decParam
}

func newDecGen() (*decGen, error) {
	var all []decVal
	for m := int64(1); m <= 99; m++ {
		if m%10 != 0 {
			all = append(all, decVal{m, 1})
		}
	}
	for m := int64(1); m <= 199; m++ {
		if m%10 != 0 {
			all = append(all, decVal{m, 2})
		}
	}
	// 值升序全排序（比较器与 decVal.smaller 同口径；值两两不等 → 全序确定）
	sort.Slice(all, func(i, j int) bool { return all[i].smaller(all[j]) })

	space := make([]decParam, 0, len(all)*(len(all)-1)/2)
	for i := 0; i < len(all); i++ {
		for j := i + 1; j < len(all); j++ {
			space = append(space, decParam{all[i], all[j]})
		}
	}
	if len(space) < 30000 {
		return nil, fmt.Errorf("母题 %s 参数空间过小：%d", idDecCmp, len(space))
	}
	g := &decGen{
		space: space,
		tplMeta: newTplMeta(idDecCmp, "1.0.0", map[string]any{
			"template_id": idDecCmp,
			"objective": map[string]any{
				"kp_code":         "math.nal.decimal.compare",
				"cognitive_level": "understand",
				"gradeband":       "M",
			},
			"slots": map[string]any{
				"x": map[string]any{"type": "decimal", "difficulty_relevant": true},
				"y": map[string]any{"type": "decimal", "difficulty_relevant": true},
			},
			"variation_axes": map[string]any{
				"value_universe": "270 个规范小数（一位 90 + 两位 180，尾零剔除后值两两不等）",
				"pairs":          "值升序两两组合 C(270,2)=36315 可枚举参数点",
				"trap":           "含跨整数部分（1.3 与 1.28）与位数陷阱（0.05 与 0.5）结构",
			},
			"presentation":   map[string]any{"blocks_kind": "text", "stem_variants": 3},
			"answer_program": map[string]any{"expression": "argmax_dec(x, y)", "returns": "number"},
			"distractor_rules": []any{
				map[string]any{"rule_type": "deterministic", "expression": "mantissa±1", "error_type_id": "err.cmp.dec.digit-slip"},
				map[string]any{"rule_type": "deterministic", "expression": "swap_last_frac_digit", "error_type_id": "err.cmp.dec.digit-confuse"},
			},
		}),
	}
	if err := Register(g); err != nil {
		return nil, fmt.Errorf("注册母题 %s 失败: %w", idDecCmp, err)
	}
	return g, nil
}

var decStemTemplates = []struct{ tpl string }{
	{"{x} 和 {y} 比较大小：更大的数是（  ）"},
	{"比一比：{x} 与 {y}，把更大的数填在括号里（  ）"},
	{"想一想：{x} 和 {y} 中，（  ）是更大的那个数"},
}

// renderDecStem 填充 {x}/{y}（值级 distinct 的规范串，直排渲染）。
func renderDecStem(tpl string, x, y decVal) string {
	s := replaceOnce(tpl, "{x}", x.display())
	return replaceOnce(s, "{y}", y.display())
}

func (g *decGen) Size() int { return len(g.space) }

// padDisplay 把规范化小数补零渲染到恰好 pad 位小数（保留尾零——「对齐位数」
// 的呈现契约）。与 validators.go 的补零实现互为独立副本：同一输入必须产出
// 同一字符串，任一侧实现错都会在防共谋测试里暴露。
func padDisplay(d decVal, pad int) string {
	scaled := d.m * pow10(pad-d.s)
	unit := pow10(pad)
	fp := fmtInt(scaled % unit)
	for len(fp) < pad {
		fp = "0" + fp
	}
	return fmtInt(scaled/unit) + "." + fp
}

// distractorPool 迷思候选 + 值升序兜底（同 frac 系口径：规范形态 + 值级去重）。
func (g *decGen) distractorPool(big, small decVal) []namedOption {
	addValid := func(pool []namedOption, d decVal, et string) []namedOption {
		if !d.canonical() {
			return pool
		}
		if d.display() == big.display() || d.display() == small.display() {
			return pool
		}
		for _, q := range pool {
			if q.label == d.display() {
				return pool
			}
		}
		return append(pool, namedOption{label: d.display(), errorType: et})
	}

	pool := make([]namedOption, 0, 8)

	// 有效数字邻域迷思：mantissa ±1（先加后减的确定性顺序）
	pool = addValid(pool, decVal{big.m + 1, big.s}, "err.cmp.dec.digit-slip")
	pool = addValid(pool, decVal{big.m - 1, big.s}, "err.cmp.dec.digit-slip")
	pool = addValid(pool, decVal{small.m + 1, small.s}, "err.cmp.dec.digit-slip")
	pool = addValid(pool, decVal{small.m - 1, small.s}, "err.cmp.dec.digit-slip")

	// 位数陷阱迷思：两数末位小数字符对调（构造出的仍是规范小数才入池）
	lastOf := func(d decVal) int64 { return d.m % 10 }
	baseOf := func(d decVal) int64 { return d.m - d.m%10 }
	pool = addValid(pool, decVal{baseOf(big) + lastOf(small), big.s}, "err.cmp.dec.digit-confuse")
	pool = addValid(pool, decVal{baseOf(small) + lastOf(big), small.s}, "err.cmp.dec.digit-confuse")

	// 兜底：值升序枚举本母题值域（确定性顺序，不依赖随机）。
	if len(pool) < 2 {
		for m := int64(1); m <= 199 && len(pool) < 2; m++ {
			if m%10 == 0 {
				continue
			}
			if len(pool) < 2 {
				pool = addValid(pool, decVal{m, 2}, "err.cmp.dec.filler")
			}
			if len(pool) < 2 {
				pool = addValid(pool, decVal{m, 1}, "err.cmp.dec.filler")
			}
		}
	}
	return pool
}

func (g *decGen) Instance(index int) (*Instance, error) {
	if err := g.checkIndex(index, len(g.space)); err != nil {
		return nil, err
	}
	p := g.space[index]

	inner := rand.New(rand.NewPCG(0x1B873593, uint64(index)))
	stemTplIdx := inner.IntN(len(decStemTemplates))
	swap := inner.IntN(2) == 1

	big, small := p.y, p.x // 构造期已定序 x<y
	x, y := p.x, p.y
	if swap {
		x, y = y, x
	}

	ansLabel := big.display()
	pool := g.distractorPool(big, small)
	perm := inner.Perm(len(pool))
	taken := map[string]bool{ansLabel: true, small.display(): true}
	distractors := make([]namedOption, 0, 2)
	for _, pi := range perm {
		c := pool[pi]
		if taken[c.label] {
			continue
		}
		taken[c.label] = true
		distractors = append(distractors, c)
		if len(distractors) == 2 {
			break
		}
	}
	if len(distractors) < 2 {
		return nil, fmt.Errorf("小数比较干扰项池不足：%s / %s", big.display(), small.display())
	}

	opts := make([]namedOption, 0, numOptionCnt)
	opts = append(opts,
		namedOption{label: ansLabel},
		namedOption{label: small.display(), errorType: "err.cmp.dec.reverse-order"})
	opts = append(opts, distractors...)
	inner.Shuffle(len(opts), func(i, j int) { opts[i], opts[j] = opts[j], opts[i] })

	letters := make([]string, len(opts))
	answerIdx := -1
	stemT := decStemTemplates[stemTplIdx]
	blocks := make([]any, 0, len(opts)+1)
	blocks = append(blocks, textBlock(stemT.tpl, renderDecStem(stemT.tpl, x, y)))
	for i, o := range opts {
		let := string(rune('A' + i))
		letters[i] = let
		blocks = append(blocks, textBlock(let+". {"+let+"}", let+". "+o.label))
		if o.label == ansLabel {
			answerIdx = i
		}
	}
	if answerIdx < 0 {
		return nil, fmt.Errorf("正解项丢失：index=%d", index)
	}
	ansLet := letters[answerIdx]

	// 解析用「补零对齐位数」口径（与验证器同口径不同实现：验证器自行补零重算）
	pad := big.s
	if small.s > pad {
		pad = small.s
	}
	bigPad := padDisplay(big, pad)
	smallPad := padDisplay(small, pad)

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
		TemplateID:        idDecCmp,
		TemplateVersionID: g.versionID(),
		Locale:            "zh-CN",
		Objective:         objective("math.nal.decimal.compare", "understand", "M"),
		InteractionRef: map[string]any{
			"interaction_id":     "single_choice",
			"interaction_params": map[string]any{"options": optionParams},
		},
		Content: map[string]any{
			"blocks": blocks,
			"answer": map[string]any{"letter": ansLet, "value": ansLabel},
			"explanation": "比较 " + x.display() + " 与 " + y.display() + "：补零对齐成 " +
				fmtInt(int64(pad)) + " 位小数：" + bigPad + " 与 " + smallPad +
				"，因为 " + bigPad + " > " + smallPad +
				"，所以更大的数是 " + ansLabel + "，选 " + ansLet + "。",
		},
		ScoringRef: map[string]any{
			"scorer_id":     "exact_match",
			"scorer_params": map[string]any{"answer": ansLabel},
		},
		ErrorBindings: errBinds,
		Lineage: lineage(g.versionID(), map[string]any{
			"index": index,
			"x":     p.x.display(),
			"y":     p.y.display(),
		}),
	}
	return inst, nil
}
