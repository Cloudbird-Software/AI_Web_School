// binops.go 承载二元/函数运算语义（对齐 Python 数值混算矩阵）：
//
//   - int/bool 参与算术按 int；+ - * 整型闭合（int64 溢出 fail-closed）；
//   - `/` 恒为真除法（int/int → float）；`//`/`%` int 语义（取底，
//     余数符号随除数）；`**` 负指数回退 float；
//   - Fraction 参与：与 int/bool/Fraction 精确有理数运算；与 float → float；
//   - Decimal 参与：与 int/bool/Decimal 运算；除法与负幂按 28 位有效
//     数字上下文舍入（ROUND_HALF_EVEN）；与 float/Fraction 混算拒绝
//     （对齐 Python TypeError）。
package expr

import (
	"fmt"
	"math"
	"math/big"
)

// errTypeOp 类型不兼容错误。
func errTypeOp(op string, a, b Value) error {
	return fmt.Errorf("unsupported operand type(s) for %s: '%s' and '%s'", op, kindName(a), kindName(b))
}

// applyBinop 应用二元运算符（不使用 eval；opName 为 Python ast 节点名）。
func applyBinop(opName string, left, right Value) (Value, error) {
	a := asArith(left)
	b := asArith(right)

	switch opName {
	case "Add":
		return binAdd(a, b)
	case "Sub":
		return binSub(a, b)
	case "Mult":
		return binMul(a, b)
	case "Div":
		return binDiv(a, b)
	case "FloorDiv":
		return binFloorDiv(a, b)
	case "Mod":
		return binMod(a, b)
	case "Pow":
		return binPow(a, b)
	default:
		return nil, unsafef("未支持的二元运算符：%s", opName)
	}
}

func binAdd(a, b Value) (Value, error) {
	if sa, ok := a.(StringValue); ok {
		sb, ok := b.(StringValue)
		if !ok {
			return nil, errTypeOp("+", a, b)
		}
		return sa + sb, nil
	}
	if la, ok := a.(ListValue); ok {
		lb, ok := b.(ListValue)
		if !ok {
			return nil, errTypeOp("+", a, b)
		}
		out := make(ListValue, 0, len(la)+len(lb))
		out = append(out, la...)
		out = append(out, lb...)
		return out, nil
	}
	return arith2(a, b, "+",
		func(x, y int64) (Value, error) { return addInt(x, y) },
		func(x, y float64) (Value, error) { return FloatValue(x + y), nil },
		func(x, y *big.Rat) (Value, error) { return ratResult(a, b, new(big.Rat).Add(x, y)), nil })
}

func binSub(a, b Value) (Value, error) {
	return arith2(a, b, "-",
		func(x, y int64) (Value, error) { return subInt(x, y) },
		func(x, y float64) (Value, error) { return FloatValue(x - y), nil },
		func(x, y *big.Rat) (Value, error) { return ratResult(a, b, new(big.Rat).Sub(x, y)), nil })
}

func binMul(a, b Value) (Value, error) {
	// 序列重复：str * int / int * str / list * int
	if sa, ok := a.(StringValue); ok {
		if n, ok := b.(IntValue); ok {
			return StringValue(repeatStr(string(sa), int(n))), nil
		}
		return nil, errTypeOp("*", a, b)
	}
	if n, ok := a.(IntValue); ok {
		if sb, ok := b.(StringValue); ok {
			return StringValue(repeatStr(string(sb), int(n))), nil
		}
		if lb, ok := b.(ListValue); ok {
			return repeatList(lb, int(n)), nil
		}
	}
	return arith2(a, b, "*",
		func(x, y int64) (Value, error) { return mulInt(x, y) },
		func(x, y float64) (Value, error) { return FloatValue(x * y), nil },
		func(x, y *big.Rat) (Value, error) { return ratResult(a, b, new(big.Rat).Mul(x, y)), nil })
}

func binDiv(a, b Value) (Value, error) {
	if isZero(b) {
		return nil, fmt.Errorf("division by zero")
	}
	// Python 真除法 `/`：int/int → float；Fraction 参与 → Fraction 精确；
	// Decimal 参与 → Decimal（28 位有效数字上下文舍入）；Decimal×float 拒绝。
	if _, aF := a.(FloatValue); aF {
		return FloatValue(toFloat(a) / toFloat(b)), nil
	}
	if _, bF := b.(FloatValue); bF {
		if isDecimalRat(a) {
			return nil, errTypeOp("/", a, b)
		}
		return FloatValue(toFloat(a) / toFloat(b)), nil
	}
	if isDecimalRat(a) || isDecimalRat(b) {
		q := new(big.Rat).Quo(exactRat(a), exactRat(b))
		return &RatValue{R: roundRatSigDigits(q, 28), IsDecimal: true}, nil
	}
	if _, aRat := a.(*RatValue); aRat {
		return &RatValue{R: new(big.Rat).Quo(exactRat(a), exactRat(b)), IsDecimal: false}, nil
	}
	if _, bRat := b.(*RatValue); bRat {
		return &RatValue{R: new(big.Rat).Quo(exactRat(a), exactRat(b)), IsDecimal: false}, nil
	}
	return FloatValue(toFloat(a) / toFloat(b)), nil
}

func binFloorDiv(a, b Value) (Value, error) {
	if isZero(b) {
		return nil, fmt.Errorf("integer division or modulo by zero")
	}
	// float 参与 → float 取底（Decimal×float 已拒绝）。
	if _, aF := a.(FloatValue); aF {
		return FloatValue(math.Floor(toFloat(a) / toFloat(b))), nil
	}
	if _, bF := b.(FloatValue); bF {
		if isDecimalRat(a) {
			return nil, errTypeOp("//", a, b)
		}
		return FloatValue(math.Floor(toFloat(a) / toFloat(b))), nil
	}
	// Decimal 参与：商向零截断（Decimal // 语义），结果 Decimal。
	if isDecimalRat(a) || isDecimalRat(b) {
		q := new(big.Rat).Quo(exactRat(a), exactRat(b))
		return &RatValue{R: new(big.Rat).SetInt(truncRat(q)), IsDecimal: true}, nil
	}
	// int / Fraction：精确取底 → int（对齐 math.floor(Fraction 商)）。
	q := new(big.Rat).Quo(exactRat(a), exactRat(b))
	m, _ := floorQuoRem(q.Num(), q.Denom())
	return IntValue(m.Int64()), nil
}

func binMod(a, b Value) (Value, error) {
	if isZero(b) {
		return nil, fmt.Errorf("integer division or modulo by zero")
	}
	// float 参与（Decimal×float 已拒绝）。
	if _, aF := a.(FloatValue); aF {
		return FloatValue(pythonFmod(toFloat(a), toFloat(b))), nil
	}
	if _, bF := b.(FloatValue); bF {
		if isDecimalRat(a) {
			return nil, errTypeOp("%", a, b)
		}
		return FloatValue(pythonFmod(toFloat(a), toFloat(b))), nil
	}
	// Decimal：% 符号随被除数（截断语义），结果 Decimal。
	if isDecimalRat(a) || isDecimalRat(b) {
		ra := exactRat(a)
		rb := exactRat(b)
		q := truncRat(new(big.Rat).Quo(ra, rb))
		r := new(big.Rat).Sub(ra, new(big.Rat).Mul(new(big.Rat).SetInt(q), rb))
		return &RatValue{R: r, IsDecimal: true}, nil
	}
	// int / Fraction：% 取底，符号随除数（r = a - b * floor(a / b)）。
	ra := exactRat(a)
	rb := exactRat(b)
	floorQ := new(big.Rat).Quo(ra, rb)
	fq, _ := floorQuoRem(floorQ.Num(), floorQ.Denom())
	r := new(big.Rat).Sub(ra, new(big.Rat).Mul(rb, new(big.Rat).SetInt(fq)))
	if _, aRat := a.(*RatValue); aRat {
		return &RatValue{R: r, IsDecimal: false}, nil
	}
	if _, bRat := b.(*RatValue); bRat {
		return &RatValue{R: r, IsDecimal: false}, nil
	}
	return ratFromRatToInt(r), nil
}

func binPow(a, b Value) (Value, error) {
	// float 参与：结果 float（Fraction×float → float；Decimal×float → 拒绝）。
	if _, aF := a.(FloatValue); aF {
		return powFloat(toFloat(a), toFloat(b))
	}
	if _, bF := b.(FloatValue); bF {
		if ra, ok := a.(*RatValue); ok && !ra.IsDecimal {
			return powFloat(toFloat(a), toFloat(b))
		}
		return nil, errTypeOp("**", a, b)
	}
	// 指数必须为整数（int/bool/整值 rational）；非整值 rational 幂在
	// Python 中按 float 降级，DSL 子集外 fail-closed（已知边界）。
	be := exactRat(b)
	if !be.IsInt() || !be.Num().IsInt64() {
		return nil, errTypeOp("**", a, b)
	}
	exp := be.Num().Int64()
	if ra, ok := a.(*RatValue); ok {
		base := new(big.Rat).Set(ra.R)
		neg := false
		if exp < 0 {
			if base.Sign() == 0 {
				return nil, fmt.Errorf("zero cannot be raised to a negative power")
			}
			if exp == math.MinInt64 {
				return nil, fmt.Errorf("整数幂溢出（fail-closed）")
			}
			neg = true
			exp = -exp
		}
		res := ratPow(base, exp)
		if neg {
			res = new(big.Rat).Inv(res)
			if ra.IsDecimal {
				res = roundRatSigDigits(res, 28)
			}
		}
		return &RatValue{R: res, IsDecimal: ra.IsDecimal}, nil
	}
	// 整数幂：负指数 → float；非负 → int（溢出 fail-closed）。
	ai := exactRat(a)
	if !ai.Num().IsInt64() {
		return nil, fmt.Errorf("整数底超出 int64（fail-closed）")
	}
	base := ai.Num().Int64()
	if exp < 0 {
		// 0 的负指数幂已在 isZero 检查前由 powFloat 拒绝；±1 底恒定。
		if base == 1 {
			return FloatValue(1), nil
		}
		if base == -1 {
			return FloatValue(math.Pow(-1, float64(exp))), nil
		}
		return powFloat(float64(base), float64(exp))
	}
	return powInt(base, exp)
}

// ────────────────────────────────────────────────────────────────────
// 辅助
// ────────────────────────────────────────────────────────────────────

// arith2 数值二元分派：整数闭合、float 提升、有理数精确。
// ratOp 仅在两侧兼容（Decimal 只与 int/bool/Decimal；Fraction 只与
// int/bool/Fraction）时调用。
func arith2(a, b Value, op string, intOp func(x, y int64) (Value, error), floatOp func(x, y float64) (Value, error), ratOp func(x, y *big.Rat) (Value, error)) (Value, error) {
	if !isNumeric(a) || !isNumeric(b) {
		return nil, errTypeOp(op, a, b)
	}
	_, aFloat := a.(FloatValue)
	_, bFloat := b.(FloatValue)
	ra, aRat := a.(*RatValue)
	rb, bRat := b.(*RatValue)
	switch {
	case aFloat || bFloat:
		// Decimal 与 float 混算拒绝（对齐 Python TypeError）。
		if (aRat && ra.IsDecimal) || (bRat && rb.IsDecimal) {
			return nil, errTypeOp(op, a, b)
		}
		return floatOp(toFloat(a), toFloat(b))
	case aRat || bRat:
		if aRat && bRat && ra.IsDecimal != rb.IsDecimal {
			return nil, errTypeOp(op, a, b)
		}
		return ratOp(exactRat(a), exactRat(b))
	default:
		return intOp(exactRat(a).Num().Int64(), exactRat(b).Num().Int64())
	}
}

// ratResult 依左侧语义标签决定结果 flavor（int 左侧提升为右侧 flavor）。
func ratResult(a, b Value, r *big.Rat) Value {
	if ra, ok := a.(*RatValue); ok {
		return &RatValue{R: r, IsDecimal: ra.IsDecimal}
	}
	if rb, ok := b.(*RatValue); ok {
		return &RatValue{R: r, IsDecimal: rb.IsDecimal}
	}
	if isExact(a) && isExact(b) {
		// int 与 int 不会进入 ratOp；仅防御。
		return &RatValue{R: r, IsDecimal: false}
	}
	return &RatValue{R: r, IsDecimal: false}
}

func ratFromRatToInt(r *big.Rat) Value {
	if r.IsInt() {
		return IntValue(r.Num().Int64())
	}
	return &RatValue{R: r, IsDecimal: false}
}

func addInt(x, y int64) (Value, error) {
	s := x + y
	if (x > 0 && y > 0 && s < 0) || (x < 0 && y < 0 && s >= 0) {
		return nil, fmt.Errorf("整数加法溢出 int64（fail-closed）")
	}
	return IntValue(s), nil
}

func subInt(x, y int64) (Value, error) {
	s := x - y
	if (x >= 0 && y < 0 && s < 0) || (x < 0 && y > 0 && s > 0) {
		return nil, fmt.Errorf("整数减法溢出 int64（fail-closed）")
	}
	return IntValue(s), nil
}

func mulInt(x, y int64) (Value, error) {
	if x == 0 || y == 0 {
		return IntValue(0), nil
	}
	p := x * y
	if p/y != x {
		return nil, fmt.Errorf("整数乘法溢出 int64（fail-closed）")
	}
	return IntValue(p), nil
}

func powInt(base, exp int64) (Value, error) {
	if base == 0 || base == 1 {
		return IntValue(base), nil
	}
	if base == -1 {
		return IntValue(1 - 2*(exp%2)), nil
	}
	if exp > 62 {
		return nil, fmt.Errorf("整数幂溢出 int64（fail-closed）")
	}
	res := int64(1)
	b := base
	e := exp
	for e > 0 {
		if e&1 == 1 {
			v, err := mulInt(res, b)
			if err != nil {
				return nil, err
			}
			res = int64(v.(IntValue))
		}
		e >>= 1
		if e > 0 {
			v, err := mulInt(b, b)
			if err != nil {
				return nil, err
			}
			b = int64(v.(IntValue))
		}
	}
	return IntValue(res), nil
}

func powFloat(x, y float64) (Value, error) {
	if y < 0 && x == 0 {
		return nil, fmt.Errorf("zero cannot be raised to a negative power")
	}
	if x < 0 && y != math.Trunc(y) {
		return nil, fmt.Errorf("负底的负指数幂产生 complex（DSL 子集外，fail-closed）")
	}
	return FloatValue(math.Pow(x, y)), nil
}

// ratPow 精确有理数幂（exp ≥ 0）。
func ratPow(base *big.Rat, exp int64) *big.Rat {
	res := big.NewRat(1, 1)
	b := new(big.Rat).Set(base)
	for e := exp; e > 0; e >>= 1 {
		if e&1 == 1 {
			res.Mul(res, b)
		}
		e2 := e >> 1
		if e2 > 0 {
			b.Mul(b, b)
		}
		e = e2
	}
	return res
}

// truncRat 返回向零截断的整数值。
func truncRat(r *big.Rat) *big.Int {
	q, _ := new(big.Int).QuoRem(r.Num(), r.Denom(), new(big.Int))
	return q
}

// pythonFmod 对齐 Python float % 语义：余数符号随除数。
func pythonFmod(a, b float64) float64 {
	r := math.Mod(a, b)
	if r != 0 && (r < 0) != (b < 0) {
		r += b
	}
	return r
}

// repeatStr 对齐 Python str * int（n ≤ 0 → 空串）。
func repeatStr(s string, n int) string {
	if n <= 0 {
		return ""
	}
	out := make([]byte, 0, len(s)*n)
	for range n {
		out = append(out, s...)
	}
	return string(out)
}

func repeatList(l ListValue, n int) ListValue {
	if n <= 0 {
		return ListValue{}
	}
	out := make(ListValue, 0, len(l)*n)
	for range n {
		out = append(out, l...)
	}
	return out
}
