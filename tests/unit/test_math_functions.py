"""tests/unit/test_math_functions.py — T-W2-025 验收单测

覆盖任务卡三条验收标准：
  §1 variable_types.py 定义 5 类型 + 规范化与相等比较
  §2 functions.py 实现 ≥15 函数 + docstring + 类型标注
  §3 全量单测覆盖每个函数的边界与错误输入
  §4 不 import 核心域未暴露的模块（学科零特判反向：学科包不私接核心域）

实现策略：用 importlib 把 variable-types/variable_types.py 与 functions.py
当独立模块加载，避免 pyproject.toml packages 注册（目录名带连字符无法作为
Python 包名）。functions.py 内部已用 importlib 加载 variable_types.py。
"""
from __future__ import annotations

import importlib.util
import inspect
import math
import sys
from decimal import Decimal as PyDecimal
from fractions import Fraction as PyFraction
from pathlib import Path

import pytest

# ────────────────────────────────────────────────────────────────────
# 加载被测模块（连字符目录无法用普通 import）
# ────────────────────────────────────────────────────────────────────
_ROOT = Path(__file__).parent.parent.parent
_VT_PATH = (
    _ROOT
    / "src"
    / "packs"
    / "subject-math"
    / "variable-types"
    / "variable_types.py"
)
_FN_PATH = (
    _ROOT
    / "src"
    / "packs"
    / "subject-math"
    / "variable-types"
    / "functions.py"
)


def _load_module(path: Path, name: str):
    """以 importlib 加载 .py 文件为独立模块。

    关键：若 name 已存在于 sys.modules（如 functions.py 加载 variable_types 时
    注册的实例），则直接复用该实例——保证 isinstance 检查一致。
    """
    # 已加载则复用，避免双重加载导致 isinstance 检查失败
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载 {path}")
    mod = importlib.util.module_from_spec(spec)
    # 注册到 sys.modules（必须在 exec_module 之前，避免递归 import 时重复加载）
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


# 关键：模块名必须与 functions.py 中 _VT_MODULE_NAME 一致，
# 这样测试加载的 variable_types 与 functions.py 内部加载的是同一实例，
# isinstance 检查才能通过。
_VT_MODULE_NAME = "subject_math_variable_types"

# 先加载 variable_types 并注册到 sys.modules，
# 随后加载 functions.py 时会直接复用该实例（不再二次加载）。
vt = _load_module(_VT_PATH, _VT_MODULE_NAME)
fn = _load_module(_FN_PATH, "subject_math_functions_under_test")

MathInteger = vt.MathInteger
Fraction = vt.Fraction
Decimal = vt.Decimal
Quantity = vt.Quantity
Interval = vt.Interval


# ────────────────────────────────────────────────────────────────────
# 验收 §1：variable_types.py 5 类型 + 规范化 + 相等比较
# ────────────────────────────────────────────────────────────────────
class TestMathInteger:
    def test_construct_from_int(self):
        m = MathInteger(42)
        assert m.value == 42
        assert str(m) == "42"

    def test_construct_from_string_with_whitespace(self):
        m = MathInteger("  123  ")
        assert m.value == 123

    def test_construct_big_integer_no_drift(self):
        """大整数无浮点漂移（IEEE 754 双精度无法精确表示 2^60）。"""
        big = "123456789012345678901234567890"
        m = MathInteger(big)
        assert m.value == 123456789012345678901234567890

    def test_negative(self):
        assert MathInteger(-5).value == -5
        assert MathInteger("-5").value == -5

    def test_rejects_bool(self):
        with pytest.raises(TypeError, match="bool"):
            MathInteger(True)

    def test_rejects_float(self):
        with pytest.raises(TypeError, match="int 或 str"):
            MathInteger(3.14)  # type: ignore[arg-type]

    def test_rejects_empty_string(self):
        with pytest.raises(ValueError, match="空字符串"):
            MathInteger("")

    def test_rejects_non_numeric_string(self):
        with pytest.raises(ValueError, match="无法解析"):
            MathInteger("abc")

    def test_equality_int_and_mathinteger(self):
        assert MathInteger(7) == 7
        assert MathInteger(7) == MathInteger(7)
        assert MathInteger(7) != MathInteger(8)

    def test_hashable(self):
        s = {MathInteger(1), MathInteger(1), MathInteger(2)}
        assert len(s) == 2

    def test_normalized_returns_self(self):
        m = MathInteger(99)
        assert m.normalized() is m


class TestFraction:
    def test_construct_basic(self):
        f = Fraction(1, 2)
        assert f.numerator == 1
        assert f.denominator == 2
        assert str(f) == "1/2"

    def test_auto_reduce(self):
        f = Fraction(4, 8)
        assert f.numerator == 1
        assert f.denominator == 2

    def test_sign_normalized_to_denominator(self):
        """分母为正、符号在分子。"""
        f = Fraction(1, -2)
        assert f.numerator == -1
        assert f.denominator == 2
        assert str(f) == "-1/2"

    def test_zero_fraction(self):
        f = Fraction(0, 5)
        assert f.numerator == 0
        assert f.denominator == 1
        assert str(f) == "0"

    def test_integer_like_fraction_str(self):
        """分母为 1 时字符串表现为整数。"""
        assert str(Fraction(5, 1)) == "5"

    def test_default_denominator_one(self):
        f = Fraction(7)
        assert f.denominator == 1

    def test_rejects_zero_denominator(self):
        with pytest.raises(ZeroDivisionError, match="分母不能为零"):
            Fraction(1, 0)

    def test_rejects_bool(self):
        with pytest.raises(TypeError, match="bool"):
            Fraction(True, 1)  # type: ignore[arg-type]

    def test_rejects_non_numeric(self):
        with pytest.raises(ValueError, match="无法解析"):
            Fraction("abc", 1)  # type: ignore[arg-type]

    def test_equality_after_reduction(self):
        """1/2 == 2/4 == 3/6。"""
        assert Fraction(1, 2) == Fraction(2, 4)
        assert Fraction(1, 2) == Fraction(3, 6)
        assert Fraction(1, 2) != Fraction(1, 3)

    def test_equality_with_int(self):
        assert Fraction(6, 1) == 6
        assert Fraction(0, 5) == 0

    def test_hash_consistency(self):
        assert hash(Fraction(1, 2)) == hash(Fraction(2, 4))


class TestDecimal:
    def test_construct_from_string(self):
        d = Decimal("3.14")
        assert d.value == PyDecimal("3.14")

    def test_construct_from_decimal(self):
        d = Decimal(PyDecimal("0.5"))
        assert d.value == PyDecimal("0.5")

    def test_rejects_float_directly(self):
        with pytest.raises(TypeError, match="禁止直接接收 float"):
            Decimal(3.14)  # type: ignore[arg-type]

    def test_rejects_empty_string(self):
        with pytest.raises(ValueError, match="空字符串"):
            Decimal("")

    def test_rejects_non_numeric(self):
        with pytest.raises(ValueError, match="无法解析"):
            Decimal("abc")

    def test_normalized_strips_trailing_zeros(self):
        """0.50 → 0.5；前导零也去除。"""
        assert Decimal("0.50").normalized() == Decimal("0.5")
        assert Decimal("00.5").normalized() == Decimal("0.5")

    def test_normalized_negative_zero(self):
        """-0 规范化为 0。"""
        assert Decimal("-0").normalized() == Decimal("0")

    def test_equality_after_normalization(self):
        """0.5 == 0.50 == 0.500。"""
        assert Decimal("0.5") == Decimal("0.50")
        assert Decimal("0.5") == Decimal("0.500")

    def test_equality_with_int(self):
        assert Decimal("5") == 5
        assert Decimal("0") == 0

    def test_hash_after_normalization(self):
        assert hash(Decimal("0.5")) == hash(Decimal("0.50"))

    def test_str_normalized(self):
        assert str(Decimal("0.50")) == "0.5"


class TestQuantity:
    def test_construct_basic(self):
        q = Quantity(5, "cm")
        assert q.unit == "cm"
        assert q.value == MathInteger(5)

    def test_unit_with_whitespace_stripped(self):
        q = Quantity(5, "  cm  ")
        assert q.unit == "cm"

    def test_rejects_empty_unit(self):
        with pytest.raises(ValueError, match="不能为空"):
            Quantity(5, "")

    def test_rejects_empty_unit_after_strip(self):
        with pytest.raises(ValueError, match="不能为空"):
            Quantity(5, "   ")

    def test_rejects_float_value(self):
        with pytest.raises(TypeError, match="禁止直接接收 float"):
            Quantity(3.14, "cm")  # type: ignore[arg-type]

    def test_string_value_parses_to_integer_or_decimal(self):
        q_int = Quantity("42", "kg")
        assert q_int.value == MathInteger(42)
        q_dec = Quantity("3.14", "kg")
        assert q_dec.value == Decimal("3.14")

    def test_equality_same_unit(self):
        assert Quantity(5, "cm") == Quantity(5, "cm")
        assert Quantity(5, "cm") != Quantity(6, "cm")

    def test_inequality_different_unit_even_same_value(self):
        """5 cm != 5 m（不同单位视为不同量）。"""
        assert Quantity(5, "cm") != Quantity(5, "m")

    def test_equality_after_unit_lowercasing(self):
        """5 CM == 5 cm（规范化后单位一致）。"""
        assert Quantity(5, "CM").normalized() == Quantity(5, "cm").normalized()

    def test_rejects_bool_value(self):
        with pytest.raises(TypeError, match="bool"):
            Quantity(True, "cm")  # type: ignore[arg-type]


class TestInterval:
    def test_closed_interval(self):
        iv = Interval(1, 10, low_closed=True, high_closed=True)
        assert iv.contains(1)
        assert iv.contains(10)
        assert iv.contains(5)
        assert not iv.contains(0)
        assert not iv.contains(11)

    def test_open_interval(self):
        iv = Interval(1, 10, low_closed=False, high_closed=False)
        assert not iv.contains(1)
        assert not iv.contains(10)
        assert iv.contains(5)

    def test_half_open(self):
        iv_left_closed = Interval(1, 10, low_closed=True, high_closed=False)
        assert iv_left_closed.contains(1)
        assert not iv_left_closed.contains(10)
        iv_right_closed = Interval(1, 10, low_closed=False, high_closed=True)
        assert not iv_right_closed.contains(1)
        assert iv_right_closed.contains(10)

    def test_default_closed_both_ends(self):
        iv = Interval(1, 10)
        assert iv.contains(1)
        assert iv.contains(10)

    def test_rejects_inverted_endpoints(self):
        with pytest.raises(ValueError, match="顺序错误"):
            Interval(10, 1)

    def test_rejects_non_bool_closed_flag(self):
        with pytest.raises(TypeError, match="bool"):
            Interval(1, 10, low_closed="yes")  # type: ignore[arg-type]

    def test_equality_same_endpoints_and_closedness(self):
        a = Interval(1, 10)
        b = Interval(1, 10)
        c = Interval(1, 10, low_closed=False)
        assert a == b
        assert a != c

    def test_hashable(self):
        s = {Interval(1, 10), Interval(1, 10), Interval(1, 10, low_closed=False)}
        assert len(s) == 2

    def test_contains_with_uncomparable_type_returns_false(self):
        iv = Interval(1, 10)
        # 字符串与 int 不可比较
        assert iv.contains("5") is False

    def test_accepts_variable_type_endpoints(self):
        iv = Interval(MathInteger(1), MathInteger(10))
        assert iv.contains(MathInteger(5))
        assert iv.contains(5)  # int 与 MathInteger 可比较（MathInteger.__eq__ 支持）


# ────────────────────────────────────────────────────────────────────
# 验收 §2/§3：functions.py 每个函数 + 边界 + 错误输入
# ────────────────────────────────────────────────────────────────────
class TestAddSubMulDiv:
    def test_add_int(self):
        assert fn.add(2, 3) == 5

    def test_add_fraction(self):
        assert fn.add(PyFraction(1, 2), PyFraction(1, 3)) == PyFraction(5, 6)

    def test_add_decimal(self):
        result = fn.add(PyDecimal("0.1"), PyDecimal("0.2"))
        assert result == PyDecimal("0.3")

    def test_add_quantity_same_unit(self):
        a = Quantity(5, "cm")
        b = Quantity(3, "cm")
        assert fn.add(a, b).value == 8

    def test_add_quantity_different_unit_rejected(self):
        a = Quantity(5, "cm")
        b = Quantity(3, "m")
        with pytest.raises(TypeError, match="单位不同"):
            fn.add(a, b)

    def test_sub(self):
        assert fn.sub(10, 4) == 6
        assert fn.sub(Quantity(10, "g"), Quantity(3, "g")).value == 7

    def test_sub_quantity_different_unit_rejected(self):
        with pytest.raises(TypeError, match="单位不同"):
            fn.sub(Quantity(10, "g"), Quantity(3, "kg"))

    def test_mul(self):
        assert fn.mul(3, 4) == 12

    def test_div(self):
        assert fn.div(10, 4) == 2.5

    def test_div_by_zero_raises(self):
        with pytest.raises(ZeroDivisionError, match="除数不能为零"):
            fn.div(5, 0)

    def test_div_fraction_by_zero(self):
        with pytest.raises(ZeroDivisionError, match="除数不能为零"):
            fn.div(PyFraction(1, 2), 0)


class TestGcdLcm:
    def test_gcd_basic(self):
        assert fn.gcd(12, 18) == 6

    def test_gcd_with_zero(self):
        assert fn.gcd(0, 7) == 7
        assert fn.gcd(7, 0) == 7
        assert fn.gcd(0, 0) == 0

    def test_gcd_negative_takes_absolute(self):
        assert fn.gcd(-12, 18) == 6
        assert fn.gcd(12, -18) == 6

    def test_gcd_rejects_bool(self):
        with pytest.raises(TypeError, match="bool"):
            fn.gcd(True, 5)  # type: ignore[arg-type]

    def test_gcd_rejects_float(self):
        with pytest.raises(TypeError, match="int"):
            fn.gcd(1.5, 3)  # type: ignore[arg-type]

    def test_lcm_basic(self):
        assert fn.lcm(4, 6) == 12

    def test_lcm_with_zero(self):
        assert fn.lcm(0, 5) == 0
        assert fn.lcm(5, 0) == 0

    def test_lcm_rejects_bool(self):
        with pytest.raises(TypeError, match="bool"):
            fn.lcm(False, 5)  # type: ignore[arg-type]


class TestSimplifyFraction:
    def test_basic_reduction(self):
        f = fn.simplify_fraction(4, 8)
        assert f.numerator == 1
        assert f.denominator == 2

    def test_already_reduced(self):
        f = fn.simplify_fraction(3, 7)
        assert f.numerator == 3
        assert f.denominator == 7

    def test_sign_normalized(self):
        f = fn.simplify_fraction(1, -2)
        assert f.numerator == -1
        assert f.denominator == 2

    def test_zero_numerator(self):
        f = fn.simplify_fraction(0, 5)
        assert f.numerator == 0
        assert f.denominator == 1

    def test_zero_denominator_raises(self):
        with pytest.raises(ZeroDivisionError):
            fn.simplify_fraction(1, 0)

    def test_rejects_bool(self):
        with pytest.raises(TypeError, match="bool"):
            fn.simplify_fraction(True, 2)  # type: ignore[arg-type]


class TestIsReduced:
    def test_reduced(self):
        assert fn.is_reduced(3, 7) is True
        assert fn.is_reduced(1, 2) is True

    def test_not_reduced(self):
        assert fn.is_reduced(2, 4) is False
        assert fn.is_reduced(6, 9) is False

    def test_negative_denominator_not_reduced(self):
        assert fn.is_reduced(1, -2) is False

    def test_zero_numerator_reduced_when_denom_positive(self):
        assert fn.is_reduced(0, 5) is True

    def test_zero_denominator_raises(self):
        with pytest.raises(ZeroDivisionError):
            fn.is_reduced(1, 0)


class TestFloorCeilRound:
    def test_floor_positive(self):
        assert fn.floor(3.7) == 3

    def test_floor_negative(self):
        assert fn.floor(-3.2) == -4

    def test_ceil_positive(self):
        assert fn.ceil(3.2) == 4

    def test_ceil_negative(self):
        assert fn.ceil(-3.7) == -3

    def test_round_half_up_default_zero_digits(self):
        """0.5 → 1（half-up，非 round() 的银行家舍入 0）。"""
        assert fn.round_half_up("0.5") == PyDecimal(1)
        assert fn.round_half_up("1.5") == PyDecimal(2)
        assert fn.round_half_up("2.5") == PyDecimal(3)

    def test_round_half_up_with_digits(self):
        assert fn.round_half_up("3.14159", 2) == PyDecimal("3.14")
        assert fn.round_half_up("3.145", 2) == PyDecimal("3.15")

    def test_round_half_up_negative_digits_treated_as_zero(self):
        assert fn.round_half_up("3.7", -1) == PyDecimal(4)

    def test_round_half_up_float_input(self):
        """float 先 str() 化避免漂移。"""
        assert fn.round_half_up(0.5) == PyDecimal(1)


class TestAbsSqrtPowerMinMax:
    def test_abs_int(self):
        assert fn.abs_value(-5) == 5
        assert fn.abs_value(5) == 5

    def test_abs_decimal(self):
        assert fn.abs_value(PyDecimal("-3.14")) == PyDecimal("3.14")

    def test_sqrt_basic(self):
        result = fn.sqrt_decimal(4)
        assert result == PyDecimal(2) or abs(float(result) - 2.0) < 1e-10

    def test_sqrt_zero(self):
        assert fn.sqrt_decimal(0) == PyDecimal(0)

    def test_sqrt_negative_raises(self):
        with pytest.raises(ValueError, match="负数"):
            fn.sqrt_decimal(-1)

    def test_sqrt_rejects_float(self):
        with pytest.raises(TypeError, match="float"):
            fn.sqrt_decimal(2.0)  # type: ignore[arg-type]

    def test_sqrt_rejects_bool(self):
        with pytest.raises(TypeError, match="bool"):
            fn.sqrt_decimal(True)  # type: ignore[arg-type]

    def test_power_basic(self):
        assert fn.power(2, 3) == 8

    def test_power_zero_exp(self):
        assert fn.power(5, 0) == 1

    def test_power_negative_exp_rejected(self):
        with pytest.raises(ValueError, match="负指数"):
            fn.power(2, -1)

    def test_power_rejects_bool_exp(self):
        with pytest.raises(TypeError, match="bool"):
            fn.power(2, True)  # type: ignore[arg-type]

    def test_power_rejects_float_exp(self):
        with pytest.raises(TypeError, match="int"):
            fn.power(2, 2.0)  # type: ignore[arg-type]

    def test_min(self):
        assert fn.minimum(3, 5) == 3
        assert fn.minimum(-1, -2) == -2

    def test_max(self):
        assert fn.maximum(3, 5) == 5
        assert fn.maximum(-1, -2) == -1


class TestConvertUnit:
    def test_same_unit_returns_unchanged(self):
        q = fn.convert_unit(5, "cm", "cm")
        assert q.value == 5
        assert q.unit == "cm"

    def test_m_to_cm(self):
        q = fn.convert_unit(2, "m", "cm")
        assert q.value == 200
        assert q.unit == "cm"

    def test_cm_to_m_decimal_result(self):
        q = fn.convert_unit(50, "cm", "m")
        assert q.value == Decimal("0.5")

    def test_km_to_m(self):
        q = fn.convert_unit(2, "km", "m")
        assert q.value == 2000

    def test_kg_to_g(self):
        q = fn.convert_unit(3, "kg", "g")
        assert q.value == 3000

    def test_unknown_unit_raises(self):
        with pytest.raises(KeyError, match="无换算规则"):
            fn.convert_unit(5, "m", "kg")

    def test_custom_table(self):
        """自定义换算表：1 foo = 100 bar。"""
        table = {"foo": {"bar": 100}}
        q = fn.convert_unit(2, "foo", "bar", conversion_table=table)
        assert q.value == 200

    def test_rejects_bool_value(self):
        with pytest.raises(TypeError, match="bool"):
            fn.convert_unit(True, "m", "cm")  # type: ignore[arg-type]


class TestIntervalFunctions:
    def test_in_interval_inside(self):
        iv = Interval(1, 10)
        assert fn.in_interval(5, iv) is True

    def test_in_interval_outside(self):
        iv = Interval(1, 10)
        assert fn.in_interval(15, iv) is False

    def test_in_interval_boundary_closed(self):
        iv = Interval(1, 10)
        assert fn.in_interval(1, iv) is True
        assert fn.in_interval(10, iv) is True

    def test_in_interval_boundary_open(self):
        iv = Interval(1, 10, low_closed=False, high_closed=False)
        assert fn.in_interval(1, iv) is False
        assert fn.in_interval(10, iv) is False

    def test_in_interval_rejects_non_interval(self):
        with pytest.raises(TypeError, match="Interval"):
            fn.in_interval(5, (1, 10))  # type: ignore[arg-type]

    def test_interval_overlap_yes(self):
        a = Interval(1, 5)
        b = Interval(3, 8)
        assert fn.interval_overlap(a, b) is True

    def test_interval_overlap_no(self):
        a = Interval(1, 5)
        b = Interval(6, 10)
        assert fn.interval_overlap(a, b) is False

    def test_interval_overlap_touching_closed(self):
        """[1,5] 与 [5,10] 在 5 处重合（两端闭区间）→ 重叠。"""
        a = Interval(1, 5, low_closed=True, high_closed=True)
        b = Interval(5, 10, low_closed=True, high_closed=True)
        assert fn.interval_overlap(a, b) is True

    def test_interval_overlap_touching_one_open(self):
        """[1,5] 与 (5,10] → 5 处不重叠（b 开端）。"""
        a = Interval(1, 5, low_closed=True, high_closed=True)
        b = Interval(5, 10, low_closed=False, high_closed=True)
        assert fn.interval_overlap(a, b) is False

    def test_interval_overlap_rejects_non_interval(self):
        with pytest.raises(TypeError, match="Interval"):
            fn.interval_overlap(Interval(1, 5), (3, 8))  # type: ignore[arg-type]


# ────────────────────────────────────────────────────────────────────
# 验收 §2 元检查：函数数量、docstring、类型标注
# ────────────────────────────────────────────────────────────────────
class TestFunctionContract:
    """元检查：functions.py 公开函数契约。"""

    PUBLIC_FUNCTIONS = [
        "add", "sub", "mul", "div",
        "gcd", "lcm", "simplify_fraction", "is_reduced",
        "floor", "ceil", "round_half_up",
        "abs_value", "sqrt_decimal", "power",
        "minimum", "maximum",
        "convert_unit",
        "in_interval", "interval_overlap",
    ]

    def test_at_least_15_functions(self):
        """验收 §2：≥15 个函数。实际 19 个。"""
        assert len(self.PUBLIC_FUNCTIONS) >= 15

    def test_all_functions_callable_and_exist(self):
        for name in self.PUBLIC_FUNCTIONS:
            assert hasattr(fn, name), f"functions.py 缺函数 {name}"
            assert callable(getattr(fn, name)), f"{name} 不可调用"

    def test_all_functions_have_docstring(self):
        """验收 §2：每个函数含 docstring。"""
        for name in self.PUBLIC_FUNCTIONS:
            func = getattr(fn, name)
            assert func.__doc__ is not None, f"{name} 缺 docstring"
            assert len(func.__doc__.strip()) > 0, f"{name} 的 docstring 为空"

    def test_all_functions_have_type_annotations(self):
        """验收 §2：每个函数含类型标注（至少返回值标注或参数标注之一）。"""
        for name in self.PUBLIC_FUNCTIONS:
            func = getattr(fn, name)
            hints = inspect.signature(func).parameters
            # 至少有返回标注 OR 任一参数标注
            sig = inspect.signature(func)
            has_return_anno = sig.return_annotation is not inspect.Signature.empty
            has_param_anno = any(
                p.annotation is not inspect.Parameter.empty
                for p in sig.parameters.values()
            )
            assert has_return_anno or has_param_anno, (
                f"{name} 完全没有类型标注"
            )

    def test_safe_math_functions_table_complete(self):
        """SAFE_MATH_FUNCTIONS 表含全部公开函数（供 T-W2-002 evaluator 注入）。"""
        for name in self.PUBLIC_FUNCTIONS:
            assert name in fn.SAFE_MATH_FUNCTIONS, (
                f"SAFE_MATH_FUNCTIONS 缺 {name}"
            )

    def test_no_core_import(self):
        """验收 §4：不 import 核心域未暴露模块。

        检查 functions.py 不 import 任何 src.core.* 或 src.registry.*。
        """
        src_code = _FN_PATH.read_text(encoding="utf-8")
        assert "from src.core" not in src_code, (
            "functions.py 违反 X6：import 了核心域模块"
        )
        assert "from src.registry" not in src_code, (
            "functions.py 违反 X6：import 了注册表模块"
        )
        assert "import src.core" not in src_code
        assert "import src.registry" not in src_code

    def test_variable_types_no_core_import(self):
        """variable_types.py 同样不 import 核心域。"""
        src_code = _VT_PATH.read_text(encoding="utf-8")
        assert "from src.core" not in src_code
        assert "from src.registry" not in src_code
        assert "import src.core" not in src_code
        assert "import src.registry" not in src_code


# ────────────────────────────────────────────────────────────────────
# 端到端：用 002 evaluator 接口契约验证（鸭子类型）
# ────────────────────────────────────────────────────────────────────
class TestEvaluatorIntegration:
    """验证函数库符合 T-W2-002 evaluator 的 SAFE_FUNCTIONS 注入契约。

    002 的 SAFE_FUNCTIONS: dict[str, Callable[..., Any]]，
    本模块的 SAFE_MATH_FUNCTIONS 同形态，可直接合并注入。
    """

    def test_safe_math_functions_is_dict_of_callables(self):
        assert isinstance(fn.SAFE_MATH_FUNCTIONS, dict)
        for name, func in fn.SAFE_MATH_FUNCTIONS.items():
            assert isinstance(name, str)
            assert callable(func), f"{name} 不是 callable"

    def test_safe_math_functions_no_io_no_state(self):
        """同一参数多次调用结果一致（无副作用、确定性）。"""
        for _ in range(3):
            assert fn.add(2, 3) == 5
            assert fn.gcd(12, 18) == 6
            assert fn.simplify_fraction(4, 8).numerator == 1
