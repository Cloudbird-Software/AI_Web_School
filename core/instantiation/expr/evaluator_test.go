// evaluator_test.go 安全表达式求值器的验收测试。
//
// 测试策略（对齐冻结基准 src/core/instantiation/expr/evaluator.py 的验收）：
//  1. 表驱动求值：期望值为冻结 Python 实跑产出（math.sqrt/round/floor/ceil
//     与 Decimal/Fraction 语义同源），含优先级/链式比较/短路/三元/白名单函数。
//  2. 错误路径 fail-closed：除零、未知变量、类型错、非白名单调用、
//     禁用语法节点（Attribute/Subscript/List/Lambda/f-string）。
//  3. 确定性：同一 (expression, env) 并发求值必得同一结果（-race）。
//  4. Names 提取：变体引擎 objective 依赖检测的地面真值。
package expr

import (
	"encoding/json"
	"fmt"
	"sync"
	"testing"
)

// evalTable 求值正例（期望值与冻结 Python 3.11 实跑逐项核对）。
func evalTable() []struct {
	expr string
	env  map[string]any
	want Value
} {
	dec := func(s string) Value {
		v, err := NewDecimal(s)
		if err != nil {
			panic(err)
		}
		return v
	}
	return []struct {
		expr string
		env  map[string]any
		want Value
	}{
		// 整数算术（黄金样例口径：a + b）
		{"a + b", map[string]any{"a": 3, "b": 4}, IntValue(7)},
		{"a - b", map[string]any{"a": 3, "b": 4}, IntValue(-1)},
		{"a * b", map[string]any{"a": 3, "b": 4}, IntValue(12)},
		{"7 // 2", nil, IntValue(3)},
		{"-7 // 2", nil, IntValue(-4)},
		{"-7 % 3", nil, IntValue(2)},
		{"7 % -3", nil, IntValue(-2)},
		{"2 ** 10", nil, IntValue(1024)},
		{"2 ** -1", nil, FloatValue(0.5)},
		{"-2 ** 2", nil, IntValue(-4)}, // 幂优先于一元负号
		{"2 ** 3 ** 2", nil, IntValue(512)},
		{"1 + 2 * 3", nil, IntValue(7)},
		{"(1 + 2) * 3", nil, IntValue(9)},
		// 浮点（IEEE 754）
		{"7 / 2", nil, FloatValue(3.5)},
		{"1 / 3", nil, FloatValue(1.0 / 3.0)},
		{"2.5 + 0.5", nil, FloatValue(3.0)},
		{"sqrt(a * a + b * b)", map[string]any{"a": 3, "b": 4}, FloatValue(5.0)},
		// 白名单函数
		{"abs(-5)", nil, IntValue(5)},
		{"abs(-2.5)", nil, FloatValue(2.5)},
		{"min(3, 1, 2)", nil, IntValue(1)},
		{"max(3, 1, 2)", nil, IntValue(3)},
		{"min(l)", map[string]any{"l": []any{4, 2, 3}}, IntValue(2)}, // env 注入 list（单 iterable）
		{"floor(3.7)", nil, IntValue(3)},
		{"ceil(3.2)", nil, IntValue(4)},
		{"floor(-3.2)", nil, IntValue(-4)},
		{"ceil(-3.8)", nil, IntValue(-3)},
		{"round(2.5)", nil, IntValue(2)}, // 银行家舍入（Python 实跑）
		{"round(3.5)", nil, IntValue(4)},
		{"round(2.675, 2)", nil, FloatValue(2.67)}, // 二进制表示偏低的经典例
		{"round(x, 1)", map[string]any{"x": 5}, IntValue(5)},
		// 比较与链式
		{"1 < 2 < 3", nil, BoolValue(true)},
		{"1 < 2 > 3", nil, BoolValue(false)},
		{"3 >= 3 == 3", nil, BoolValue(true)},
		{"a + 1 == b", map[string]any{"a": 3, "b": 4}, BoolValue(true)},
		{`"a" < "b"`, nil, BoolValue(true)},
		{"1 == 1.0", nil, BoolValue(true)},
		{`1 != "1"`, nil, BoolValue(true)}, // 避免字符串冒充数值
		// 布尔短路（返回操作数值，对齐 Python）
		{"not 0", nil, BoolValue(true)},
		{"not 3", nil, BoolValue(false)},
		{"1 and 2", nil, IntValue(2)},
		{"0 and 2", nil, IntValue(0)},
		{`0 or "a"`, nil, StringValue("a")},
		{`"x" and 1.5`, nil, FloatValue(1.5)},
		// 三元
		{"x if x > 0 else -x", map[string]any{"x": -4}, IntValue(4)},
		{"x if x > 0 else -x", map[string]any{"x": 4}, IntValue(4)},
		// 字符串
		{`"ab" + "cd"`, nil, StringValue("abcd")},
		{`'单引号' + "双引号"`, nil, StringValue("单引号双引号")},
		{`"ab" * 3`, nil, StringValue("ababab")},
		{`"line\n"`, nil, StringValue("line\n")},
		{`r"raw\n"`, nil, StringValue(`raw\n`)},
		// True/False/None 常量（bool 是 int 子类）
		{"True + 1", nil, IntValue(2)},
		{"True and 1", nil, IntValue(1)},
		// Decimal 语义（引擎 eval_env：decimal 槽 → Decimal）
		{"a + b", map[string]any{"a": dec("3.10"), "b": dec("0.05")}, dec("3.15")},
		{"a / 3", map[string]any{"a": dec("1")}, dec("0.3333333333333333333333333333")},
		{"a * 2", map[string]any{"a": dec("1.5")}, dec("3.0")},
		// Fraction 语义（引擎 eval_env：fraction 槽 → Fraction）
		{"f + 1", map[string]any{"f": NewFraction(3, 4)}, NewFraction(7, 4)},
		{"f / 2", map[string]any{"f": NewFraction(3, 4)}, NewFraction(3, 8)},
		{"f * f", map[string]any{"f": NewFraction(3, 4)}, NewFraction(9, 16)},
		{"f // 1", map[string]any{"f": NewFraction(7, 2)}, IntValue(3)},
	}
}

func TestEvaluateTable(t *testing.T) {
	for _, tc := range evalTable() {
		got, err := Evaluate(tc.expr, tc.env)
		if err != nil {
			t.Errorf("Evaluate(%q) 意外错误: %v", tc.expr, err)
			continue
		}
		if !ValuesEqual(got, tc.want) {
			t.Errorf("Evaluate(%q) = %v, 期望 %v", tc.expr, got, tc.want)
		}
	}
}

// evalErrorTable 错误路径（fail-closed）。
func evalErrorTable() []struct {
	expr string
	env  map[string]any
	// wantSyntax 为 true 表示对齐 Python SyntaxError（语法层拒绝），
	// 否则应得 *UnsafeError（语义层拒绝）。
	wantSyntax bool
} {
	return []struct {
		expr       string
		env        map[string]any
		wantSyntax bool
	}{
		// 运行时不安全：除零 / 类型错
		{"1 / 0", nil, false},
		{"1 // 0", nil, false},
		{"1 % 0", nil, false},
		{"a / 0", map[string]any{"a": 3}, false},
		{`"a" + 1`, nil, false},
		{`"a" + "b" * "c"`, nil, false},
		{"abs(\"x\")", nil, false},
		{"sqrt(0 - 1)", nil, false},      // math domain error
		{"min(1, \"a\")", nil, false},    // 顺序未定义
		{"-\"x\"", nil, false},           // 一元负号类型错
		{"unknown_var + 1", nil, false},  // 未声明标识符
		{"round(1.5, 2, 3)", nil, false}, // 参数个数
		{"2 ** 2 ** 100", nil, false},    // 整数幂溢出 fail-closed
		{"9223372036854775807 + 1", nil, false},
		{"99999999999999999999", nil, true}, // int64 表示域外
		// 名字不安全：非白名单调用
		{"__import__('os')", nil, false},
		{"eval('1')", nil, false},
		{"open('x')", nil, false},
		// 静态不安全：禁用节点
		{"a.b", nil, false},         // Attribute
		{"a[0]", nil, false},        // Subscript
		{"[1, 2]", nil, false},      // List
		{"(1, 2)", nil, false},      // Tuple
		{"{'k': 1}", nil, false},    // Dict
		{"lambda x: x", nil, false}, // Lambda
		{"f(x=1)", nil, false},      // 关键字参数
		{"abs(*[1])", nil, false},   // *args
		{"a | b", nil, false},       // BitOr
		{"a & b", nil, false},       // BitAnd
		{"~a", nil, false},          // BitNot
		{"1 in [1]", nil, false},    // In
		{"a is None", nil, false},   // Is
		{`f"{'a'}"`, nil, false},    // JoinedStr
		{"a if b", nil, true},       // 缺 else
		{"1 +", nil, true},          // 截断
		{"(1", nil, true},           // 未闭合
		{"", nil, true},             // 空
		{"a = 1", nil, true},        // 赋值语句
		{"1 2", nil, true},          // 相邻字面量
	}
}

func TestEvaluateErrorPaths(t *testing.T) {
	for _, tc := range evalErrorTable() {
		// 先走 Validate 面：语法错误 → SyntaxError；语义拒绝 → UnsafeError。
		verr := Validate(tc.expr)
		_, ee := Evaluate(tc.expr, tc.env)
		if verr == nil && ee == nil {
			t.Errorf("Evaluate(%q) 期望失败，实际成功", tc.expr)
			continue
		}
		for _, err := range []error{verr, ee} {
			if err == nil {
				continue
			}
			_, isSyntax := err.(*SyntaxError)
			_, isUnsafe := err.(*UnsafeError)
			if tc.wantSyntax && !isSyntax {
				t.Errorf("Validate(%q) 期望 SyntaxError，实际 %T: %v", tc.expr, err, err)
			}
			if !tc.wantSyntax && !isUnsafe {
				t.Errorf("Evaluate(%q) 期望 UnsafeError，实际 %T: %v", tc.expr, err, err)
			}
		}
	}
}

func TestEvaluateDeterministicConcurrent(t *testing.T) {
	exprStr := "sqrt(a * a + b * b) + round(x, 2)"
	env := map[string]any{"a": 3, "b": 4, "x": 1.5}
	first, err := Evaluate(exprStr, env)
	if err != nil {
		t.Fatalf("首次求值失败: %v", err)
	}
	var wg sync.WaitGroup
	results := make([]Value, 32)
	errs := make([]error, 32)
	for i := range results {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			results[i], errs[i] = Evaluate(exprStr, env)
		}(i)
	}
	wg.Wait()
	for i := range results {
		if errs[i] != nil {
			t.Fatalf("并发求值 [%d] 失败: %v", i, errs[i])
		}
		if String(results[i]) != String(first) {
			t.Fatalf("并发求值 [%d] = %v, 期望 %v（D3 确定性破坏）", i, results[i], first)
		}
	}
}

func TestValidateAcceptsGoldenExpressions(t *testing.T) {
	// 黄金样例（tests/golden/instantiation/*.yaml）中的表达式全集。
	for _, e := range []string{"a + b", "sqrt(a * a + b * b)", "first_num", "a + b + 1", "a * a + b * b"} {
		if err := Validate(e); err != nil {
			t.Errorf("Validate(%q) 意外拒绝: %v", e, err)
		}
	}
}

func TestNames(t *testing.T) {
	cases := []struct {
		expr string
		want []string
	}{
		{"a + b", []string{"a", "b"}},
		{"sqrt(a * a + b * b)", []string{"a", "b", "sqrt"}}, // ast.walk 含函数名（冻结口径）
		{"first_num", []string{"first_num"}},
		{"x if x > 0 else -x", []string{"x"}},
		{"min(a, b) + c", []string{"a", "b", "c", "min"}},
		// 语法错误 → 空引用集（对齐 _extract_referenced_slots）
		{"a +", nil},
	}
	for _, tc := range cases {
		got, err := Names(tc.expr)
		if err != nil {
			t.Errorf("Names(%q) 错误: %v", tc.expr, err)
			continue
		}
		if tc.want == nil {
			if len(got) != 0 {
				t.Errorf("Names(%q) = %v, 期望空", tc.expr, got)
			}
			continue
		}
		if fmt.Sprint(got) != fmt.Sprint(tc.want) {
			t.Errorf("Names(%q) = %v, 期望 %v", tc.expr, got, tc.want)
		}
	}
}

func TestEvaluateEnvTypeFailClosed(t *testing.T) {
	// env 值类型不支持 → fail-closed。
	if _, err := Evaluate("a + 1", map[string]any{"a": struct{}{}}); err == nil {
		t.Errorf("env 传结构体期望失败，实际成功")
	}
	// json.Number 整数字面量 → int（对齐 Python json.load 分派）。
	v, err := Evaluate("n + 1", map[string]any{"n": json.Number("3")})
	if err != nil {
		t.Fatalf("json.Number 求值失败: %v", err)
	}
	if v != IntValue(4) {
		t.Errorf("json.Number(\"3\") + 1 = %v, 期望 4", v)
	}
	// 溢出的整数字面量 → 拒绝。
	if _, err := Evaluate("n + 1", map[string]any{"n": json.Number("99999999999999999999")}); err == nil {
		t.Errorf("溢出 json.Number 期望失败")
	}
}

func TestValuesEqualCrossType(t *testing.T) {
	dec1, _ := NewDecimal("1")
	cases := []struct {
		a, b Value
		want bool
	}{
		{IntValue(1), FloatValue(1.0), true},
		{BoolValue(true), IntValue(1), true},
		{dec1, FloatValue(1.0), true},          // Decimal('1') == 1.0（Python 实跑）
		{dec1, NewFraction(1, 1), true},        // Decimal('1') == Fraction(1)
		{StringValue("1"), IntValue(1), false}, // 字符串不冒充数值
		{NoneValue{}, NoneValue{}, true},
		{NoneValue{}, IntValue(0), false},
		{ListValue{IntValue(1)}, ListValue{FloatValue(1.0)}, true},
	}
	for i, tc := range cases {
		if got := ValuesEqual(tc.a, tc.b); got != tc.want {
			t.Errorf("case %d: ValuesEqual(%v, %v) = %v, 期望 %v", i, tc.a, tc.b, got, tc.want)
		}
	}
}
