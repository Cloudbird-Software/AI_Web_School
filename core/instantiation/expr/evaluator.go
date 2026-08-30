// evaluator.go 承载静态校验与递归求值（Python 冻结基准
// src/core/instantiation/expr/evaluator.py 的语义移植）。
package expr

import (
	"fmt"
	"math"
	"math/big"
	"sort"
)

// ────────────────────────────────────────────────────────────────────
// 错误类型
// ────────────────────────────────────────────────────────────────────

// SyntaxError 表达式本身无法解析（对应冻结实现透传的 Python SyntaxError）。
type SyntaxError struct {
	Msg  string
	Line int
	Col  int
}

func (e *SyntaxError) Error() string {
	if e.Line > 0 {
		return fmt.Sprintf("expr: 语法错误（%d:%d）: %s", e.Line, e.Col, e.Msg)
	}
	return fmt.Sprintf("expr: 语法错误: %s", e.Msg)
}

// UnsafeError 表达式不安全或求值失败时返回。
//
// 覆盖三类场景（对齐冻结实现 ExpressionUnsafeError）：
//   - 静态不安全：含 import/loop/attribute/subscript/lambda 等禁节点
//   - 名字不安全：未声明的标识符、非白名单函数调用
//   - 运行时不安全：除零、类型错误、值错误等被包装为本错误
type UnsafeError struct {
	Msg string
}

func (e *UnsafeError) Error() string { return "expr: " + e.Msg }

func unsafef(format string, args ...any) error {
	return &UnsafeError{Msg: fmt.Sprintf(format, args...)}
}

// ────────────────────────────────────────────────────────────────────
// 白名单
// ────────────────────────────────────────────────────────────────────

// SafeFunctionName 报告白名单函数名集合（对齐冻结实现 SAFE_FUNCTIONS）。
var safeFunctionNames = []string{"abs", "min", "max", "sqrt", "round", "floor", "ceil"}

// IsSafeFunction 名是否在白名单函数表内。
func IsSafeFunction(name string) bool {
	for _, n := range safeFunctionNames {
		if n == name {
			return true
		}
	}
	return false
}

// ────────────────────────────────────────────────────────────────────
// 静态校验
// ────────────────────────────────────────────────────────────────────

// Validate 静态校验表达式是否安全（不执行）。
func Validate(expression string) error {
	ast, err := parseExpr(expression)
	if err != nil {
		return err
	}
	return validateNode(ast)
}

func validateNode(n ExprNode) error {
	switch x := n.(type) {
	case *Constant, *Name:
		return nil
	case *BinOp:
		if !x.Allowed {
			return unsafef("禁止的二元运算符：%s", x.OpName)
		}
		if err := validateNode(x.X); err != nil {
			return err
		}
		return validateNode(x.Y)
	case *UnaryOp:
		if !x.Allowed {
			return unsafef("禁止的一元运算符：%s", x.OpName)
		}
		return validateNode(x.X)
	case *BoolOp:
		for _, v := range x.Values {
			if err := validateNode(v); err != nil {
				return err
			}
		}
		return nil
	case *Compare:
		for _, op := range x.Ops {
			if !op.allowed {
				return unsafef("禁止的比较运算符：%s", op.name)
			}
		}
		if err := validateNode(x.Left); err != nil {
			return err
		}
		for _, c := range x.Comparators {
			if err := validateNode(c); err != nil {
				return err
			}
		}
		return nil
	case *IfExp:
		if err := validateNode(x.Test); err != nil {
			return err
		}
		if err := validateNode(x.Body); err != nil {
			return err
		}
		return validateNode(x.Orelse)
	case *Call:
		name, ok := x.Func.(*Name)
		if !ok {
			return unsafef("禁止的调用形式：%s（仅允许白名单函数直调）", nodeKindName(x.Func))
		}
		if !IsSafeFunction(name.ID) {
			return unsafef("调用非白名单函数：%q", name.ID)
		}
		if x.HasKw {
			return unsafef("禁止使用关键字参数")
		}
		if x.Starred {
			return unsafef("禁止使用 *args 解包")
		}
		for _, arg := range x.Args {
			if err := validateNode(arg); err != nil {
				return err
			}
		}
		return nil
	default:
		// Attribute/Subscript/Forbidden 等一律拒绝：涵盖 Import/ImportFrom、
		// For/While、FunctionDef/ClassDef/Lambda、推导式、JoinedStr 等。
		return unsafef("禁止的语法节点：%s", nodeKindName(n))
	}
}

// Names 提取表达式引用的变量名集合（升序去重；对应冻结实现
// ast.walk 提取 Name 节点的口径）。语法错误时返回空集合（对齐
// _extract_referenced_slots 对 SyntaxError 返回空集的行为）。
func Names(expression string) ([]string, error) {
	ast, err := parseExpr(expression)
	if err != nil {
		return nil, nil //nolint:nilerr // 对齐冻结实现：解析失败视为空引用集
	}
	seen := map[string]bool{}
	var walk func(n ExprNode)
	walk = func(n ExprNode) {
		switch x := n.(type) {
		case *Name:
			seen[x.ID] = true
		case *BinOp:
			walk(x.X)
			walk(x.Y)
		case *UnaryOp:
			walk(x.X)
		case *BoolOp:
			for _, v := range x.Values {
				walk(v)
			}
		case *Compare:
			walk(x.Left)
			for _, c := range x.Comparators {
				walk(c)
			}
		case *IfExp:
			walk(x.Test)
			walk(x.Body)
			walk(x.Orelse)
		case *Call:
			walk(x.Func)
			for _, a := range x.Args {
				walk(a)
			}
		case *Attribute:
			walk(x.Obj)
		case *Subscript:
			walk(x.Obj)
		}
	}
	walk(ast)
	out := make([]string, 0, len(seen))
	for k := range seen {
		out = append(out, k)
	}
	sort.Strings(out)
	return out, nil
}

// ────────────────────────────────────────────────────────────────────
// 求值
// ────────────────────────────────────────────────────────────────────

// Evaluate 安全求值表达式（确定性：同一 (expression, env) 任意次求值
// 结果一致；浮点遵循 IEEE 754，不做额外随机化）。
//
// env 为变量绑定（槽值）；nil 等价空 env。env 值类型经 ToValue 归一，
// 不支持的类型 fail-closed 报错。
func Evaluate(expression string, env map[string]any) (Value, error) {
	vals, err := ToEnvValue(env)
	if err != nil {
		return nil, err
	}
	ast, err := parseExpr(expression)
	if err != nil {
		return nil, err
	}
	if err := validateNode(ast); err != nil {
		return nil, err
	}
	return evalNode(ast, vals)
}

func evalNode(n ExprNode, env map[string]Value) (Value, error) {
	switch x := n.(type) {
	case *Constant:
		return x.Val, nil
	case *Name:
		v, ok := env[x.ID]
		if !ok {
			return nil, unsafef("未声明的标识符：%q", x.ID)
		}
		return v, nil
	case *BinOp:
		left, err := evalNode(x.X, env)
		if err != nil {
			return nil, err
		}
		right, err := evalNode(x.Y, env)
		if err != nil {
			return nil, err
		}
		res, err := applyBinop(x.OpName, left, right)
		if err != nil {
			return nil, unsafef("二元运算 %s 失败：%v", x.OpName, err)
		}
		return res, nil
	case *UnaryOp:
		operand, err := evalNode(x.X, env)
		if err != nil {
			return nil, err
		}
		res, err := applyUnary(x.OpName, operand)
		if err != nil {
			return nil, unsafef("一元运算 %s 失败：%v", x.OpName, err)
		}
		return res, nil
	case *BoolOp:
		var err error
		if x.IsAnd {
			var res Value = BoolValue(true)
			for _, v := range x.Values {
				res, err = evalNode(v, env)
				if err != nil {
					return nil, err
				}
				if !IsTruthy(res) {
					return res, nil
				}
			}
			return res, nil
		}
		var res Value = BoolValue(false)
		for _, v := range x.Values {
			res, err = evalNode(v, env)
			if err != nil {
				return nil, err
			}
			if IsTruthy(res) {
				return res, nil
			}
		}
		return res, nil
	case *Compare:
		left, err := evalNode(x.Left, env)
		if err != nil {
			return nil, err
		}
		for i, op := range x.Ops {
			right, err := evalNode(x.Comparators[i], env)
			if err != nil {
				return nil, err
			}
			ok, err := applyCompare(op, left, right)
			if err != nil {
				return nil, unsafef("比较 %s 失败：%v", op.name, err)
			}
			if !ok {
				return BoolValue(false), nil
			}
			left = right
		}
		return BoolValue(true), nil
	case *IfExp:
		test, err := evalNode(x.Test, env)
		if err != nil {
			return nil, err
		}
		if IsTruthy(test) {
			return evalNode(x.Body, env)
		}
		return evalNode(x.Orelse, env)
	case *Call:
		name := x.Func.(*Name).ID
		args := make([]Value, 0, len(x.Args))
		for _, a := range x.Args {
			v, err := evalNode(a, env)
			if err != nil {
				return nil, err
			}
			args = append(args, v)
		}
		res, err := callSafeFunction(name, args)
		if err != nil {
			return nil, unsafef("调用 %s() 失败：%v", name, err)
		}
		return res, nil
	default:
		// 静态校验应已拒绝所有其他节点
		return nil, unsafef("求值阶段遇到未支持节点：%s（应已被 Validate 拦截）", nodeKindName(n))
	}
}

func applyCompare(op compareOp, left, right Value) (bool, error) {
	if !op.allowed {
		return false, unsafef("禁止的比较运算符：%s", op.name)
	}
	switch op.name {
	case "Eq":
		return ValuesEqual(left, right), nil
	case "NotEq":
		return !ValuesEqual(left, right), nil
	default:
		canonical, ok := map[string]string{
			"Lt": "lt", "LtE": "le", "Gt": "gt", "GtE": "ge",
		}[op.name]
		if !ok {
			return false, fmt.Errorf("未支持的比较运算符：%s", op.name)
		}
		return CompareValues(canonical, left, right)
	}
}

func applyUnary(opName string, operand Value) (Value, error) {
	v := asArith(operand)
	switch opName {
	case "Not":
		return BoolValue(!IsTruthy(operand)), nil
	case "UAdd":
		switch v.(type) {
		case IntValue, FloatValue, *RatValue:
			return v, nil
		default:
			return nil, fmt.Errorf("不支持的操作数类型 %s", kindName(operand))
		}
	case "USub":
		switch x := v.(type) {
		case IntValue:
			if x == math.MinInt64 {
				return nil, fmt.Errorf("整数取负溢出 int64（fail-closed）")
			}
			return -x, nil
		case FloatValue:
			return -x, nil
		case *RatValue:
			return &RatValue{R: new(big.Rat).Neg(x.R), IsDecimal: x.IsDecimal}, nil
		default:
			return nil, fmt.Errorf("不支持的操作数类型 %s", kindName(operand))
		}
	default:
		return nil, fmt.Errorf("未支持的一元运算符：%s", opName)
	}
}
