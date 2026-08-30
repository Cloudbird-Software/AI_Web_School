// funcs.go 承载白名单函数表（对齐冻结实现 SAFE_FUNCTIONS：
// abs/min/max/sqrt/round/floor/ceil；学科函数库经 env 扩展点注入，
// 不污染本表——调用侧仍只允许直调本表）。
package expr

import (
	"fmt"
	"math"
	"math/big"
)

// callSafeFunction 调用白名单函数（参数已求值）。
func callSafeFunction(name string, args []Value) (Value, error) {
	switch name {
	case "abs":
		return fnAbs(args)
	case "min":
		return fnMinMax(true, args)
	case "max":
		return fnMinMax(false, args)
	case "sqrt":
		return fnSqrt(args)
	case "round":
		return fnRound(args)
	case "floor":
		return fnFloorCeil(true, args)
	case "ceil":
		return fnFloorCeil(false, args)
	default:
		return nil, fmt.Errorf("非白名单函数 %q", name)
	}
}

func needArgs(name string, args []Value, min, max int) error {
	n := len(args)
	if n < min || (max >= 0 && n > max) {
		return fmt.Errorf("%s() 参数个数 %d 不在 [%d, %d]", name, n, min, max)
	}
	return nil
}

// fnAbs 绝对值：int→int、float→float、rationals→同 flavor、bool→int。
func fnAbs(args []Value) (Value, error) {
	if err := needArgs("abs", args, 1, 1); err != nil {
		return nil, err
	}
	switch x := asArith(args[0]).(type) {
	case IntValue:
		if x == math.MinInt64 {
			return nil, fmt.Errorf("abs() 溢出 int64（fail-closed）")
		}
		if x < 0 {
			return -x, nil
		}
		return x, nil
	case FloatValue:
		return FloatValue(math.Abs(float64(x))), nil
	case *RatValue:
		return &RatValue{R: new(big.Rat).Abs(x.R), IsDecimal: x.IsDecimal}, nil
	default:
		return nil, fmt.Errorf("abs() 不支持类型 %s", kindName(args[0]))
	}
}

// fnMinMax min/max：单 iterable 参数（list）或 ≥2 个标量。
// 比较走 CompareValues（数值混合精确比较；同类型 str 亦可）。
func fnMinMax(isMin bool, args []Value) (Value, error) {
	candidates := args
	if len(args) == 1 {
		lst, ok := args[0].(ListValue)
		if !ok {
			return nil, fmt.Errorf("min/max() 单参数必须为 list（对齐 Python iterable 约定）")
		}
		if len(lst) == 0 {
			return nil, fmt.Errorf("min/max() iterable 为空")
		}
		candidates = lst
	}
	if len(candidates) == 0 {
		return nil, fmt.Errorf("min/max() 至少需要一个参数")
	}
	best := candidates[0]
	for _, c := range candidates[1:] {
		ok, err := CompareValues(map[bool]string{true: "lt", false: "gt"}[isMin], c, best)
		if err != nil {
			return nil, err
		}
		if ok {
			best = c
		}
	}
	return best, nil
}

// fnSqrt 平方根：int/float/rationals → float；负数 → math domain error。
func fnSqrt(args []Value) (Value, error) {
	if err := needArgs("sqrt", args, 1, 1); err != nil {
		return nil, err
	}
	v := asArith(args[0])
	switch v.(type) {
	case IntValue, FloatValue, *RatValue:
	default:
		return nil, fmt.Errorf("sqrt() 不支持类型 %s", kindName(args[0]))
	}
	f := toFloat(v)
	if f < 0 {
		return nil, fmt.Errorf("math domain error")
	}
	return FloatValue(math.Sqrt(f)), nil
}

// fnRound round(x[, n])：银行家舍入（半舍到偶，精确有理数判定）。
//   - int/bool：原值返回（无论 n）；
//   - float：无 n → int；有 n → float（精确二进制值上舍入后回 float）；
//   - Fraction：无 n → int；有 n → Fraction（精确 10^-n 网格）；
//   - Decimal：无 n → int；有 n → Decimal。
func fnRound(args []Value) (Value, error) {
	if err := needArgs("round", args, 1, 2); err != nil {
		return nil, err
	}
	x := args[0]
	hasN := len(args) == 2
	ndigits := Value(NoneValue{})
	if hasN {
		ndigits = args[1]
		if _, isNone := ndigits.(NoneValue); isNone {
			hasN = false
		}
	}
	n := int64(0)
	if hasN {
		nv := asArith(ndigits)
		iv, ok := nv.(IntValue)
		if !ok {
			return nil, fmt.Errorf("round() 的 ndigits 必须为整数")
		}
		n = int64(iv)
	}
	if hasN && (n > 1024 || n < -1024) {
		return nil, fmt.Errorf("round() 的 ndigits 超出 [-1024, 1024]（fail-closed）")
	}
	switch v := asArith(x).(type) {
	case IntValue:
		return v, nil
	case *RatValue:
		if !hasN {
			return ratInt64(roundHalfEvenRat(v.R), x)
		}
		scaled := new(big.Rat).Mul(v.R, ratFromInt10(int(n)))
		res := new(big.Rat).SetInt(roundHalfEvenRat(scaled))
		res.Mul(res, ratFromInt10(int(-n)))
		return &RatValue{R: res, IsDecimal: v.IsDecimal}, nil
	case FloatValue:
		r := floatToRat(float64(v))
		if !hasN {
			return ratInt64(roundHalfEvenRat(r), x)
		}
		scaled := new(big.Rat).Mul(r, ratFromInt10(int(n)))
		res := new(big.Rat).SetInt(roundHalfEvenRat(scaled))
		res.Mul(res, ratFromInt10(int(-n)))
		f, _ := res.Float64()
		return FloatValue(f), nil
	default:
		return nil, fmt.Errorf("round() 不支持类型 %s", kindName(x))
	}
}

// ratInt64 把 big.Int 转 IntValue（越界 fail-closed）。
func ratInt64(m *big.Int, origin Value) (Value, error) {
	if !m.IsInt64() {
		return nil, fmt.Errorf("round() 结果溢出 int64（fail-closed）")
	}
	_ = origin
	return IntValue(m.Int64()), nil
}

// fnFloorCeil floor/ceil：int/float/rationals → int。
func fnFloorCeil(isFloor bool, args []Value) (Value, error) {
	name := "ceil"
	if isFloor {
		name = "floor"
	}
	if err := needArgs(name, args, 1, 1); err != nil {
		return nil, err
	}
	switch v := asArith(args[0]).(type) {
	case IntValue:
		return v, nil
	case FloatValue:
		f := toFloat(v)
		m := math.Floor(f)
		if !isFloor {
			m = math.Ceil(f)
		}
		if m < -9.223372036854776e18 || m >= 9.223372036854776e18 {
			return nil, fmt.Errorf("%s() 结果溢出 int64（fail-closed）", name)
		}
		return IntValue(int64(m)), nil
	case *RatValue:
		m, _ := floorQuoRem(v.R.Num(), v.R.Denom())
		if !isFloor && v.R.Denom().Cmp(big.NewInt(1)) != 0 {
			m.Add(m, big.NewInt(1))
		}
		return ratInt64(m, args[0])
	default:
		return nil, fmt.Errorf("%s() 不支持类型 %s", name, kindName(args[0]))
	}
}
