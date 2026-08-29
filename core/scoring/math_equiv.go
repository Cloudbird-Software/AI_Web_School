package scoring

// math_equivalence 评分器（scorer.yaml §72；Python 学科包实现
// src/packs/subject-math/scorers/math_equivalence.py 的 Go 平台面移植——
// 学科包评分器收敛进平台注册表，id/契约签名以冻结的 scorer.yaml 为准）。
//
// 双实现独立重算纪律（架构 v2 §4.3 验算之双实现的本卡口径）：解析与比较
// 分两套代码——parseMathExpr 只负责「文本→精确有理数」，equivalentValues
// 只负责「值×值→等值判定」，两侧互不感知实现细节。零浮点：全程 math/big
// 精确算术，tolerance 为规范化小数字符串（契约 §notes 禁浮点字面量）。
//
// 与 packs/subjectmath 的生成/验证器零代码共享（X6：core 禁 import packs；
// 单位换算表在本文件独立成表——重复常量表是纪律要求，防实现间陪跑同错）。

import (
	"context"
	"encoding/json"
	"fmt"
	"math/big"
	"regexp"
	"sort"
	"strings"

	"github.com/Cloudbird-Software/AI_Web_School/registry"
)

// versionMathEquivalence 评分器版本（Python 冻结实现 1.0.0+subject-math 同值）.
const versionMathEquivalence = "1.0.0+subject-math"

// ────────────────────────────────────────────────────────────────────
// 第一套代码：解析（文本 → 精确有理数）
// ────────────────────────────────────────────────────────────────────

// parseMathExpr 解析安全表达式子集（数字/四则/括号/一元正负号）为精确有理数。
// 语法：expr := term (('+'|'-') term)* ；term := factor (('*'|'/') factor)*；
// factor := ('+'|'-')* atom ；atom := number | '(' expr ')'。
// 空表达式/语法错误/除零皆报错（错误文本只含位置，不含表达式原文，X3）.
func parseMathExpr(s string) (*big.Rat, error) {
	p := &mathParser{src: []rune(strings.TrimSpace(s))}
	v, err := p.expr()
	if err != nil {
		return nil, err
	}
	p.skipWS()
	if p.i != len(p.src) {
		return nil, fmt.Errorf("%w: math_equivalence 表达式语法错误（位置 %d）", ErrInvalidInput, p.i)
	}
	return v, nil
}

type mathParser struct {
	src []rune
	i   int
}

func (p *mathParser) skipWS() {
	for p.i < len(p.src) {
		switch p.src[p.i] {
		case ' ', '\t', '\n', '\r':
			p.i++
		default:
			return
		}
	}
}

func (p *mathParser) peek() (rune, bool) {
	if p.i < len(p.src) {
		return p.src[p.i], true
	}
	return 0, false
}

func (p *mathParser) expr() (*big.Rat, error) {
	left, err := p.term()
	if err != nil {
		return nil, err
	}
	for {
		p.skipWS()
		r, ok := p.peek()
		if !ok || (r != '+' && r != '-') {
			return left, nil
		}
		p.i++
		right, err := p.term()
		if err != nil {
			return nil, err
		}
		if r == '+' {
			left.Add(left, right)
		} else {
			left.Sub(left, right)
		}
	}
}

func (p *mathParser) term() (*big.Rat, error) {
	left, err := p.factor()
	if err != nil {
		return nil, err
	}
	for {
		p.skipWS()
		r, ok := p.peek()
		if !ok || (r != '*' && r != '/') {
			return left, nil
		}
		p.i++
		right, err := p.factor()
		if err != nil {
			return nil, err
		}
		if r == '*' {
			left.Mul(left, right)
		} else {
			if right.Sign() == 0 {
				return nil, fmt.Errorf("%w: math_equivalence 表达式除零（位置 %d）", ErrInvalidInput, p.i)
			}
			left.Quo(left, right)
		}
	}
}

func (p *mathParser) factor() (*big.Rat, error) {
	p.skipWS()
	neg := false
	for {
		r, ok := p.peek()
		if !ok || (r != '+' && r != '-') {
			break
		}
		p.i++
		if r == '-' {
			neg = !neg
		}
		p.skipWS()
	}
	v, err := p.atom()
	if err != nil {
		return nil, err
	}
	if neg {
		v.Neg(v)
	}
	return v, nil
}

func (p *mathParser) atom() (*big.Rat, error) {
	p.skipWS()
	r, ok := p.peek()
	if !ok {
		return nil, fmt.Errorf("%w: math_equivalence 表达式意外结束", ErrInvalidInput)
	}
	if r == '(' {
		p.i++
		v, err := p.expr()
		if err != nil {
			return nil, err
		}
		p.skipWS()
		closer, ok := p.peek()
		if !ok || closer != ')' {
			return nil, fmt.Errorf("%w: math_equivalence 括号不闭合（位置 %d）", ErrInvalidInput, p.i)
		}
		p.i++
		return v, nil
	}
	return p.number()
}

// number 精确十进制字面量：m / 10^frac（big.Int 构造，零浮点）.
func (p *mathParser) number() (*big.Rat, error) {
	start := p.i
	digits, frac := 0, 0
	for p.i < len(p.src) && p.src[p.i] >= '0' && p.src[p.i] <= '9' {
		p.i++
		digits++
	}
	if p.i < len(p.src) && p.src[p.i] == '.' {
		p.i++
		for p.i < len(p.src) && p.src[p.i] >= '0' && p.src[p.i] <= '9' {
			p.i++
			digits++
			frac++
		}
	}
	if digits == 0 {
		return nil, fmt.Errorf("%w: math_equivalence 表达式语法错误（位置 %d）", ErrInvalidInput, start)
	}
	digitsOnly := strings.Replace(string(p.src[start:p.i]), ".", "", 1)
	num, ok := new(big.Int).SetString(digitsOnly, 10)
	if !ok {
		return nil, fmt.Errorf("%w: math_equivalence 数值解析失败（位置 %d）", ErrInvalidInput, start)
	}
	if frac == 0 {
		return new(big.Rat).SetInt(num), nil
	}
	den := new(big.Int).Exp(big.NewInt(10), big.NewInt(int64(frac)), nil)
	return new(big.Rat).SetFrac(num, den), nil
}

// ────────────────────────────────────────────────────────────────────
// 第二套代码：比较（值 × 值 → 等值判定）
// ────────────────────────────────────────────────────────────────────

// equivalentValues 比较侧独立重算：不感知解析实现，只消费精确值——等值判定、
// 化简标记与容差裕度全部在此独立推导（Python compare_with_tolerance 同语义）.
func equivalentValues(actual, expected, tol *big.Rat) (bool, string) {
	if actual.Cmp(expected) == 0 {
		// 值等而形态不同标记化简等价——big.Rat 入手即规范化（2/4 → 1/2），
		// 形态分支与 Python 冻结实现同构地只在非规范值域出现.
		if actual.RatString() != expected.RatString() {
			return true, "fraction_reduce"
		}
		return true, "exact"
	}
	if tol != nil && tol.Sign() > 0 {
		diff := new(big.Rat).Sub(actual, expected)
		if diff.Sign() < 0 {
			diff.Neg(diff)
		}
		if diff.Cmp(tol) <= 0 {
			return true, "tolerance"
		}
	}
	return false, "mismatch"
}

// classifyMathMismatch 差异类型→错误类型推断（Python infer_error_type 移植）；
// 返回 "" 表示无明显错误类型（单位换算后数值等价的表示形式差异）。
// Python 的 unsimplified_fraction 分支在规范化值域结构性不可达（值等 ⇒
// exact 已判），不移植.
func classifyMathMismatch(actual, expected *big.Rat, actualUnit, expectedUnit string) string {
	if actualUnit != "" && expectedUnit != "" && actualUnit != expectedUnit {
		if factorStr, ok := unitTable[actualUnit][expectedUnit]; ok {
			if factor, ok := new(big.Rat).SetString(factorStr); ok {
				converted := new(big.Rat).Mul(actual, factor)
				diff := new(big.Rat).Sub(converted, expected)
				if diff.Sign() < 0 {
					diff.Neg(diff)
				}
				// Python 1e-9 阈值同值（Decimal("1e-9")）.
				if diff.Cmp(big.NewRat(1, 1_000_000_000)) < 0 {
					return ""
				}
			}
			return "wrong_unit"
		}
		return "wrong_unit"
	}
	diff := new(big.Rat).Sub(actual, expected)
	if diff.Cmp(big.NewRat(1, 1)) == 0 || diff.Cmp(big.NewRat(-1, 1)) == 0 {
		return "off_by_one"
	}
	return "value_mismatch"
}

// unitTable 内置单位换算表（Python 冻结实现 _UNIT_TABLE 全量复制；因子为
// 十进制字符串，big.Rat 精确解析——含 min↔s 的 1/60 截断近似，跨语言同值）.
var unitTable = map[string]map[string]string{
	"m":   {"cm": "100", "mm": "1000", "km": "0.001"},
	"cm":  {"m": "0.01", "mm": "10"},
	"mm":  {"cm": "0.1", "m": "0.001"},
	"km":  {"m": "1000", "cm": "100000"},
	"kg":  {"g": "1000"},
	"g":   {"kg": "0.001"},
	"min": {"s": "60", "h": "0.0166666666666666666666666667"},
	"s":   {"min": "0.0166666666666666666666666667"},
	"h":   {"min": "60", "s": "3600"},
}

// unitValueRE 数值+单位识别（Python _UNIT_PATTERN 同款：-?\d+(.\d+)?(/\d+)? + 字母单位）.
var unitValueRE = regexp.MustCompile(`^\s*(-?\d+(?:\.\d+)?(?:/\d+)?)\s*([a-zA-Z]+)\s*$`)

// splitValueUnit 从作答串分离数值与可选单位（单位统一小写；无单位原样返回）.
func splitValueUnit(s string) (value, unit string) {
	m := unitValueRE.FindStringSubmatch(s)
	if m == nil {
		return s, ""
	}
	return m[1], strings.ToLower(m[2])
}

// convertUnit 尝试把 value 从 from 换算到 to（无换算规则第二返回值为 false）.
func convertUnit(value *big.Rat, from, to string) (*big.Rat, bool) {
	if from == to {
		return value, true
	}
	factorStr, ok := unitTable[from][to]
	if !ok {
		return nil, false
	}
	factor, ok := new(big.Rat).SetString(factorStr)
	if !ok {
		return nil, false
	}
	return new(big.Rat).Mul(value, factor), true
}

// ────────────────────────────────────────────────────────────────────
// 评分器
// ────────────────────────────────────────────────────────────────────

// defaultEquivalenceRules 等价规则缺省全集（Python DEFAULT_EQUIVALENCE_RULES 同源）.
var defaultEquivalenceRules = []string{"fraction_reduce", "unit_convert", "decimal_tolerance"}

// MathEquivalenceScorer 是 math_equivalence 评分器（确定性；分数化简/单位
// 换算/数值容差三类等价判定）.
type MathEquivalenceScorer struct{}

// NewMathEquivalenceScorer 构造 math_equivalence 评分器.
func NewMathEquivalenceScorer() *MathEquivalenceScorer { return &MathEquivalenceScorer{} }

// Entry 实现 registry.Scorer.
func (s *MathEquivalenceScorer) Entry() registry.Entry {
	return registry.Entry{ID: "math_equivalence", Version: versionMathEquivalence}
}

// ScorerContract 实现 registry.Contracted（scorer.yaml §78 required=[answer_expr]）.
func (s *MathEquivalenceScorer) ScorerContract() registry.ScorerSpec {
	return registry.ScorerSpec{
		Entry:         s.Entry(),
		InputSchema:   map[string]registry.ParamKind{"answer_expr": registry.KindString},
		Deterministic: true,
	}
}

// Score 执行数学等价判定。
//
// 判错但可判定（作答为空/无法解析）按 Python 语义出 0 分结果（置信度不降，
// 推断随 evidence.error_inferences 随行）；配置违例（缺 answer_expr/tolerance
// 非法/标准答案不可解析/多空作答）fail-loud 报错——无法判定≠判错.
func (s *MathEquivalenceScorer) Score(_ context.Context, answer string, params map[string]any) (registry.ScoreResult, error) {
	answerExpr, _ := params["answer_expr"].(string)
	if strings.TrimSpace(answerExpr) == "" {
		return registry.ScoreResult{}, fmt.Errorf("%w: math_equivalence 缺 answer_expr（禁止静默判错）", ErrInvalidInput)
	}

	rules := make(map[string]bool, len(defaultEquivalenceRules))
	if raw, ok := params["equivalence_rules"].([]any); ok {
		for _, r := range raw {
			if rs, ok := r.(string); ok {
				rules[rs] = true
			}
		}
	} else {
		for _, r := range defaultEquivalenceRules {
			rules[r] = true
		}
	}
	rulesApplied := make([]string, 0, len(rules))
	for r := range rules {
		rulesApplied = append(rulesApplied, r)
	}
	sort.Strings(rulesApplied)

	tolRaw := "0"
	if raw, ok := params["tolerance"].(string); ok && strings.TrimSpace(raw) != "" {
		tolRaw = strings.TrimSpace(raw)
	}
	tol, ok := new(big.Rat).SetString(tolRaw)
	if !ok {
		return registry.ScoreResult{}, fmt.Errorf("%w: math_equivalence tolerance %q 非法（须为规范化小数字符串，禁浮点字面量）", ErrInvalidInput, tolRaw)
	}

	resp := decodeAnswer(answer)
	if m, ok := resp.(map[string]any); ok {
		if _, hasBlanks := m["blanks"]; hasBlanks {
			if _, hasValue := m["value"]; !hasValue {
				blanks, ok := m["blanks"].(map[string]any)
				if !ok || len(blanks) != 1 {
					return registry.ScoreResult{}, fmt.Errorf("%w: math_equivalence 仅支持单空作答（多空题请用 exact_match 逐空或 stepwise_rubric）", ErrInvalidInput)
				}
				for _, v := range blanks {
					resp = v
				}
			}
		}
	}
	actualValueStr, actualUnit := mathExtractValue(resp)
	if actualValueStr == "" {
		return scoreZeroWithInference(map[string]any{
			"reason":        "学生作答为空",
			"rules_applied": rulesApplied,
			"tolerance":     tolRaw,
			"error_inferences": []any{map[string]any{
				"error_type_id": "empty_response",
				"confidence":    1.0,
				"rule_version":  versionMathEquivalence,
			}},
		})
	}

	expectedValueStr, expectedUnit := splitValueUnit(strings.TrimSpace(answerExpr))

	actual, err := parseMathExpr(actualValueStr)
	if err != nil {
		// 学生作答无法解析：判错但置信度不降（Python invalid_response 同语义；
		// 解析错误文本只含位置，此处也不上抛——作答形态异常是可判定的负例）.
		return scoreZeroWithInference(map[string]any{
			"reason":        "学生作答无法解析",
			"actual_raw":    answer,
			"rules_applied": rulesApplied,
			"tolerance":     tolRaw,
			"error_inferences": []any{map[string]any{
				"error_type_id": "invalid_response",
				"confidence":    1.0,
				"rule_version":  versionMathEquivalence,
			}},
		})
	}
	expected, err := parseMathExpr(expectedValueStr)
	if err != nil {
		// 标准答案不可解析是配置错误（Python 置信度 0 结果 → Go fail-loud）.
		return registry.ScoreResult{}, fmt.Errorf("%w: math_equivalence 标准答案无法解析（%v）", ErrInvalidInput, err)
	}

	// 单位处理：双方都有单位且不同时，启用 unit_convert 则换算到 expected
	// 单位；禁用或无换算规则 → 单位不相容强制不等价（Python 同构）.
	compareActual := actual
	unitsCompatible := true
	usedConversion := false
	if actualUnit != "" && expectedUnit != "" && actualUnit != expectedUnit {
		if rules["unit_convert"] {
			converted, ok := convertUnit(actual, actualUnit, expectedUnit)
			if ok {
				compareActual = converted
				usedConversion = true
			} else {
				unitsCompatible = false
			}
		} else {
			unitsCompatible = false
		}
	}

	useTol := tol
	if !rules["decimal_tolerance"] || tol.Sign() <= 0 {
		useTol = nil
	}
	isEquiv, method := equivalentValues(compareActual, expected, useTol)
	isEquiv = isEquiv && unitsCompatible

	score := 0.0
	if isEquiv {
		score = 1.0
	}
	evidence := map[string]any{
		"actual_raw":           answer,
		"actual_normalized":    actual.RatString(),
		"expected_normalized":  expected.RatString(),
		"actual_unit":          actualUnit,
		"expected_unit":        expectedUnit,
		"used_unit_conversion": usedConversion,
		"method":               method,
		"rules_applied":        rulesApplied,
		"tolerance":            tolRaw,
		"match":                isEquiv,
	}
	if !isEquiv {
		evidence["reason"] = fmt.Sprintf("作答与标准答案不等价（method=%s）", method)
		if errorType := classifyMathMismatch(compareActual, expected, actualUnit, expectedUnit); errorType != "" {
			evidence["error_inferences"] = []any{map[string]any{
				"error_type_id": errorType,
				"confidence":    1.0,
				"rule_version":  versionMathEquivalence,
			}}
		}
	}
	blob, err := json.Marshal(evidence)
	if err != nil {
		return registry.ScoreResult{}, fmt.Errorf("scoring: math_equivalence 证据序列化失败: %w", err)
	}
	return registry.ScoreResult{
		Correct:      isEquiv,
		Score:        score,
		Confidence:   1.0, // 确定性评分器
		EvidenceJSON: string(blob),
	}, nil
}

// mathExtractValue 从作答载荷提取 (值串, 单位)：裸串（含单位后缀）/
// {value, unit} 显式分离 / 标量数字（JSON 通道）.
func mathExtractValue(resp any) (string, string) {
	switch v := resp.(type) {
	case string:
		return splitValueUnit(v)
	case map[string]any:
		if _, ok := v["value"]; ok {
			unit, _ := v["unit"].(string)
			return strings.TrimSpace(scalarString(v["value"])), strings.ToLower(unit)
		}
		return "", ""
	case nil:
		return "", ""
	default:
		return strings.TrimSpace(scalarString(v)), ""
	}
}

// scoreZeroWithInference 构造「判错但可判定」的 0 分结果（置信度不降）.
func scoreZeroWithInference(evidence map[string]any) (registry.ScoreResult, error) {
	blob, err := json.Marshal(evidence)
	if err != nil {
		return registry.ScoreResult{}, fmt.Errorf("scoring: 证据序列化失败: %w", err)
	}
	return registry.ScoreResult{
		Correct:      false,
		Score:        0.0,
		Confidence:   1.0,
		EvidenceJSON: string(blob),
	}, nil
}
