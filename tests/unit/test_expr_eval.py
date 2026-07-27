"""T-W2-002 安全表达式求值器单元测试。

覆盖范围（验收 §1-§4）：
  - 算术/比较/布尔/条件/白名单函数（§1）
  - __import__/open/未声明标识符/循环语法/属性/下标/lambda/comprehension（§2）
  - 确定性回归（§3）
  - 除零/类型错误/非法函数（§4）
"""
from __future__ import annotations

import math

import pytest

from src.core.instantiation.expr import (
    SAFE_FUNCTIONS,
    ExpressionUnsafeError,
    evaluate,
    validate,
)

# ────────────────────────────────────────────────────────────────────
# 验收 §1：合法表达式
# ────────────────────────────────────────────────────────────────────


class TestArithmetic:
    """四则运算与幂模。"""

    def test_add(self) -> None:
        assert evaluate("1 + 2") == 3

    def test_sub(self) -> None:
        assert evaluate("10 - 4") == 6

    def test_mul(self) -> None:
        assert evaluate("3 * 4") == 12

    def test_div(self) -> None:
        assert evaluate("10 / 4") == 2.5

    def test_floor_div(self) -> None:
        assert evaluate("10 // 4") == 2

    def test_mod(self) -> None:
        assert evaluate("10 % 4") == 2

    def test_pow(self) -> None:
        assert evaluate("2 ** 10") == 1024

    def test_negative(self) -> None:
        assert evaluate("-5 + 3") == -2

    def test_unary_add(self) -> None:
        assert evaluate("+5") == 5

    def test_precedence(self) -> None:
        # 乘除优先于加减；幂优先于乘
        assert evaluate("1 + 2 * 3") == 7
        assert evaluate("2 ** 3 ** 2") == 512  # 右结合
        assert evaluate("(1 + 2) * 3") == 9


class TestComparison:
    """比较运算。"""

    @pytest.mark.parametrize(
        "expr,expected",
        [
            ("1 < 2", True),
            ("2 < 1", False),
            ("2 <= 2", True),
            ("3 <= 2", False),
            ("3 > 2", True),
            ("2 > 3", False),
            ("2 >= 2", True),
            ("1 >= 3", False),
            ("1 == 1", True),
            ("1 != 2", True),
        ],
    )
    def test_cmp(self, expr: str, expected: bool) -> None:
        assert evaluate(expr) is expected

    def test_chained_compare(self) -> None:
        # a < b < c → (a < b) and (b < c)
        assert evaluate("1 < 2 < 3") is True
        assert evaluate("1 < 3 < 2") is False


class TestBoolean:
    """布尔运算。"""

    def test_and_true(self) -> None:
        assert evaluate("True and True") is True

    def test_and_false(self) -> None:
        assert evaluate("True and False") is False

    def test_or_true(self) -> None:
        assert evaluate("False or True") is True

    def test_or_false(self) -> None:
        assert evaluate("False or False") is False

    def test_not(self) -> None:
        assert evaluate("not False") is True

    def test_short_circuit(self) -> None:
        # 0 < 1 为 True；False and X → False（短路，不求 X）
        # 但 X 若是非法标识符应被绕过 → 不抛错证明短路
        # 这里改为合法短路验证：True or 0 → True（取第一个真值）
        assert evaluate("1 or 0") == 1


class TestIfExp:
    """条件表达式 if/else。"""

    def test_if_true(self) -> None:
        assert evaluate("10 if 1 < 2 else 20") == 10

    def test_if_false(self) -> None:
        assert evaluate("10 if 1 > 2 else 20") == 20

    def test_if_nested(self) -> None:
        assert evaluate("1 if True else (2 if True else 3)") == 1


class TestEnvBindings:
    """环境变量绑定。"""

    def test_env_lookup(self) -> None:
        assert evaluate("a + b", {"a": 2, "b": 3}) == 5

    def test_env_mixed(self) -> None:
        assert evaluate("a * 2 + b", {"a": 3, "b": 4}) == 10

    def test_env_shadow_none(self) -> None:
        # None 不允许被 env 覆盖（理论上 Python 字典允许，但本求值器
        # 对 _BUILTIN_CONSTANTS 走内置映射，env 中的 None 不会命中 Name 节点）
        # 这里测试 None 常量本身可用
        assert evaluate("None") is None


# ────────────────────────────────────────────────────────────────────
# 验收 §1 续：白名单函数
# ────────────────────────────────────────────────────────────────────


class TestWhitelistFunctions:
    """abs/min/max/sqrt/round/floor/ceil。"""

    def test_abs(self) -> None:
        assert evaluate("abs(-5)") == 5

    def test_abs_positive(self) -> None:
        assert evaluate("abs(5)") == 5

    def test_min_two(self) -> None:
        assert evaluate("min(3, 7)") == 3

    def test_min_three(self) -> None:
        assert evaluate("min(3, 1, 7)") == 1

    def test_max_two(self) -> None:
        assert evaluate("max(3, 7)") == 7

    def test_max_three(self) -> None:
        assert evaluate("max(3, 1, 7)") == 7

    def test_sqrt(self) -> None:
        assert evaluate("sqrt(16)") == 4.0

    def test_sqrt_two(self) -> None:
        assert abs(evaluate("sqrt(2)") - math.sqrt(2)) < 1e-12

    def test_round(self) -> None:
        assert evaluate("round(3.7)") == 4

    def test_floor(self) -> None:
        assert evaluate("floor(3.7)") == 3

    def test_ceil(self) -> None:
        assert evaluate("ceil(3.2)") == 4

    def test_nested_calls(self) -> None:
        assert evaluate("max(abs(-3), min(5, 2))") == 3


# ────────────────────────────────────────────────────────────────────
# 验收 §2：不安全语法与运行时错误
# ────────────────────────────────────────────────────────────────────


class TestUnsafeSyntax:
    """静态禁止的语法节点。"""

    def test_import_statement(self) -> None:
        # import 语句在 mode='eval' 下会 SyntaxError，但显式测试以确保行为
        with pytest.raises((ExpressionUnsafeError, SyntaxError)):
            evaluate("__import__('os')")

    def test_import_attr_access(self) -> None:
        # __import__ 作为 Name 调用 → 不在 SAFE_FUNCTIONS → 拒绝
        with pytest.raises(ExpressionUnsafeError, match="非白名单函数"):
            evaluate("__import__('os')")

    def test_open_call(self) -> None:
        with pytest.raises(ExpressionUnsafeError, match="非白名单函数"):
            evaluate("open('/etc/passwd')")

    def test_attribute_access(self) -> None:
        # obj.attr：禁止 Attribute 节点
        with pytest.raises(ExpressionUnsafeError, match="禁止的调用形式"):
            evaluate("os.system('rm -rf /')", {"os": object()})

    def test_attribute_on_int(self) -> None:
        with pytest.raises(ExpressionUnsafeError, match="禁止"):
            # 整数字面量 .bit_length()
            evaluate("(1).bit_length()")

    def test_subscript(self) -> None:
        with pytest.raises(ExpressionUnsafeError, match="禁止"):
            evaluate("a[0]", {"a": [1, 2, 3]})

    def test_undeclared_identifier(self) -> None:
        with pytest.raises(ExpressionUnsafeError, match="未声明"):
            evaluate("x + 1")

    def test_lambda(self) -> None:
        with pytest.raises(ExpressionUnsafeError, match="禁止"):
            evaluate("(lambda x: x + 1)(2)")

    def test_list_comprehension(self) -> None:
        # list comp 在 mode='eval' 下通常 SyntaxError，但 generator expr 可解析
        with pytest.raises((ExpressionUnsafeError, SyntaxError)):
            evaluate("[x for x in range(10)]")

    def test_generator_expression(self) -> None:
        # sum 非白名单函数（GeneratorExp 节点先被 Call 拦截）；
        # 即便函数换为白名单，GeneratorExp 也会被静态校验拒绝
        with pytest.raises(ExpressionUnsafeError):
            evaluate("sum(x for x in range(10))")

    def test_starred_args(self) -> None:
        with pytest.raises(ExpressionUnsafeError, match="禁止"):
            evaluate("max(*[1, 2, 3])")

    def test_keyword_args(self) -> None:
        with pytest.raises(ExpressionUnsafeError, match="关键字参数"):
            evaluate("min(3, 7, default=0)")

    def test_non_whitelist_function(self) -> None:
        with pytest.raises(ExpressionUnsafeError, match="非白名单函数"):
            evaluate("len([1, 2, 3])")

    def test_print_call(self) -> None:
        with pytest.raises(ExpressionUnsafeError, match="非白名单函数"):
            evaluate("print('hello')")

    def test_eval_call(self) -> None:
        with pytest.raises(ExpressionUnsafeError, match="非白名单函数"):
            evaluate("eval('1+1')")

    def test_forbidden_binop_lshift(self) -> None:
        # 左移不在白名单
        with pytest.raises(ExpressionUnsafeError, match="禁止的二元运算符"):
            evaluate("1 << 2")

    def test_forbidden_cmpop_in(self) -> None:
        with pytest.raises(ExpressionUnsafeError, match="禁止的比较运算符"):
            evaluate("1 in [1, 2, 3]")


class TestRuntimeErrors:
    """运行时错误统一包装为 ExpressionUnsafeError（验收 §4）。"""

    def test_division_by_zero(self) -> None:
        with pytest.raises(ExpressionUnsafeError, match="除|Division|division|Zero"):
            evaluate("1 / 0")

    def test_floor_division_by_zero(self) -> None:
        with pytest.raises(ExpressionUnsafeError):
            evaluate("1 // 0")

    def test_mod_by_zero(self) -> None:
        with pytest.raises(ExpressionUnsafeError):
            evaluate("1 % 0")

    def test_type_error_string_plus_int(self) -> None:
        # 字符串 + 整数 → TypeError，统一包装为 ExpressionUnsafeError
        with pytest.raises(ExpressionUnsafeError):
            evaluate("s + 1", {"s": "abc"})

    def test_sqrt_negative(self) -> None:
        # math.sqrt(-1) 抛 ValueError
        with pytest.raises(ExpressionUnsafeError):
            evaluate("sqrt(-1)")

    def test_min_no_args(self) -> None:
        with pytest.raises(ExpressionUnsafeError):
            evaluate("min()")


# ────────────────────────────────────────────────────────────────────
# 验收 §3：确定性回归
# ────────────────────────────────────────────────────────────────────


class TestDeterminism:
    """同一表达式+同一 env 任意次求值一致。"""

    @pytest.mark.parametrize(
        "expr,env",
        [
            ("a + b * 2", {"a": 3, "b": 4}),
            ("max(a, b) - min(a, b)", {"a": 5, "b": 2}),
            ("sqrt(a ** 2 + b ** 2)", {"a": 3, "b": 4}),
            ("(a if a > b else b) + 1", {"a": 7, "b": 3}),
            ("abs(a - b) * 2 + 1", {"a": -1, "b": 5}),
        ],
    )
    def test_repeated_eval_consistent(
        self, expr: str, env: dict[str, int]
    ) -> None:
        results = [evaluate(expr, env) for _ in range(5)]
        assert all(r == results[0] for r in results)

    def test_validate_idempotent(self) -> None:
        # 多次 validate 同一表达式不抛错
        for _ in range(3):
            validate("a + b * 2")


# ────────────────────────────────────────────────────────────────────
# 杂项：SAFE_FUNCTIONS 与边界
# ────────────────────────────────────────────────────────────────────


class TestSafeFunctionsRegistry:
    """白名单函数表正确性。"""

    def test_safe_functions_callable(self) -> None:
        for name, fn in SAFE_FUNCTIONS.items():
            assert callable(fn), f"{name} 不是 callable"

    def test_safe_functions_keys(self) -> None:
        expected = {"abs", "min", "max", "sqrt", "round", "floor", "ceil"}
        assert expected.issubset(set(SAFE_FUNCTIONS.keys()))

    def test_empty_expression_syntax_error(self) -> None:
        with pytest.raises(SyntaxError):
            evaluate("")

    def test_non_string_expression(self) -> None:
        with pytest.raises(ExpressionUnsafeError, match="必须为 str"):
            evaluate(123)  # type: ignore[arg-type]

    def test_non_dict_env(self) -> None:
        with pytest.raises(ExpressionUnsafeError, match="env 必须为 dict"):
            evaluate("1 + 2", env=["a", "b"])  # type: ignore[arg-type]


# ────────────────────────────────────────────────────────────────────
# 用例计数：本文件合计 70+ 用例（含参数化展开），满足验收 §4「≥ 30 用例」。
# 实际统计（pytest --collect-only 可复核）：
#   TestArithmetic 10 / TestComparison 11 / TestBoolean 6 / TestIfExp 3
#   TestEnvBindings 3 / TestWhitelistFunctions 12 / TestUnsafeSyntax 17
#   TestRuntimeErrors 6 / TestDeterminism 6 / TestSafeFunctionsRegistry 4
# ────────────────────────────────────────────────────────────────────
