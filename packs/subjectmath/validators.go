package subjectmath

import (
	"errors"
	"fmt"
	"regexp"
	"strconv"
	"strings"
)

// validators.go —— 母题级确定性 validator（校验门里 validator 类语义的
// 学科包实现侧母题版）。
//
// 独立性纪律（禁生成器自证）：
//   1. 只消费实例的**已发布形态文本**（content.blocks 渲染串、answer、
//      scoring_ref）——从不读取 lineage.params，不看生成器内部状态；
//   2. 从题干渲染串**重新提取**操作数/单位/分数并重算答案；
//   3. 本文件持有自己的量纲表副本与比较算术——生成表错了验证器不陪跑同错；
//   4. 除正解重算外还做性质断言：选项唯一可解性、格式规范性（最短十进制、
//      最简分数）、解析文本自洽。
//
// 错误按四类哨兵包装，batch 层据此输出「拒绝原因分布」（W6 S2 产能报告口径）。

var (
	// ErrShapeInvalid 结构形态问题：缺字段/块序非法/交互类型不符等
	ErrShapeInvalid = errors.New("shape-invalid")
	// ErrAnswerMismatch 重算答案与声明答案不一致（含答案字母指错）
	ErrAnswerMismatch = errors.New("answer-mismatch")
	// ErrFormatViolation 格式规范破坏：非最短十进制/未化简分数/超域数值
	ErrFormatViolation = errors.New("format-violation")
	// ErrConsistencyBroken 性质断言失败：多解选项/解析缺要素/跨量纲等
	ErrConsistencyBroken = errors.New("consistency-broken")
)

func shapef(format string, args ...any) error {
	return fmt.Errorf("%w: %s", ErrShapeInvalid, fmt.Sprintf(format, args...))
}
func answerf(format string, args ...any) error {
	return fmt.Errorf("%w: %s", ErrAnswerMismatch, fmt.Sprintf(format, args...))
}
func formatf(format string, args ...any) error {
	return fmt.Errorf("%w: %s", ErrFormatViolation, fmt.Sprintf(format, args...))
}
func consistf(format string, args ...any) error {
	return fmt.Errorf("%w: %s", ErrConsistencyBroken, fmt.Sprintf(format, args...))
}

// Validate 按模板 id 分发到该母题的独立验证器。未知模板返回错误。
func Validate(inst *Instance) error {
	switch inst.TemplateID {
	case idIntMul:
		return validateIntMul(inst)
	case idFracCmp:
		return validateFracCompare(inst)
	case idUnitConv:
		return validateUnitConv(inst)
	case idIntRound:
		return validateIntRound(inst)
	case idFracAddSub:
		return validateFracAddSub(inst)
	case idDecCmp:
		return validateDecCompare(inst)
	case idIntAddSub:
		return validateIntAddSub(inst)
	default:
		return shapef("未知母题模板 id：%q", inst.TemplateID)
	}
}

// ────────────────────────────────────────────────────────────────
// 公共结构检查（只对“已发布形态”做断言；三个验证器共用此入口，
// 与生成器仍零共享——生成器不调用本文件的任何函数）
// ────────────────────────────────────────────────────────────────

type stemBlock struct{ rendered string }

func checkPublishedShape(inst *Instance) (stemBlock, error) {
	if inst.TemplateID == "" || inst.TemplateVersionID == "" {
		return stemBlock{}, shapef("模板身份字段缺失")
	}
	if inst.Locale != "zh-CN" {
		return stemBlock{}, shapef("locale 非平台默认 zh-CN：%q", inst.Locale)
	}
	kpSet, ok := inst.Objective["kp_set"].([]any)
	if !ok || len(kpSet) == 0 {
		return stemBlock{}, shapef("objective.kp_set 缺失或为空")
	}
	for _, f := range []string{"kp_set_mode", "cognitive_level", "gradeband", "graph_release"} {
		if s, _ := inst.Objective[f].(string); s == "" {
			return stemBlock{}, shapef("objective.%s 缺失", f)
		}
	}
	blocks, ok := inst.Content["blocks"].([]any)
	if !ok || len(blocks) == 0 {
		return stemBlock{}, shapef("content.blocks 缺失或为空")
	}
	first, ok := blocks[0].(map[string]any)
	if !ok || first["kind"] != "text" {
		return stemBlock{}, shapef("blocks[0] 非题干 text 块")
	}
	stemR, _ := first["rendered"].(string)
	if strings.TrimSpace(stemR) == "" {
		return stemBlock{}, shapef("题干 rendered 为空")
	}
	expl, _ := inst.Content["explanation"].(string)
	if expl == "" {
		return stemBlock{}, shapef("解析 explanation 为空")
	}
	scorerID, _ := inst.ScoringRef["scorer_id"].(string)
	if scorerID != "exact_match" {
		return stemBlock{}, shapef("scorer_id 非注册表 exact_match：%q", scorerID)
	}
	params, ok := inst.ScoringRef["scorer_params"].(map[string]any)
	if !ok {
		return stemBlock{}, shapef("scoring_ref.scorer_params 缺失")
	}
	ansStr, _ := params["answer"].(string)
	if ansStr == "" {
		return stemBlock{}, shapef("scorer_params.answer 为空")
	}
	if inst.Lineage["tier"] != "A" {
		return stemBlock{}, shapef("lineage.tier 非 A 级生产线")
	}
	if len(inst.ErrorBindings) == 0 {
		return stemBlock{}, shapef("error_bindings 为空（R-Q-06 要求干扰项即错误映射）")
	}
	return stemBlock{rendered: stemR}, nil
}

// choiceOptions 解析单选题的 A-D 选项块：字母连续、label 非空。
type choiceOptions struct {
	letters []string
	labels  []string // 选项呈现文本（去 "A. " 前缀后）
}

func parseChoiceBlocks(inst *Instance, wantN int) (choiceOptions, error) {
	interID, _ := inst.InteractionRef["interaction_id"].(string)
	if interID != "single_choice" {
		return choiceOptions{}, shapef("interaction_id=%q，本模板应为 single_choice", interID)
	}
	blocks := inst.Content["blocks"].([]any)
	opts := choiceOptions{letters: make([]string, 0, wantN), labels: make([]string, 0, wantN)}
	prefixRe := regexp.MustCompile(`^([A-Z])\. (.+)$`)
	seenLetter := map[string]bool{}
	for i, b := range blocks[1:] {
		m, ok := b.(map[string]any)
		if !ok {
			continue
		}
		rendered, _ := m["rendered"].(string)
		mm := prefixRe.FindStringSubmatch(rendered)
		if mm == nil {
			if i < wantN {
				return choiceOptions{}, shapef("第 %d 块非选项行：%q", i+1, rendered)
			}
			continue
		}
		let, label := mm[1], mm[2]
		expect := string(rune('A' + len(opts.letters)))
		if let != expect {
			return choiceOptions{}, shapef("选项字母不连续：期望 %s 得 %s", expect, let)
		}
		if seenLetter[let] {
			return choiceOptions{}, shapef("选项字母重复：%s", let)
		}
		seenLetter[let] = true
		if label == "" {
			return choiceOptions{}, shapef("选项 %s 文本为空", let)
		}
		if i >= wantN {
			return choiceOptions{}, shapef("选项数超过预期 %d", wantN)
		}
		opts.letters = append(opts.letters, let)
		opts.labels = append(opts.labels, label)
	}
	if len(opts.labels) != wantN {
		return choiceOptions{}, shapef("选项数量=%d，期望 %d", len(opts.labels), wantN)
	}
	return opts, nil
}

// answerOf 取 content.answer 的字符串字段。
func answerOf(inst *Instance, field string) string {
	m, _ := inst.Content["answer"].(map[string]any)
	s, _ := m[field].(string)
	return s
}

// ────────────────────────────────────────────────────────────────
// 母题① 整数乘法单选验证器
// ────────────────────────────────────────────────────────────────

var intTokenRe = regexp.MustCompile(`[0-9]+`)

func validateIntMul(inst *Instance) error {
	stem, err := checkPublishedShape(inst)
	if err != nil {
		return err
	}
	tokens := intTokenRe.FindAllString(stem.rendered, -1)
	if len(tokens) != 2 {
		return consistf("题干应恰含两个操作数，实得 %d 个数字串", len(tokens))
	}
	a, errA := strconv.ParseInt(tokens[0], 10, 64)
	b, errB := strconv.ParseInt(tokens[1], 10, 64)
	if errA != nil || errB != nil {
		return formatf("操作数解析失败：%v/%v", tokens[0], tokens[1])
	}
	product := a * b // 独立重算（乘法即定义，不复用生成器表达式路径）
	productStr := fmtInt(product)

	answerValue := answerOf(inst, "value")
	if answerValue == "" {
		return shapef("content.answer.value 缺失")
	}
	scAns, _ := inst.ScoringRef["scorer_params"].(map[string]any)["answer"].(string)
	if answerValue != productStr || scAns != productStr {
		return answerf("乘积应为 %s，content.answer=%q scoring_ref.answer=%q",
			productStr, answerValue, scAns)
	}

	opts, err := parseChoiceBlocks(inst, numOptionCnt)
	if err != nil {
		return err
	}
	seen := map[string]bool{}
	correctLetters := make([]string, 0, 1)
	for i, lab := range opts.labels {
		v, perr := strconv.ParseInt(lab, 10, 64)
		if perr != nil || v <= 0 {
			return formatf("选项 %s 非正整数：%q", opts.letters[i], lab)
		}
		if seen[lab] {
			return consistf("选项值重复不可解：%s 出现多次", lab)
		}
		seen[lab] = true
		if lab == productStr {
			correctLetters = append(correctLetters, opts.letters[i])
		}
	}
	if len(correctLetters) != 1 {
		return consistf("等于正解的选项应有且仅有 1 个，实得 %d", len(correctLetters))
	}
	if got := answerOf(inst, "letter"); got != correctLetters[0] {
		return answerf("answer.letter=%q 应指向正解选项 %s", got, correctLetters[0])
	}
	// 每个干扰项都必须登记错误类型（R-Q-06）
	wantBinds := len(opts.labels) - 1
	if len(inst.ErrorBindings) != wantBinds {
		return consistf("error_bindings=%d 条，期望每个干扰项 1 条（%d）",
			len(inst.ErrorBindings), wantBinds)
	}
	expl, _ := inst.Content["explanation"].(string)
	if !strings.Contains(expl, fmtInt(a)) ||
		!strings.Contains(expl, fmtInt(b)) ||
		!strings.Contains(expl, productStr) {
		return consistf("解析未包含可复核要素（%d×%d=%s）", a, b, productStr)
	}
	return nil
}

// ────────────────────────────────────────────────────────────────
// 母题② 分数比较单选验证器
// ────────────────────────────────────────────────────────────────

var fracTokenRe = regexp.MustCompile(`([0-9]+)/([0-9]+)`)

func validateFracCompare(inst *Instance) error {
	stem, err := checkPublishedShape(inst)
	if err != nil {
		return err
	}
	ms := fracTokenRe.FindAllStringSubmatch(stem.rendered, -1)
	if len(ms) != 2 {
		return consistf("题干应恰含两个分数，实得 %d 个", len(ms))
	}
	f1, e1 := parseFracPair(ms[0])
	f2, e2 := parseFracPair(ms[1])
	if e1 != nil {
		return e1
	}
	if e2 != nil {
		return e2
	}
	if f1.key() == f2.key() {
		return consistf("两分数相等（%s），无比较意义", f1.display())
	}
	// 独立交叉相乘定大小
	x, y := f1.n*f2.d, f2.n*f1.d
	var big fracVal
	switch {
	case x > y:
		big = f1
	case y > x:
		big = f2
	default:
		return consistf("交叉相乘结果相等但键不同，数值异常")
	}
	bigStr := big.display()

	answerValue := answerOf(inst, "value")
	scAns, _ := inst.ScoringRef["scorer_params"].(map[string]any)["answer"].(string)
	if answerValue != bigStr || scAns != bigStr {
		return answerf("更大分数应为 %s，content.answer=%q scoring_ref.answer=%q",
			bigStr, answerValue, scAns)
	}

	opts, err := parseChoiceBlocks(inst, numOptionCnt)
	if err != nil {
		return err
	}
	keyLabel := map[string]string{}
	correct := make([]string, 0, 1)
	for i, lab := range opts.labels {
		f, ferr := parseFracPair(fracTokenRe.FindStringSubmatch(lab))
		if ferr != nil || lab != fracTokenRe.FindString(lab) {
			return formatf("选项 %s 非规范分数显示：%q", opts.letters[i], lab)
		}
		if prev, dup := keyLabel[f.key()]; dup {
			return consistf("存在约分后等值的选项（多解）：%s 与 %s", prev, lab)
		}
		keyLabel[f.key()] = lab
		if f.key() == big.key() {
			correct = append(correct, opts.letters[i])
		}
	}
	if len(correct) != 1 {
		return consistf("更大分数的选项应有且仅有 1 个，实得 %d", len(correct))
	}
	if got := answerOf(inst, "letter"); got != correct[0] {
		return answerf("answer.letter=%q 应指向更大分数选项 %s", got, correct[0])
	}
	expl, _ := inst.Content["explanation"].(string)
	if !strings.Contains(expl, fmtInt(x)) || !strings.Contains(expl, fmtInt(y)) ||
		!strings.Contains(expl, bigStr) {
		return consistf("解析缺少交叉相乘复核要素（%s / %s / %s）", fmtInt(x), fmtInt(y), bigStr)
	}
	if len(inst.ErrorBindings) != len(opts.labels)-1 {
		return consistf("error_bindings 数量与干扰项数不一致")
	}
	return nil
}

// parseFracPair 把 FindStringSubmatch 的 (整串,n,d) 组装成分数并做规范断言。
func parseFracPair(m []string) (fracVal, error) {
	if m == nil {
		return fracVal{}, formatf("空分数匹配")
	}
	return parseFracTokens(m[1], m[2])
}

func parseFracTokens(nStr, dStr string) (fracVal, error) {
	n, err1 := strconv.ParseInt(nStr, 10, 64)
	d, err2 := strconv.ParseInt(dStr, 10, 64)
	if err1 != nil || err2 != nil {
		return fracVal{}, formatf("分数解析失败：%s/%s", nStr, dStr)
	}
	f := fracVal{n, d}
	if !f.properReduced() {
		return fracVal{}, formatf("分数未按最简真分数规范：%d/%d", n, d)
	}
	return f, nil
}

// ────────────────────────────────────────────────────────────────
// 母题③ 单位换算数值填空验证器
// ────────────────────────────────────────────────────────────────

// unitFamiliesVal 验证器侧量纲表独立副本（禁止改引用 generators 的表）。
var unitFamiliesVal = []struct {
	units map[string]int
}{
	{units: map[string]int{"毫米": 0, "厘米": 1, "分米": 2, "米": 3, "千米": 6}},
	{units: map[string]int{"克": 0, "千克": 3, "吨": 6}},
	{units: map[string]int{"分": 0, "角": 1, "元": 2}},
}

var convStemRe = regexp.MustCompile(
	`([0-9]+(?:\.[0-9]+)?)\s*(千米|米|分米|厘米|毫米|千克|克|吨|元|角|分)\s*=\s*（\s*）\s*(千米|米|分米|厘米|毫米|千克|克|吨|元|角|分)`)

// familyOf 返回单位所属族下标（未登记单位返回 -1）；exp10Of 返回幂次。
func familyOf(u string) int {
	for fi, f := range unitFamiliesVal {
		if _, ok := f.units[u]; ok {
			return fi
		}
	}
	return -1
}

func exp10Of(u string) int {
	for _, f := range unitFamiliesVal {
		if e, ok := f.units[u]; ok {
			return e
		}
	}
	return -1000
}

func validateUnitConv(inst *Instance) error {
	stem, err := checkPublishedShape(inst)
	if err != nil {
		return err
	}
	interID, _ := inst.InteractionRef["interaction_id"].(string)
	if interID != "numeric_blank" {
		return shapef("interaction_id=%q，本模板应为 numeric_blank", interID)
	}
	ms := convStemRe.FindStringSubmatch(stem.rendered)
	if ms == nil {
		return shapef("题干不匹配单位换算句式：%q", stem.rendered)
	}
	valStr, uFrom, uTo := ms[1], ms[2], ms[3]
	fi, fo := familyOf(uFrom), familyOf(uTo)
	if fi < 0 || fo < 0 {
		return shapef("单位不在验证器量纲表内：%q→%q", uFrom, uTo)
	}
	if fi != fo {
		return consistf("跨量纲换算被拒：%s → %s", uFrom, uTo)
	}
	if uFrom == uTo {
		return consistf("同单位换算无意义：%s", uFrom)
	}
	mant, scale, perr := parseDecString(valStr)
	if perr != nil {
		return formatf("换算值非规范十进制：%q（%v）", valStr, perr)
	}
	if mant <= 0 || mant > 999 {
		return formatf("换算值超出母题值域 [1,999] 的整数有效数字段：%q", valStr)
	}

	// 独立重算（本文件自己的指数表）：exp=「1 该单位=多少基单位」的幂次，
	// 故乘数 = 10^(exp(from)-exp(to))——大→小放大，小→大缩小。
	delta := exp10Of(uFrom) - exp10Of(uTo)
	if delta > 7-scale || delta < -4-scale {
		return formatf("进率差超界 delta=%d scale=%d", delta, scale)
	}
	var expected string
	if delta >= scale {
		expected = fmtInt(mant * pow10(delta-scale))
	} else {
		expected = decString(mant, scale-delta)
	}

	ansM, _ := inst.Content["answer"].(map[string]any)
	gotVal, _ := ansM["value"].(string)
	gotUnit, _ := ansM["unit"].(string)
	sp, _ := inst.ScoringRef["scorer_params"].(map[string]any)
	scAns, _ := sp["answer"].(string)
	scUnit, _ := sp["unit"].(string)
	if gotVal != expected || scAns != expected {
		return answerf("%s %s → %s 应为 %s，content.answer=%q scoring_ref=%q",
			valStr, uFrom, uTo, expected, gotVal, scAns)
	}
	if gotUnit != uTo || scUnit != uTo {
		return answerf("目标单位应为 %s，content.answer.unit=%q scoring_ref.unit=%q", uTo, gotUnit, scUnit)
	}
	if blank, _ := ansM["blank_id"].(string); blank == "" {
		return shapef("content.answer.blank_id 缺失")
	}
	expl, _ := inst.Content["explanation"].(string)
	if !strings.Contains(expl, expected) || !strings.Contains(expl, uTo) {
		return consistf("解析未含复核要素（%s / %s）", expected, uTo)
	}
	if inst.ErrorBindings[0]["subject"] != "blank:b1" {
		return consistf("填空题错误绑定主体应为 blank:b1")
	}
	return nil
}

// ────────────────────────────────────────────────────────────────
// 母题④ 四舍五入近似数 数值填空验证器
//
// half-up 用纯算术路径（余数×2 对比半单位）重算——与生成器的数位字符串
// 路径互为独立实现；位档词表为验证器自持副本（字面与生成器一致但引用隔离）。
// ────────────────────────────────────────────────────────────────

var roundStemRe = regexp.MustCompile(
	`([0-9]+)\s*≈\s*（\s*）\s*（四舍五入到(十|百|千)位）`)

func validateIntRound(inst *Instance) error {
	stem, err := checkPublishedShape(inst)
	if err != nil {
		return err
	}
	interID, _ := inst.InteractionRef["interaction_id"].(string)
	if interID != "numeric_blank" {
		return shapef("interaction_id=%q，本模板应为 numeric_blank", interID)
	}
	ms := roundStemRe.FindStringSubmatch(stem.rendered)
	if ms == nil {
		return shapef("题干不匹配近似数句式：%q", stem.rendered)
	}
	v, perr := strconv.ParseInt(ms[1], 10, 64)
	if perr != nil {
		return formatf("近似数值解析失败：%q", ms[1])
	}
	place := map[string]int{"十": 1, "百": 2, "千": 3}[ms[2]]
	if v < 100 || v > 9999 {
		return formatf("近似数值超出母题值域 [100,9999]：%d", v)
	}
	if place == 3 && v < 1000 {
		return formatf("三位数 %d 无千位可四舍五入（位档与值域矛盾）", v)
	}

	// 独立重算（本文件自己的 half-up 算术路径）：u=10^k，余数×2 ≥ 半单位则进一
	u := int64(1)
	for i := 0; i < place; i++ {
		u *= 10
	}
	rem := v % u
	var expected int64
	if rem*2 >= u {
		expected = v - rem + u
	} else {
		expected = v - rem
	}
	if expected < 0 || expected-v > u/2 || v-expected > u/2 {
		return consistf("half-up 性质破坏：v=%d place=%d expected=%d", v, place, expected)
	}

	ansM, _ := inst.Content["answer"].(map[string]any)
	gotVal, _ := ansM["value"].(string)
	sp, _ := inst.ScoringRef["scorer_params"].(map[string]any)
	scAns, _ := sp["answer"].(string)
	if gotVal != fmtInt(expected) || scAns != fmtInt(expected) {
		return answerf("%d 四舍五入到%s应为 %s，content.answer=%q scoring_ref=%q",
			v, ms[2], fmtInt(expected), gotVal, scAns)
	}
	if blank, _ := ansM["blank_id"].(string); blank == "" {
		return shapef("content.answer.blank_id 缺失")
	}
	expl, _ := inst.Content["explanation"].(string)
	if !strings.Contains(expl, fmtInt(v)) || !strings.Contains(expl, fmtInt(expected)) ||
		!strings.Contains(expl, ms[2]) {
		return consistf("解析未含复核要素（%d / %s / %s）", v, fmtInt(expected), ms[2])
	}
	if inst.ErrorBindings[0]["subject"] != "blank:b1" {
		return consistf("填空题错误绑定主体应为 blank:b1")
	}
	return nil
}

// ────────────────────────────────────────────────────────────────
// 母题⑤ 同分母分数加减 单选验证器
//
// 操作数只需真分数（允许未化简，如 2/8——同分母练习的原始形态）；**答案与
// 选项必须最简**。重算路径：重新提取两分数→判同分母→分子加减→自行约分。
// ────────────────────────────────────────────────────────────────

func validateFracAddSub(inst *Instance) error {
	stem, err := checkPublishedShape(inst)
	if err != nil {
		return err
	}
	hasAdd := strings.Contains(stem.rendered, "＋")
	hasSub := strings.Contains(stem.rendered, "－")
	if hasAdd == hasSub {
		return shapef("题干算符歧义（应恰含一个 ＋/－）：%q", stem.rendered)
	}

	ms := fracTokenRe.FindAllStringSubmatch(stem.rendered, -1)
	if len(ms) != 2 {
		return consistf("题干应恰含两个分数，实得 %d 个", len(ms))
	}
	ops := make([]fracVal, 0, 2)
	for i, m := range ms {
		n, e1 := strconv.ParseInt(m[1], 10, 64)
		d, e2 := strconv.ParseInt(m[2], 10, 64)
		if e1 != nil || e2 != nil {
			return formatf("操作数 %d 解析失败：%s/%s", i+1, m[1], m[2])
		}
		if d < 3 || d > 12 {
			return formatf("分母 %d 超出母题口径 [3,12]", d)
		}
		if n < 1 || n >= d {
			return formatf("操作数 %d 非真分数：%d/%d", i+1, n, d)
		}
		ops = append(ops, fracVal{n, d})
	}
	if ops[0].d != ops[1].d {
		return consistf("非同分母（%s 与 %s），不属本母题", ms[0][0], ms[1][0])
	}
	d := ops[0].d

	var raw int64
	if hasAdd {
		raw = ops[0].n + ops[1].n
		if raw >= d {
			return consistf("加法和 %d ≥ 分母 %d（超出真分数空间约束）", raw, d)
		}
	} else {
		raw = ops[0].n - ops[1].n
		if raw < 1 {
			return consistf("减法差 %d 非正（超空间约束）", raw)
		}
	}
	// 独立约分（验证器自己的三行约分，不复用生成器表达式路径）
	g := gcdI64(raw, d)
	expected := fmtInt(raw/g) + "/" + fmtInt(d/g)

	answerValue := answerOf(inst, "value")
	scAns, _ := inst.ScoringRef["scorer_params"].(map[string]any)["answer"].(string)
	if answerValue != expected || scAns != expected {
		return answerf("结果应为 %s，content.answer=%q scoring_ref.answer=%q",
			expected, answerValue, scAns)
	}

	opts, err := parseChoiceBlocks(inst, numOptionCnt)
	if err != nil {
		return err
	}
	expKey := fracVal{raw, d}.key()
	keyLabel := map[string]string{}
	correct := make([]string, 0, 1)
	for i, lab := range opts.labels {
		om := fracTokenRe.FindStringSubmatch(lab)
		if om == nil || om[0] != lab {
			return formatf("选项 %s 非纯分数标签：%q", opts.letters[i], lab)
		}
		f, ferr := parseFracPair(om)
		if ferr != nil {
			return ferr
		}
		if prev, dup := keyLabel[f.key()]; dup {
			return consistf("存在约分后等值的选项（多解）：%s 与 %s", prev, lab)
		}
		keyLabel[f.key()] = lab
		if f.key() == expKey {
			correct = append(correct, opts.letters[i])
		}
	}
	if len(correct) != 1 {
		return consistf("等于正解的选项应有且仅有 1 个，实得 %d", len(correct))
	}
	if got := answerOf(inst, "letter"); got != correct[0] {
		return answerf("answer.letter=%q 应指向正解选项 %s", got, correct[0])
	}
	expl, _ := inst.Content["explanation"].(string)
	for _, need := range []string{fmtInt(ops[0].n), fmtInt(ops[1].n), fmtInt(d), fmtInt(raw), expected} {
		if !strings.Contains(expl, need) {
			return consistf("解析缺可复核要素 %q", need)
		}
	}
	if len(inst.ErrorBindings) != len(opts.labels)-1 {
		return consistf("error_bindings 数量与干扰项数不一致")
	}
	return nil
}

// ────────────────────────────────────────────────────────────────
// 母题⑥ 小数大小比较 单选验证器
//
// 独立比较路径：补零对齐位数后按整数比（与生成器的交叉相乘互为对照）。
// 规范域断言自持副本：s∈{1,2}、值域上限、无尾零——非规范串一律 format 拒。
// ────────────────────────────────────────────────────────────────

var decTokenRe = regexp.MustCompile(`([0-9]+)\.([0-9]+)`)

// decFromParts 把小数的整数/小数部分串重组为规范化 (m, s)（验证器自持实现）：
// "0.01" → (1,2)；前导零整数部分（"01.2"）与超 int64 拼接一律拒绝。
func decFromParts(ip, fp string) (decVal, error) {
	if len(ip) > 1 && ip[0] == '0' {
		return decVal{}, fmt.Errorf("整数部分前导零非规范：%s.%s", ip, fp)
	}
	m, err := strconv.ParseInt(ip+fp, 10, 64)
	if err != nil {
		return decVal{}, fmt.Errorf("数值超出 int64：%s.%s", ip, fp)
	}
	return decVal{m: m, s: len(fp)}, nil
}

func validateDecCompare(inst *Instance) error {
	stem, err := checkPublishedShape(inst)
	if err != nil {
		return err
	}
	ms := decTokenRe.FindAllStringSubmatch(stem.rendered, -1)
	if len(ms) != 2 {
		return consistf("题干应恰含两个小数，实得 %d 个", len(ms))
	}
	vals := make([]decVal, 0, 2)
	for i, m := range ms {
		d, perr := decFromParts(m[1], m[2])
		if perr != nil || !d.canonical() {
			return formatf("小数 %d 非母题规范域（s∈{1,2}，无尾零，值域内）：%q（%v）", i+1, m[0], perr)
		}
		vals = append(vals, d)
	}
	// 独立重算：补零对齐位数后比整数（与生成器的交叉相乘互为对照路径）。
	// 补零渲染为验证器自持实现（数位切分补齐，非 decString——尾零必须保留）。
	pad := vals[0].s
	if vals[1].s > pad {
		pad = vals[1].s
	}
	padFormOf := func(d decVal) string {
		str := fmtInt(d.m)
		if len(str) <= d.s {
			str = strings.Repeat("0", d.s-len(str)+1) + str
		}
		fp := str[len(str)-d.s:]
		for len(fp) < pad {
			fp += "0"
		}
		return str[:len(str)-d.s] + "." + fp
	}
	// 值级比较键：补零后的整数读数（值 × 10^pad，值相等 ⇔ 键相等）
	keyOf := func(d decVal) int64 { return d.m * int64(pow10(pad-d.s)) }
	na, nb := keyOf(vals[0]), keyOf(vals[1])
	if na == nb {
		return consistf("两小数等值（%s），无比较意义", vals[0].display())
	}
	big, small := vals[0], vals[1]
	if nb > na {
		big, small = vals[1], vals[0]
	}
	bigStr := big.display()
	bigPad := padFormOf(big)
	smallPad := padFormOf(small)

	answerValue := answerOf(inst, "value")
	scAns, _ := inst.ScoringRef["scorer_params"].(map[string]any)["answer"].(string)
	if answerValue != bigStr || scAns != bigStr {
		return answerf("更大数应为 %s，content.answer=%q scoring_ref.answer=%q",
			bigStr, answerValue, scAns)
	}

	opts, err := parseChoiceBlocks(inst, numOptionCnt)
	if err != nil {
		return err
	}
	seenVal := map[string]string{}
	correct := make([]string, 0, 1)
	for i, lab := range opts.labels {
		om := decTokenRe.FindStringSubmatch(lab)
		if om == nil || om[0] != lab {
			return formatf("选项 %s 非纯小数标签：%q", opts.letters[i], lab)
		}
		d, perr := decFromParts(om[1], om[2])
		if perr != nil || !d.canonical() {
			return formatf("选项 %s 非规范小数：%q", opts.letters[i], lab)
		}
		// 值级键：规范化形态串（值域构造保证「规范化串 ↔ 值」双射，
		// 等值 ⇔ 等串；不用按题干位数补零的整数键——选项位数可能超过题干）
		if prev, dup := seenVal[d.display()]; dup {
			return consistf("存在等值选项（多解）：%s 与 %s", prev, lab)
		}
		seenVal[d.display()] = lab
		if d.display() == bigStr {
			correct = append(correct, opts.letters[i])
		}
	}
	if len(correct) != 1 {
		return consistf("更大数的选项应有且仅有 1 个，实得 %d", len(correct))
	}
	if got := answerOf(inst, "letter"); got != correct[0] {
		return answerf("answer.letter=%q 应指向更大数选项 %s", got, correct[0])
	}
	expl, _ := inst.Content["explanation"].(string)
	if !strings.Contains(expl, bigPad) || !strings.Contains(expl, smallPad) ||
		!strings.Contains(expl, bigStr) {
		return consistf("解析缺对齐位数复核要素（%s / %s / %s）", bigPad, smallPad, bigStr)
	}
	if len(inst.ErrorBindings) != len(opts.labels)-1 {
		return consistf("error_bindings 数量与干扰项数不一致")
	}
	return nil
}

// ────────────────────────────────────────────────────────────────
// 母题⑦ 整数进位加/退位减 数值填空验证器
//
// 重算即定义（加减法），独立性落在：操作数重新提取 + 进/退位性质断言 +
// 值域断言。op 判定优先级：算符符号 > 关键词锚点（一共/还剩），均缺即拒。
// ────────────────────────────────────────────────────────────────

func validateIntAddSub(inst *Instance) error {
	stem, err := checkPublishedShape(inst)
	if err != nil {
		return err
	}
	interID, _ := inst.InteractionRef["interaction_id"].(string)
	if interID != "numeric_blank" {
		return shapef("interaction_id=%q，本模板应为 numeric_blank", interID)
	}
	hasAdd := strings.Contains(stem.rendered, "＋")
	hasSub := strings.Contains(stem.rendered, "－")
	if hasAdd && hasSub {
		return shapef("题干同时含 ＋ 与 －，算符歧义：%q", stem.rendered)
	}
	var isAdd bool
	switch {
	case hasAdd:
		isAdd = true
	case hasSub:
		isAdd = false
	case strings.Contains(stem.rendered, "一共"):
		isAdd = true
	case strings.Contains(stem.rendered, "还剩"):
		isAdd = false
	default:
		return shapef("题干无算符亦无关键词锚点（一共/还剩）：%q", stem.rendered)
	}

	tokens := intTokenRe.FindAllString(stem.rendered, -1)
	if len(tokens) != 2 {
		return consistf("题干应恰含两个操作数，实得 %d 个数字串", len(tokens))
	}
	a, e1 := strconv.ParseInt(tokens[0], 10, 64)
	b, e2 := strconv.ParseInt(tokens[1], 10, 64)
	if e1 != nil || e2 != nil {
		return formatf("操作数解析失败：%v/%v", tokens[0], tokens[1])
	}
	if a < 100 || a > 999 {
		return formatf("被操作数 %d 超出母题值域 [100,999]", a)
	}
	if b < 11 || b > 99 {
		return formatf("操作数 %d 超出母题值域 [11,99]", b)
	}
	if isAdd {
		if a%10+b%10 < 10 {
			return consistf("个位 %d+%d=%d 无进位，不属进位加法空间", a%10, b%10, a%10+b%10)
		}
	} else {
		if a <= b {
			return consistf("减法 a=%d ≤ b=%d（差非正，超空间约束）", a, b)
		}
		if a%10 >= b%10 {
			return consistf("个位 %d ≥ %d 无退位，不属退位减法空间", a%10, b%10)
		}
	}

	// 独立重算（加减即定义）
	var expected int64
	if isAdd {
		expected = a + b
	} else {
		expected = a - b
	}

	ansM, _ := inst.Content["answer"].(map[string]any)
	gotVal, _ := ansM["value"].(string)
	sp, _ := inst.ScoringRef["scorer_params"].(map[string]any)
	scAns, _ := sp["answer"].(string)
	if gotVal != fmtInt(expected) || scAns != fmtInt(expected) {
		return answerf("结果应为 %s，content.answer=%q scoring_ref=%q",
			fmtInt(expected), gotVal, scAns)
	}
	if blank, _ := ansM["blank_id"].(string); blank == "" {
		return shapef("content.answer.blank_id 缺失")
	}
	if !strings.Contains(stem.rendered, "（") || !strings.Contains(stem.rendered, "）") {
		return shapef("题干缺括号空位：%q", stem.rendered)
	}
	expl, _ := inst.Content["explanation"].(string)
	if !strings.Contains(expl, fmtInt(a)) || !strings.Contains(expl, fmtInt(b)) ||
		!strings.Contains(expl, fmtInt(expected)) {
		return consistf("解析未含可复核要素（%d / %d / %s）", a, b, fmtInt(expected))
	}
	if inst.ErrorBindings[0]["subject"] != "blank:b1" {
		return consistf("填空题错误绑定主体应为 blank:b1")
	}
	return nil
}
