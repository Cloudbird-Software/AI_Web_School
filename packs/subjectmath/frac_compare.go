package subjectmath

import (
	"fmt"
	"math/rand/v2"
)

// frac_compare.go —— 母题②分数比较大小（单选）。
//
// 三种**结构形态**的变式轴（不是单纯数值扰动——题干语义与干扰项构造随形态切换）：
//   form=D 同分母异分子：分母相同，比分子；
//   form=N 同分子异分母：分子相同，比分母（反直觉点，易错高发）；
//   form=X 异分母异分子：需通分或交叉相乘。
//
// 参数空间（全部真分数、显示必为最简形式；分母上限 12 为课标口径）：
//   D: 同分母最简分子对 + N: 同分子互质分母对 + X: 异分母异分子非等值对
//   合计 ≈ 九百可枚举互异参数点 —— 30 实例仅占 ~3%，预留 15 倍余量。
//
// 干扰项规则（确定性、按经典迷思绑定错误类型）：
//   mediant：（n1+n2)/(d1+d2) 分子加分子分母加分母误区  err.cmp.frac.mediant
//   只看分子：正确答案分子±1 的邻域分数                 err.cmp.frac.numerator-only
//   分母混淆：(n_small / d_big) 型错位                 err.cmp.frac.denominator-confuse
// 候选做「值级」去重（约分后等值也算撞车——多解选项是坏题），不足即报错。
//
// 与 validateFracCompare 零代码共享：验证器从题干文本提取两分数、独立交叉
// 相乘定大小、逐个选项重判——禁止生成器自证。

const (
	idFracCmp = "tpl-sm-frac-cmp-sc"
	// minFracSpace 空间下限：W6 出口需 30 实例/母题，16 倍余量起步。
	minFracSpace = 480
)

type fracVal struct{ n, d int64 }

// key 值级规范键：约分到最简后 n/d（值相等的分数共享同一键）。
func (f fracVal) key() string {
	g := gcdI64(f.n, f.d)
	return fmtInt(f.n/g) + "/" + fmtInt(f.d/g)
}

// display 展示串：生成器侧恒输出最简形式。
func (f fracVal) display() string {
	g := gcdI64(f.n, f.d)
	return fmtInt(f.n/g) + "/" + fmtInt(f.d/g)
}

// smaller 判断 f 是否严格小于 o（独立交叉相乘，全整数无浮点）。
func (f fracVal) smaller(o fracVal) bool { return f.n*o.d < o.n*f.d }

// properReduced 真分数且最简（验证器同样有此判据；此处用于空间预过滤）。
func (f fracVal) properReduced() bool {
	return f.d > 1 && f.n >= 1 && f.n < f.d && gcdI64(f.n, f.d) == 1
}

type fracParam struct {
	form byte // 'D' / 'N' / 'X'
	f1,
	f2 fracVal
}

type fracGen struct {
	tplMeta
	space []fracParam
}

func newFracGen() (*fracGen, error) {
	space := make([]fracParam, 0, 4096)

	// 最简真分数全集（d∈[2,12]）
	var all []fracVal
	for d := int64(2); d <= 12; d++ {
		for n := int64(1); n < d; n++ {
			f := fracVal{n, d}
			if f.properReduced() {
				all = append(all, f)
			}
		}
	}

	// form=D：同分母，两最简分子组成一对
	for d := int64(4); d <= 12; d++ {
		var nums []int64
		for n := int64(1); n < d; n++ {
			if gcdI64(n, d) == 1 {
				nums = append(nums, n)
			}
		}
		for i := 0; i < len(nums); i++ {
			for j := i + 1; j < len(nums); j++ {
				space = append(space, fracParam{'D', fracVal{nums[i], d}, fracVal{nums[j], d}})
			}
		}
	}

	// form=N：同分子，两互质分母组成一对（排除已在 D 出现的同分母对）
	for n := int64(2); n <= 6; n++ {
		var dens []int64
		for d := n + 1; d <= 12; d++ {
			if gcdI64(n, d) == 1 {
				dens = append(dens, d)
			}
		}
		for i := 0; i < len(dens); i++ {
			for j := i + 1; j < len(dens); j++ {
				space = append(space, fracParam{'N', fracVal{n, dens[i]}, fracVal{n, dens[j]}})
			}
		}
	}

	// form=X：异分母异分子，值不等（字典序去重，D/N 已覆盖的对自然不重复入表，
	// 这里按“至少一个维度不同”放宽为排除同分母与同分子）
	for i := 0; i < len(all); i++ {
		for j := i + 1; j < len(all); j++ {
			a, b := all[i], all[j]
			if a.d == b.d || a.n == b.n || a.key() == b.key() {
				continue
			}
			space = append(space, fracParam{'X', a, b})
		}
	}

	if len(space) < minFracSpace {
		return nil, fmt.Errorf("母题 %s 参数空间过小：%d（阈值 %d）", idFracCmp, len(space), minFracSpace)
	}
	g := &fracGen{
		space: space,
		tplMeta: newTplMeta(idFracCmp, "1.0.0", map[string]any{
			"template_id": idFracCmp,
			"objective": map[string]any{
				"kp_code":         "math.nal.fraction.compare",
				"cognitive_level": "understand",
				"gradeband":       "M",
			},
			"slots": map[string]any{
				"f1": map[string]any{"type": "fraction", "difficulty_relevant": true},
				"f2": map[string]any{"type": "fraction", "difficulty_relevant": true},
			},
			"variation_axes": map[string]any{
				"structural_forms": []any{"same_denominator", "same_numerator", "cross"},
				"operand_pairs":    "最简真分数对（d≤12）",
			},
			"presentation":   map[string]any{"blocks_kind": "text", "stem_variants": 3},
			"answer_program": map[string]any{"expression": "argmax(f1, f2)", "returns": "number"},
			"distractor_rules": []any{
				map[string]any{"rule_type": "deterministic", "expression": "(n1+n2)/(d1+d2)", "error_type_id": "err.cmp.frac.mediant"},
				map[string]any{"rule_type": "deterministic", "expression": "(n_max±1)/d_max", "error_type_id": "err.cmp.frac.numerator-only"},
				map[string]any{"rule_type": "deterministic", "expression": "n_min/d_max", "error_type_id": "err.cmp.frac.denominator-confuse"},
			},
		}),
	}
	if err := Register(g); err != nil {
		return nil, fmt.Errorf("注册母题 %s 失败: %w", idFracCmp, err)
	}
	return g, nil
}

var fracStemTemplates = []struct {
	tpl string
}{
	{"{x} 和 {y} 比较大小：更大的分数是（  ）"},
	{"比一比：{x} 与 {y}，谁更大？把更大的填在括号里（  ）"},
	{"想一想：分数 {x} 和 {y} 中，（  ）是更大的那一个"},
}

// renderFracStem 填充 {x}/{y} 占位符。
func renderFracStem(tpl string, x, y fracVal) string {
	s := replaceOnce(tpl, "{x}", x.display())
	return replaceOnce(s, "{y}", y.display())
}

func replaceOnce(s, old, new string) string {
	i := indexOf(s, old)
	if i < 0 {
		return s
	}
	return s[:i] + new + s[i+len(old):]
}

func indexOf(s, sub string) int {
	for i := 0; i+len(sub) <= len(s); i++ {
		if s[i:i+len(sub)] == sub {
			return i
		}
	}
	return -1
}

func (g *fracGen) Size() int { return len(g.space) }

func (g *fracGen) Instance(index int) (*Instance, error) {
	if err := g.checkIndex(index, len(g.space)); err != nil {
		return nil, err
	}
	p := g.space[index]

	inner := rand.New(rand.NewPCG(0x2545F491, uint64(index)))
	stemTplIdx := inner.IntN(len(fracStemTemplates))
	swap := inner.IntN(2) == 1

	big, small := p.f1, p.f2
	if p.f1.smaller(p.f2) {
		big, small = p.f2, p.f1
	}
	f1, f2 := p.f1, p.f2
	if swap {
		f1, f2 = f2, f1
	}

	ansLabel := big.display()
	pool := g.distractorPool(big, small)
	perm := inner.Perm(len(pool))
	taken := map[string]bool{big.key(): true, small.key(): true}
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
		return nil, fmt.Errorf("分数比较干扰项池不足：form=%c %s/%s", p.form, p.f1.display(), p.f2.display())
	}

	opts := make([]namedOption, 0, numOptionCnt)
	opts = append(opts,
		namedOption{label: ansLabel},
		namedOption{label: small.display(), errorType: "err.cmp.frac.reverse-order"})
	opts = append(opts, distractors...)
	inner.Shuffle(len(opts), func(i, j int) { opts[i], opts[j] = opts[j], opts[i] })

	letters := make([]string, len(opts))
	answerIdx := -1
	stemT := fracStemTemplates[stemTplIdx]
	blocks := make([]any, 0, len(opts)+1)
	blocks = append(blocks, textBlock(stemT.tpl, renderFracStem(stemT.tpl, f1, f2)))
	for i, o := range opts {
		let := string(rune('A' + i))
		letters[i] = let
		blocks = append(blocks, textBlock(let+". {"+let+"}", let+". "+o.label))
		if o.label == ansLabel {
			answerIdx = i
		}
	}
	if answerIdx < 0 {
		return nil, fmt.Errorf("正解项丢失：form=%c index=%d", p.form, index)
	}
	ansLet := letters[answerIdx]
	x := f1.n * f2.d
	y := f2.n * f1.d
	relation := ">"
	if y > x {
		relation = "<"
	}

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
		TemplateID:        idFracCmp,
		TemplateVersionID: g.versionID(),
		Locale:            "zh-CN",
		Objective:         objective("math.nal.fraction.compare", "understand", "M"),
		InteractionRef: map[string]any{
			"interaction_id":     "single_choice",
			"interaction_params": map[string]any{"options": optionParams},
		},
		Content: map[string]any{
			"blocks": blocks,
			"answer": map[string]any{"letter": ansLet, "value": big.display()},
			"explanation": "比较 " + f1.display() + " 与 " + f2.display() + "：" +
				fmtInt(f1.n) + "×" + fmtInt(f2.d) + "=" + fmtInt(x) +
				"，" + fmtInt(f2.n) + "×" + fmtInt(f1.d) + "=" + fmtInt(y) +
				"，因为 " + fmtInt(x) + relation + fmtInt(y) +
				"，所以更大的分数是 " + big.display() + "，选 " + ansLet + "。",
		},
		ScoringRef: map[string]any{
			"scorer_id":     "exact_match",
			"scorer_params": map[string]any{"answer": big.display()},
		},
		ErrorBindings: errBinds,
		Lineage: lineage(g.versionID(), map[string]any{
			"index": index,
			"form":  string(p.form),
			"f1":    p.f1.display(),
			"f2":    p.f2.display(),
		}),
	}
	return inst, nil
}

// distractorPool 构造候选池：经典迷思确定性派生 + 有效性与值级去重过滤。
// 迷思池之后追加一份**全空间确定性兜底序列**（d、n 双升序的最简真分数表），
// 保证任何合法参数对都能凑出 2 个互异干扰项——凑不出视为生成缺陷，
// 由装配层报错上浮，不放行坏实例。
func (g *fracGen) distractorPool(big, small fracVal) []namedOption {
	addValid := func(pool []namedOption, f fracVal, et string) []namedOption {
		if !f.properReduced() {
			return pool
		}
		k := f.key()
		if k == big.key() || k == small.key() {
			return pool
		}
		for _, p := range pool {
			if p.label == f.display() {
				return pool
			}
		}
		return append(pool, namedOption{label: f.display(), errorType: et})
	}

	pool := make([]namedOption, 0, 8)

	// mediant 迷思：分子加分子、分母加分母
	pool = addValid(pool, fracVal{big.n + small.n, big.d + small.d}, "err.cmp.frac.mediant")

	// 只看分子迷思：大分数分子 ±1 的有效邻域（先加后减的确定性顺序）
	pool = addValid(pool, fracVal{big.n + 1, big.d}, "err.cmp.frac.numerator-only")
	pool = addValid(pool, fracVal{big.n - 1, big.d}, "err.cmp.frac.numerator-only")

	// 分母混淆迷思：两分数的分子/分母错位组合
	pool = addValid(pool, fracVal{small.n, big.d}, "err.cmp.frac.denominator-confuse")
	pool = addValid(pool, fracVal{big.n, small.d}, "err.cmp.frac.denominator-confuse")

	// 分子邻域（小分数侧）与分母平移邻域
	pool = addValid(pool, fracVal{small.n + 1, small.d}, "err.cmp.frac.numerator-only")
	pool = addValid(pool, fracVal{small.n - 1, small.d}, "err.cmp.frac.numerator-only")
	pool = addValid(pool, fracVal{big.n, big.d + 1}, "err.cmp.frac.denominator-confuse")
	pool = addValid(pool, fracVal{small.n, small.d + 1}, "err.cmp.frac.denominator-confuse")

	// 全空间兜底：升序枚举最简真分数（确定性顺序，不依赖随机）。
	if len(pool) < 2 {
		for d := int64(2); d <= 12 && len(pool) < 2; d++ {
			for n := int64(1); n < d && len(pool) < 2; n++ {
				pool = addValid(pool, fracVal{n, d}, "err.cmp.frac.filler")
			}
		}
	}
	return pool
}
