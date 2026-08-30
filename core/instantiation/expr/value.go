// Package expr 承载安全表达式求值器（Python 冻结基准
// src/core/instantiation/expr/evaluator.py 的 Go 移植；T-W2-002）。
//
// DSL answer_program 与 distractor_rules 使用的纯函数表达式求值：
// 无 IO、无循环、无递归调用、无属性访问、无 import。函数调用只能命中
// 白名单（SafeFunctions：abs/min/max/sqrt/round/floor/ceil）。
//
// 与冻结实现的口径对齐：
//   - 数值模型：IntValue↔Python int（int64 有界，溢出 fail-closed——
//     冻结实现是任意精度整数，本移植在 2^63 边界显式拒绝，宁拒不歧义）；
//     FloatValue↔Python float（IEEE 754 双精度）；RatValue 承载 Python
//     的 Decimal（IsDecimal=true，除法按 28 位有效数字上下文舍入）与
//     Fraction（IsDecimal=false，精确有理数）；Decimal 与 Fraction/float
//     混算按 Python 语义拒绝（TypeError→求值失败）。
//   - fail-closed：除零、未知变量、类型错、非白名单调用、禁用语法节点
//     一律返回 UnsafeError；语法错误返回 SyntaxError。
//
// 宪法 X6：本包不 import 任何学科/学段包。
package expr

import (
	"encoding/json"
	"fmt"
	"math"
	"math/big"
	"strconv"
	"strings"
)

// Value 是表达式求值域的值接口（Python 任意值的安全子集）。
type Value interface {
	isValue()
}

// NoneValue 对应 Python None。
type NoneValue struct{}

// BoolValue 对应 Python bool（算术中按 int 参与：True+1==2）。
type BoolValue bool

// IntValue 对应 Python int（int64 有界；溢出 fail-closed）。
type IntValue int64

// FloatValue 对应 Python float（IEEE 754 双精度）。
type FloatValue float64

// RatValue 承载 Python 的 Decimal（IsDecimal=true）与 Fraction
// （IsDecimal=false），底层为精确有理数。
type RatValue struct {
	R         *big.Rat
	IsDecimal bool
}

// StringValue 对应 Python str。
type StringValue string

// ListValue 对应 Python list。
type ListValue []Value

func (NoneValue) isValue()   {}
func (BoolValue) isValue()   {}
func (IntValue) isValue()    {}
func (FloatValue) isValue()  {}
func (*RatValue) isValue()   {}
func (StringValue) isValue() {}
func (ListValue) isValue()   {}

// ────────────────────────────────────────────────────────────────────
// 构造器（供引擎装配 eval env 使用）
// ────────────────────────────────────────────────────────────────────

// NewDecimal 从十进制字面量字符串（如 "3.14"、"-0.75"）构造 Decimal 语义
// 的 RatValue（对应 Python Decimal(str(value))）。
func NewDecimal(s string) (*RatValue, error) {
	r, ok := new(big.Rat).SetString(s)
	if !ok || !isDecimalLiteral(s) {
		return nil, fmt.Errorf("expr: 非法十进制字面量 %q", s)
	}
	return &RatValue{R: r, IsDecimal: true}, nil
}

// NewFraction 构造 Fraction 语义的 RatValue（对应 Python Fraction(num, den)）。
func NewFraction(num, den int64) *RatValue {
	return &RatValue{R: big.NewRat(num, den), IsDecimal: false}
}

// isDecimalLiteral 判断 s 是否为纯十进制字面量（拒绝 big.Rat.SetString
// 额外接受的 "3/4" 分数形式——Decimal(str(value)) 不会走分数路径）。
func isDecimalLiteral(s string) bool {
	if s == "" {
		return false
	}
	i := 0
	if s[0] == '+' || s[0] == '-' {
		i++
	}
	digits, dots := 0, 0
	for ; i < len(s); i++ {
		switch c := s[i]; {
		case c >= '0' && c <= '9':
			digits++
		case c == '.':
			dots++
			if dots > 1 {
				return false
			}
		case c == 'e' || c == 'E':
			return isDecimalExponent(s[i+1:])
		default:
			return false
		}
	}
	return digits > 0
}

func isDecimalExponent(s string) bool {
	if s == "" {
		return false
	}
	i := 0
	if s[0] == '+' || s[0] == '-' {
		i++
	}
	digits := 0
	for ; i < len(s); i++ {
		if s[i] < '0' || s[i] > '9' {
			return false
		}
		digits++
	}
	return digits > 0
}

// ToValue 把 Go 原生值转换为求值域 Value（对应 Python env 直接持有原生值）。
// 支持：nil、bool、整型族、float64、json.Number（整数字面量→Int，
// 其余→Float，与 Python json.load 的 int/float 分派一致）、string、
// *big.Rat（Fraction 语义）、Value 原样、[]any（逐元素递归）。
func ToValue(v any) (Value, error) {
	switch x := v.(type) {
	case nil:
		return NoneValue{}, nil
	case Value:
		return x, nil
	case bool:
		return BoolValue(x), nil
	case int:
		return IntValue(x), nil
	case int8:
		return IntValue(x), nil
	case int16:
		return IntValue(x), nil
	case int32:
		return IntValue(x), nil
	case int64:
		return IntValue(x), nil
	case uint:
		return intFromUint(uint64(x))
	case uint8:
		return intFromUint(uint64(x))
	case uint16:
		return intFromUint(uint64(x))
	case uint32:
		return intFromUint(uint64(x))
	case uint64:
		return intFromUint(x)
	case float32:
		return FloatValue(x), nil
	case float64:
		return FloatValue(x), nil
	case json.Number:
		return fromJSONNumber(string(x))
	case string:
		return StringValue(x), nil
	case *big.Rat:
		return &RatValue{R: new(big.Rat).Set(x)}, nil
	case []any:
		out := make(ListValue, 0, len(x))
		for i, e := range x {
			ev, err := ToValue(e)
			if err != nil {
				return nil, fmt.Errorf("expr: env 列表元素 [%d]: %w", i, err)
			}
			out = append(out, ev)
		}
		return out, nil
	default:
		return nil, fmt.Errorf("expr: env 值类型不支持 %T（fail-closed）", v)
	}
}

func intFromUint(u uint64) (Value, error) {
	if u > math.MaxInt64 {
		return nil, fmt.Errorf("expr: 整数溢出 int64：%d（fail-closed）", u)
	}
	return IntValue(u), nil
}

// fromJSONNumber 按 Python json.load 的口径分派：整数字面量→int，
// 带小数/指数→float。
func fromJSONNumber(s string) (Value, error) {
	if _, ok := new(big.Int).SetString(s, 10); ok {
		v, err := strconvInt64(s)
		if err != nil {
			return nil, err
		}
		return v, nil
	}
	f, err := json.Number(s).Float64()
	if err != nil {
		return nil, fmt.Errorf("expr: 非法数字字面量 %q", s)
	}
	return FloatValue(f), nil
}

func strconvInt64(s string) (Value, error) {
	if s == "" {
		return nil, fmt.Errorf("expr: 非法整数字面量 %q", s)
	}
	var v int64
	for i := 0; i < len(s); i++ {
		if s[i] < '0' || s[i] > '9' {
			return nil, fmt.Errorf("expr: 非法整数字面量 %q", s)
		}
		d := int64(s[i] - '0')
		if v > (math.MaxInt64-d)/10 {
			return nil, fmt.Errorf("expr: 整数溢出 int64：%s（fail-closed）", s)
		}
		v = v*10 + d
	}
	return IntValue(v), nil
}

// ToEnvValue 把 env 字典整体转换（键不变，值走 ToValue）。
func ToEnvValue(env map[string]any) (map[string]Value, error) {
	out := make(map[string]Value, len(env))
	for k, v := range env {
		val, err := ToValue(v)
		if err != nil {
			return nil, fmt.Errorf("expr: env[%q]: %w", k, err)
		}
		out[k] = val
	}
	return out, nil
}

// ────────────────────────────────────────────────────────────────────
// 真值 / 精确数值辅助
// ────────────────────────────────────────────────────────────────────

// IsTruthy 对应 Python bool(x)：None/False/0/0.0/""/空列表为假。
func IsTruthy(v Value) bool {
	switch x := v.(type) {
	case NoneValue:
		return false
	case BoolValue:
		return bool(x)
	case IntValue:
		return x != 0
	case FloatValue:
		return x != 0
	case *RatValue:
		return x.R.Sign() != 0
	case StringValue:
		return len(x) > 0
	case ListValue:
		return len(x) > 0
	default:
		return true
	}
}

// isExact 值是否可无损转有理数（int/bool/rationals）。
func isExact(v Value) bool {
	switch v.(type) {
	case IntValue, BoolValue, *RatValue:
		return true
	default:
		return false
	}
}

// exactRat 把精确值转为 *big.Rat（调用方保证 isExact(v)）。
func exactRat(v Value) *big.Rat {
	switch x := v.(type) {
	case IntValue:
		return big.NewRat(int64(x), 1)
	case BoolValue:
		if x {
			return big.NewRat(1, 1)
		}
		return new(big.Rat)
	case *RatValue:
		return new(big.Rat).Set(x.R)
	default:
		return new(big.Rat)
	}
}

// asArith 返回参与算术运算的值：bool 按 Python 语义提升为 int。
func asArith(v Value) Value {
	if b, ok := v.(BoolValue); ok {
		if b {
			return IntValue(1)
		}
		return IntValue(0)
	}
	return v
}

// toFloat 把任意数值转 float64（Python 算术的混合提升口径）。
func toFloat(v Value) float64 {
	switch x := v.(type) {
	case IntValue:
		return float64(x)
	case BoolValue:
		if x {
			return 1
		}
		return 0
	case FloatValue:
		return float64(x)
	case *RatValue:
		f, _ := x.R.Float64()
		return f
	default:
		return math.NaN()
	}
}

// isNumeric 值是否为数值（含 bool——Python bool 是 int 子类）。
func isNumeric(v Value) bool {
	switch v.(type) {
	case IntValue, BoolValue, FloatValue, *RatValue:
		return true
	default:
		return false
	}
}

// isZero 除数是否精确为零（Python 除零对 int/float/Decimal/Fraction
// 一律抛 ZeroDivisionError/InvalidOperation，不产 Inf）。
func isZero(v Value) bool {
	switch x := v.(type) {
	case IntValue:
		return x == 0
	case BoolValue:
		return !bool(x)
	case FloatValue:
		return x == 0
	case *RatValue:
		return x.R.Sign() == 0
	default:
		return false
	}
}

// ────────────────────────────────────────────────────────────────────
// 相等与比较
// ────────────────────────────────────────────────────────────────────

// ValuesEqual 宽松相等（对齐 Python ==，CPython 3.2+ 数值交叉比较
// 按精确值：1 == 1.0 == True；Decimal('1') == 1.0；Decimal('0.1') != 0.1
// 因 float 0.1 非精确 1/10）：
//   - 精确值（int/bool/Fraction/Decimal）之间转精确有理数比较；
//   - 含 float 时另一侧转精确二进制有理数比较（对齐 CPython 精确比较）；
//   - str 只与 str 相等；None 只与 None 相等（"避免字符串冒充数值"纪律）；
//   - list 逐元素；类型不兼容返回 false 而非错误（对应 Python == 不抛错）。
func ValuesEqual(a, b Value) bool {
	if isNumeric(a) && isNumeric(b) {
		_, aFloat := a.(FloatValue)
		_, bFloat := b.(FloatValue)
		switch {
		case aFloat && bFloat:
			return toFloat(a) == toFloat(b)
		case aFloat:
			return exactRat(b).Cmp(floatToRat(toFloat(a))) == 0
		case bFloat:
			return exactRat(a).Cmp(floatToRat(toFloat(b))) == 0
		default:
			return exactRat(a).Cmp(exactRat(b)) == 0
		}
	}
	switch x := a.(type) {
	case NoneValue:
		_, ok := b.(NoneValue)
		return ok
	case StringValue:
		y, ok := b.(StringValue)
		return ok && string(x) == string(y)
	case ListValue:
		y, ok := b.(ListValue)
		if !ok || len(x) != len(y) {
			return false
		}
		for i := range x {
			if !ValuesEqual(x[i], y[i]) {
				return false
			}
		}
		return true
	default:
		return false
	}
}

func isDecimalRat(v Value) bool {
	r, ok := v.(*RatValue)
	return ok && r.IsDecimal
}

// CompareValues 应用 < <= > >=（对齐 Python 3.2+ 数值交叉排序比较：
// int/float/Fraction/Decimal 之间均可按精确值比较；str 只与 str；
// 类型不兼容报错）。op 取 "lt"/"le"/"gt"/"ge"。
func CompareValues(op string, a, b Value) (bool, error) {
	as, aOK := a.(StringValue)
	bs, bOK := b.(StringValue)
	if aOK && bOK {
		return cmpResult(op, compareStrings(string(as), string(bs))), nil
	}
	if aOK != bOK {
		return false, fmt.Errorf("%s 与 %s 之间未定义顺序", kindName(a), kindName(b))
	}
	if _, ok := a.(NoneValue); ok {
		// Python 对 None 排序直接 TypeError。
		return false, fmt.Errorf("None 之间未定义顺序")
	}
	if !isNumeric(a) || !isNumeric(b) {
		return false, fmt.Errorf("%s 与 %s 之间未定义顺序", kindName(a), kindName(b))
	}
	_, aFloat := a.(FloatValue)
	_, bFloat := b.(FloatValue)
	switch {
	case aFloat && bFloat:
		return cmpResult(op, compareFloats(toFloat(a), toFloat(b))), nil
	case aFloat:
		return cmpResult(op, exactRat(b).Cmp(floatToRat(toFloat(a)))), nil
	case bFloat:
		return cmpResult(op, exactRat(a).Cmp(floatToRat(toFloat(b)))), nil
	default:
		return cmpResult(op, exactRat(a).Cmp(exactRat(b))), nil
	}
}

func compareStrings(a, b string) int {
	switch {
	case a < b:
		return -1
	case a > b:
		return 1
	default:
		return 0
	}
}

func compareFloats(a, b float64) int {
	switch {
	case a < b:
		return -1
	case a > b:
		return 1
	default:
		return 0
	}
}

func cmpResult(op string, c int) bool {
	switch op {
	case "lt":
		return c < 0
	case "le":
		return c <= 0
	case "gt":
		return c > 0
	case "ge":
		return c >= 0
	default:
		return false
	}
}

// kindName 返回值的 Python 类型名（错误信息可读性）。
func kindName(v Value) string {
	switch v.(type) {
	case NoneValue:
		return "None"
	case BoolValue:
		return "bool"
	case IntValue:
		return "int"
	case FloatValue:
		return "float"
	case *RatValue:
		return "number"
	case StringValue:
		return "str"
	case ListValue:
		return "list"
	default:
		return "unknown"
	}
}

// ────────────────────────────────────────────────────────────────────
// 有理数工具
// ────────────────────────────────────────────────────────────────────

var bigFive = big.NewInt(5)

func floatToRat(f float64) *big.Rat {
	r := new(big.Rat)
	if r.SetFloat64(f) == nil {
		return new(big.Rat)
	}
	return r
}

// floorQuoRem 欧几里得除法：q = floor(n/d)，r ∈ [0, d)（d > 0）。
func floorQuoRem(n, d *big.Int) (*big.Int, *big.Int) {
	q, r := new(big.Int).QuoRem(n, d, new(big.Int))
	if r.Sign() < 0 {
		r.Add(r, d)
		q.Sub(q, big.NewInt(1))
	}
	return q, r
}

// roundHalfEvenRat 对精确有理数做银行家舍入（Python round 语义）。
func roundHalfEvenRat(r *big.Rat) *big.Int {
	q, rem := floorQuoRem(r.Num(), r.Denom())
	twice := new(big.Int).Lsh(rem, 1)
	switch twice.Cmp(r.Denom()) {
	case 1:
		q.Add(q, big.NewInt(1))
	case 0:
		if q.Bit(0) == 1 {
			q.Add(q, big.NewInt(1))
		}
	}
	return q
}

// ratFromInt10 返回 10^e 的有理数（e 可负）。
func ratFromInt10(e int) *big.Rat {
	if e >= 0 {
		p := new(big.Int).Exp(big.NewInt(10), big.NewInt(int64(e)), nil)
		return new(big.Rat).SetInt(p)
	}
	p := new(big.Int).Exp(big.NewInt(10), big.NewInt(int64(-e)), nil)
	return new(big.Rat).SetFrac(big.NewInt(1), p)
}

// roundRatSigDigits 把有理数舍入到 sig 位有效数字（对齐 Python Decimal
// 默认 28 位精度上下文的除法/开方口径；舍入模式 ROUND_HALF_EVEN）。
func roundRatSigDigits(r *big.Rat, sig int) *big.Rat {
	if r.Sign() == 0 {
		return new(big.Rat)
	}
	e := decimalExponent(r)
	scale := sig - 1 - e
	scaled := new(big.Rat).Mul(r, ratFromInt10(scale))
	m := roundHalfEvenRat(scaled)
	res := new(big.Rat).SetInt(m)
	return res.Mul(res, ratFromInt10(-scale))
}

// decimalExponent 返回 floor(log10(|r|))，要求 r ≠ 0。
// 位数估计给出 e 的候选带 {e-1, e}（num/den 的位数上下界夹逼）。
func decimalExponent(r *big.Rat) int {
	a := len(new(big.Int).Abs(r.Num()).String())
	b := len(new(big.Int).Abs(r.Denom()).String())
	e := a - b
	if r.Cmp(ratFromInt10(e)) >= 0 {
		return e
	}
	return e - 1
}

// RatDecimalString 把有理数渲染为不带指数记号的最短十进制定点字符串
// （对齐 format(Decimal(x).normalize(), 'f')：去尾零、无 E 记号；
// 非有限小数 fail-closed）。供引擎的 decimal 槽规范化复用。
func RatDecimalString(r *big.Rat) (string, error) { return ratDecimalString(r) }

// ratDecimalString 把有理数渲染为不带指数记号的最短十进制定点字符串
// （对齐 format(Decimal(x).normalize(), 'f')：去尾零、无 E 记号；
// 非有限小数 fail-closed）。
func ratDecimalString(r *big.Rat) (string, error) {
	zero := new(big.Rat)
	if r.Cmp(zero) == 0 {
		return "0", nil
	}
	// 有限小数 ⇔ 最简分母为 2^a·5^b；小数位数 = max(a, b)。
	den := new(big.Int).Set(r.Denom())
	v2, v5 := 0, 0
	for den.Bit(0) == 0 {
		den.Rsh(den, 1)
		v2++
	}
	m5 := new(big.Int).Mod(den, bigFive)
	for m5.Sign() == 0 {
		den.Div(den, bigFive)
		v5++
		m5.Mod(den, bigFive)
	}
	if den.Cmp(big.NewInt(1)) != 0 {
		return "", fmt.Errorf("expr: 值 %s 不是有限小数（fail-closed）", r.RatString())
	}
	places := v2
	if v5 > places {
		places = v5
	}
	scaled := new(big.Int).Mul(r.Num(), pow10Big(places))
	scaled.Div(scaled, r.Denom()) // 精确整除：den 的 2/5 因子数 ≤ places
	s := scaled.String()
	neg := false
	if s[0] == '-' {
		neg = true
		s = s[1:]
	}
	var out string
	if places == 0 {
		out = s
	} else {
		for len(s) <= places {
			s = "0" + s
		}
		out = s[:len(s)-places] + "." + s[len(s)-places:]
	}
	if neg {
		out = "-" + out
	}
	return out, nil
}

func pow10Big(e int) *big.Int {
	return new(big.Int).Exp(big.NewInt(10), big.NewInt(int64(e)), nil)
}

// ────────────────────────────────────────────────────────────────────
// 字符串化（对齐 Python str()/repr 的可读形态）
// ────────────────────────────────────────────────────────────────────

// String 返回值的 Python 风格可读表示（str 口径：True/False/None 首字母大写、
// Fraction 为 "n/d"、Decimal 为最短十进制定点）。
func String(v Value) string {
	switch x := v.(type) {
	case NoneValue:
		return "None"
	case BoolValue:
		if x {
			return "True"
		}
		return "False"
	case IntValue:
		return strconv.FormatInt(int64(x), 10)
	case FloatValue:
		return strconv.FormatFloat(float64(x), 'g', -1, 64)
	case *RatValue:
		if x.IsDecimal {
			if s, err := ratDecimalString(x.R); err == nil {
				return s
			}
			return x.R.RatString()
		}
		return x.R.RatString()
	case StringValue:
		return string(x)
	case ListValue:
		parts := make([]string, 0, len(x))
		for _, e := range x {
			parts = append(parts, repr(e))
		}
		return "[" + strings.Join(parts, ", ") + "]"
	default:
		return fmt.Sprintf("%v", v)
	}
}

// repr 返回 Python repr 口径（字符串带引号）。
func repr(v Value) string {
	if s, ok := v.(StringValue); ok {
		return "'" + string(s) + "'"
	}
	return String(v)
}
