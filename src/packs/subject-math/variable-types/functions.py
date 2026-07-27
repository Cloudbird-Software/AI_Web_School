"""数学安全函数库（T-W2-025）。

实现 ≥15 个纯函数，覆盖四则、化简、单位换算、最大公约、取整、数值、区间判断等。
作为 DSL answer_program 的扩展函数源（架构 v2 §5.1）。

设计原则：
  1. 全部纯函数（无 IO、无状态、无副作用）。
  2. 类型标注完整；每个公开函数含 docstring。
  3. 不依赖核心域未暴露的模块（宪法 X6 反向：学科包不私接核心域）。
  4. 与 T-W2-002 安全表达式求值器集成：函数签名符合 dict[str, Callable] 注入。
  5. 数值运算避免 float 漂移：分数/小数运算用 fractions/decimal。

目录名 variable-types 含连字符（与 ADR §5.1 契约一致），无法作为 Python 包名。
故 functions.py 用 importlib 加载同目录下的 variable_types.py。
"""
from __future__ import annotations

import decimal
import importlib.util
import math
import sys
from decimal import Decimal as _PyDecimal
from fractions import Fraction as _PyFraction
from pathlib import Path
from typing import Any, Callable

# ────────────────────────────────────────────────────────────────────
# 加载同目录 variable_types.py（连字符目录无法用普通 import）
# ────────────────────────────────────────────────────────────────────
_VT_PATH = Path(__file__).parent / "variable_types.py"

# 模块名必须与 sys.modules 中键一致，保证后续 import 与 isinstance 检查
# 在同一进程内复用同一类对象（避免双重加载导致 isinstance 失败）。
_VT_MODULE_NAME = "subject_math_variable_types"


def _load_variable_types() -> Any:
    """以 importlib 加载 variable_types.py 为独立模块并注册到 sys.modules。

    Why: 目录 variable-types 含连字符，无法作为 Python 包导入；
         用 importlib.util.spec_from_file_location 绕过限制。
         注册到 sys.modules 保证后续加载（如测试再次加载）复用同一模块实例，
         从而 isinstance 检查能正确通过。
    """
    # 已加载则直接复用（关键：保证 isinstance 一致性）
    if _VT_MODULE_NAME in sys.modules:
        return sys.modules[_VT_MODULE_NAME]
    spec = importlib.util.spec_from_file_location(_VT_MODULE_NAME, _VT_PATH)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载 {_VT_PATH}")
    mod = importlib.util.module_from_spec(spec)
    # 注册到 sys.modules，必须在 exec_module 之前（避免循环导入时重复加载）
    sys.modules[_VT_MODULE_NAME] = mod
    spec.loader.exec_module(mod)
    return mod


_VT = _load_variable_types()

# 从 variable_types 拿出需要的类，避免本模块内部到处引用 _VT.
MathInteger = _VT.MathInteger
Fraction = _VT.Fraction
Decimal = _VT.Decimal
Quantity = _VT.Quantity
Interval = _VT.Interval


# ────────────────────────────────────────────────────────────────────
# 函数库（≥15 个）
# ────────────────────────────────────────────────────────────────────
__all__ = [
    # 四则
    "add",
    "sub",
    "mul",
    "div",
    # 化简与公约数
    "gcd",
    "lcm",
    "simplify_fraction",
    "is_reduced",
    # 取整
    "floor",
    "ceil",
    "round_half_up",
    # 数值
    "abs_value",
    "sqrt_decimal",
    "power",
    "minimum",
    "maximum",
    # 单位换算
    "convert_unit",
    # 区间
    "in_interval",
    "interval_overlap",
]


def add(a: Any, b: Any) -> Any:
    """返回 a + b。

    支持 int/float/Decimal/Fraction/MathInteger/Quantity（同单位）。
    跨类型运算遵循 Python 默认提升规则（如 int + Fraction → Fraction）。

    Raises:
        TypeError: Quantity 单位不同时无法相加。
    """
    if isinstance(a, Quantity) and isinstance(b, Quantity):
        if a.unit.lower() != b.unit.lower():
            raise TypeError(
                f"Quantity 单位不同无法相加：{a.unit} vs {b.unit}"
            )
    return a + b


def sub(a: Any, b: Any) -> Any:
    """返回 a - b。规则同 add。"""
    if isinstance(a, Quantity) and isinstance(b, Quantity):
        if a.unit.lower() != b.unit.lower():
            raise TypeError(
                f"Quantity 单位不同无法相减：{a.unit} vs {b.unit}"
            )
    return a - b


def mul(a: Any, b: Any) -> Any:
    """返回 a * b。"""
    return a * b


def div(a: Any, b: Any) -> Any:
    """返回 a / b（真除法，非整除）。

    Raises:
        ZeroDivisionError: b == 0。
    """
    if _is_zero(b):
        raise ZeroDivisionError("div 除数不能为零")
    return a / b


def gcd(a: int, b: int) -> int:
    """返回 |a| 与 |b| 的最大公约数（非负）。

    使用 Euclid 算法；gcd(0, 0) = 0（与 math.gcd 一致）。

    Raises:
        TypeError: a/b 非 int（bool 视为非 int 拒绝）。
    """
    if isinstance(a, bool) or isinstance(b, bool):
        raise TypeError("gcd 不接受 bool")
    if not isinstance(a, int) or not isinstance(b, int):
        raise TypeError(
            f"gcd 需要 int 参数，得到 {type(a).__name__} / {type(b).__name__}"
        )
    return math.gcd(a, b)


def lcm(a: int, b: int) -> int:
    """返回 |a| 与 |b| 的最小公倍数（非负）。

    lcm(0, x) = 0（与 math.lcm 一致）。

    Raises:
        TypeError: a/b 非 int。
    """
    if isinstance(a, bool) or isinstance(b, bool):
        raise TypeError("lcm 不接受 bool")
    if not isinstance(a, int) or not isinstance(b, int):
        raise TypeError(
            f"lcm 需要 int 参数，得到 {type(a).__name__} / {type(b).__name__}"
        )
    return abs(a * b) // math.gcd(a, b) if a and b else 0


def simplify_fraction(numerator: int, denominator: int) -> Fraction:
    """返回 numerator/denominator 化简后的 Fraction。

    自动约分、规范化符号（分母为正）。denominator=0 抛 ZeroDivisionError。

    Raises:
        ZeroDivisionError: denominator == 0。
        TypeError: numerator/denominator 非 int。
    """
    if isinstance(numerator, bool) or isinstance(denominator, bool):
        raise TypeError("simplify_fraction 不接受 bool")
    if not isinstance(numerator, int) or not isinstance(denominator, int):
        raise TypeError(
            "simplify_fraction 需要 int 参数，得到 "
            f"{type(numerator).__name__} / {type(denominator).__name__}"
        )
    return Fraction(numerator, denominator)


def is_reduced(numerator: int, denominator: int) -> bool:
    """判断 numerator/denominator 是否已为最简形式（互质且分母为正）。

    Raises:
        ZeroDivisionError: denominator == 0。
        TypeError: 参数非 int。
    """
    if denominator == 0:
        raise ZeroDivisionError("is_reduced 分母不能为零")
    if denominator < 0:
        return False
    return math.gcd(abs(numerator), abs(denominator)) == 1 or numerator == 0


def floor(x: Any) -> int:
    """返回不大于 x 的最大整数。"""
    return math.floor(x)


def ceil(x: Any) -> int:
    """返回不小于 x 的最小整数。"""
    return math.ceil(x)


def round_half_up(value: Any, digits: int = 0) -> _PyDecimal:
    """四舍五入到 digits 位小数（half-up，非银行家舍入）。

    Args:
        value: 数值（int/str/float/Decimal）。float 会先 str() 化避免漂移。
        digits: 小数位数（≥0；负数视为 0）。

    Returns:
        decimal.Decimal（避免 float 漂移）。

    Why half-up 而非 round()：小学数学约定"四舍五入"，与 Python round()
    的银行家舍入（half-even）不同；故显式实现。
    """
    if digits < 0:
        digits = 0
    if isinstance(value, float):
        value = str(value)
    d = _PyDecimal(value)
    # quantize 用 ROUND_HALF_UP，与小学约定一致
    quant = _PyDecimal(1).scaleb(-digits)  # 10^-digits
    # Why decimal.ROUND_HALF_UP 而非 _PyDecimal.ROUND_HALF_UP：
    # Python 3.12 起装饰器移除了 Decimal 类属性形式的 round 常量，
    # 必须从 decimal 模块直接取。
    return d.quantize(quant, rounding=decimal.ROUND_HALF_UP)


def abs_value(x: Any) -> Any:
    """返回 |x|（保持原类型）。"""
    return abs(x)


def sqrt_decimal(x: Any) -> _PyDecimal:
    """返回 sqrt(x) 作为 Decimal（避免 float 漂移）。

    Args:
        x: 非负数（int/str/Decimal）。

    Raises:
        ValueError: x < 0。
        TypeError: x 为 float（请先 str() 化）。
    """
    if isinstance(x, float):
        raise TypeError("sqrt_decimal 不接受 float，请先 str() 化以避免漂移")
    if isinstance(x, bool):
        raise TypeError("sqrt_decimal 不接受 bool")
    d = _PyDecimal(x) if not isinstance(x, _PyDecimal) else x
    if d < 0:
        raise ValueError(f"sqrt_decimal 不接受负数：{x}")
    if d == 0:
        return _PyDecimal(0)
    # Newton-Raphson 迭代求 sqrt（Decimal 精度由 context 决定）
    ctx = _PyDecimal
    # 提升精度到 28 位（默认 context）
    with _localcontext_extended() as ctx_local:
        x_dec = d.normalize()
        # 初始猜测
        guess = x_dec / 2 if x_dec > 1 else x_dec * 2
        if guess == 0:
            guess = _PyDecimal("0.1")
        last = None
        for _ in range(100):
            new_guess = (guess + x_dec / guess) / 2
            if last is not None and new_guess == last:
                break
            last = guess
            guess = new_guess
        return +guess  # 应用 context 精度


def power(base: Any, exp: int) -> Any:
    """返回 base ** exp（exp 为非负整数）。

    与 Python 内置 pow 不同：禁止负指数（避免返回 float），保持整数/Decimal 类型。

    Raises:
        ValueError: exp < 0。
        TypeError: exp 非 int 或为 bool。
    """
    if isinstance(exp, bool):
        raise TypeError("power 不接受 bool 作为指数")
    if not isinstance(exp, int):
        raise TypeError(f"power 指数必须为 int，得到 {type(exp).__name__}")
    if exp < 0:
        raise ValueError(f"power 禁止负指数（避免返回 float）：exp={exp}")
    return base**exp


def minimum(a: Any, b: Any) -> Any:
    """返回 min(a, b)。"""
    return min(a, b)


def maximum(a: Any, b: Any) -> Any:
    """返回 max(a, b)。"""
    return max(a, b)


def convert_unit(
    value: Any,
    from_unit: str,
    to_unit: str,
    conversion_table: dict[str, dict[str, Any]] | None = None,
) -> Quantity:
    """按 conversion_table 把 value 从 from_unit 换算到 to_unit。

    Args:
        value: 数值（int/str/Decimal）。
        from_unit: 源单位字符串。
        to_unit: 目标单位字符串。
        conversion_table: 换算表，形如
            ``{"m": {"cm": 100, "mm": 1000}, "cm": {"m": "0.01"}, ...}``。
            值可为 int/str/Decimal（str 推荐，避免 float 漂移）。
            为 None 时使用内置常用换算表（长度/质量）。

    Returns:
        Quantity（数值已换算、单位为 to_unit）。

    Raises:
        KeyError: from_unit → to_unit 无换算规则。
        TypeError: value 类型不支持。
    """
    if isinstance(value, bool):
        raise TypeError("convert_unit 不接受 bool 作为数值")
    table = conversion_table if conversion_table is not None else _DEFAULT_UNIT_TABLE
    fu = from_unit.strip().lower()
    tu = to_unit.strip().lower()
    if fu == tu:
        return Quantity(value, to_unit)
    if fu not in table or tu not in table[fu]:
        raise KeyError(
            f"convert_unit 无换算规则：{from_unit} → {to_unit}"
        )
    factor = table[fu][tu]
    # 用 Decimal 计算，避免 float 漂移
    v_dec = _to_decimal(value)
    f_dec = _to_decimal(factor)
    result = v_dec * f_dec
    # 若结果为整数，转 MathInteger；否则保留 Decimal
    if result == result.to_integral_value():
        return Quantity(int(result), to_unit)
    return Quantity(result, to_unit)


def in_interval(value: Any, interval: Interval) -> bool:
    """判断 value 是否在 interval 内。

    Args:
        value: 待判断值。
        interval: Interval 实例。

    Returns:
        True 若 value 在区间内；False 若不在或类型不可比较。
    """
    if not isinstance(interval, Interval):
        raise TypeError(
            f"in_interval 第二参数必须为 Interval，得到 {type(interval).__name__}"
        )
    return interval.contains(value)


def interval_overlap(a: Interval, b: Interval) -> bool:
    """判断两个区间是否有重叠（含端点重合）。

    Args:
        a, b: 两个 Interval 实例。

    Returns:
        True 若存在 x 同时属于 a 与 b。

    Note:
        端点比较使用端点类型自身的 __lt__ 等；类型不可比较时返回 False。
    """
    if not isinstance(a, Interval) or not isinstance(b, Interval):
        raise TypeError("interval_overlap 需要两个 Interval 参数")
    # 重叠条件：a.low < b.high 且 b.low < a.high（含端点的边界处理）
    try:
        # 若任一区间下界大于另一区间上界，则不重叠
        if a.low is not None and b.high is not None:
            if a.low > b.high:
                return False
            if a.low == b.high and not (a.low_closed and b.high_closed):
                return False
        if b.low is not None and a.high is not None:
            if b.low > a.high:
                return False
            if b.low == a.high and not (b.low_closed and a.high_closed):
                return False
        return True
    except TypeError:
        return False


# ────────────────────────────────────────────────────────────────────
# 辅助
# ────────────────────────────────────────────────────────────────────
def _is_zero(x: Any) -> bool:
    """判断 x 是否为零（兼容 int/Decimal/Fraction）。"""
    if isinstance(x, (int, _PyDecimal, _PyFraction)):
        return x == 0
    return False


def _to_decimal(value: Any) -> _PyDecimal:
    """把任意数值转为 Decimal（float 拒绝，要求 str 化）。"""
    if isinstance(value, float):
        raise TypeError("数值不接受 float，请用 str(float) 或字符串字面量")
    if isinstance(value, _PyDecimal):
        return value
    if isinstance(value, bool):
        raise TypeError("不接受 bool")
    if isinstance(value, int):
        return _PyDecimal(value)
    if isinstance(value, str):
        return _PyDecimal(value.strip())
    raise TypeError(f"无法转为 Decimal：{type(value).__name__}")


class _localcontext_extended:
    """扩展 decimal 精度上下文（28 位默认→50 位）。

    Why: sqrt 迭代需要更高精度避免收敛误差。
    """

    def __enter__(self) -> Any:
        import decimal

        self._ctx = decimal.getcontext()
        self._saved_prec = self._ctx.prec
        self._ctx.prec = 50
        return self._ctx

    def __exit__(self, *exc: Any) -> None:
        self._ctx.prec = self._saved_prec


# 内置常用单位换算表（小学 3-4 年级常用）
# 仅含长度、质量、时间的基础换算，值用字符串避免 float 漂移
_DEFAULT_UNIT_TABLE: dict[str, dict[str, Any]] = {
    "m": {"cm": "100", "mm": "1000", "km": "0.001"},
    "cm": {"m": "0.01", "mm": "10"},
    "mm": {"cm": "0.1", "m": "0.001"},
    "km": {"m": "1000", "cm": "100000"},
    "kg": {"g": "1000"},
    "g": {"kg": "0.001"},
    "min": {"s": "60", "h": "0.0166666666666666666666666667"},
    "s": {"min": "0.0166666666666666666666666667"},
    "h": {"min": "60", "s": "3600"},
}


# 模块级函数表（供 T-W2-002 evaluator 注入）
# 键名与公开函数名一致，避免与 T-W2-002 内置 SAFE_FUNCTIONS（abs/min/max/sqrt 等）
# 命名冲突——学科函数库应使用学科专属前缀或具名命名。
SAFE_MATH_FUNCTIONS: dict[str, Callable[..., Any]] = {
    "add": add,
    "sub": sub,
    "mul": mul,
    "div": div,
    "gcd": gcd,
    "lcm": lcm,
    "simplify_fraction": simplify_fraction,
    "is_reduced": is_reduced,
    "floor": floor,
    "ceil": ceil,
    "round_half_up": round_half_up,
    "abs_value": abs_value,
    "sqrt_decimal": sqrt_decimal,
    "power": power,
    "minimum": minimum,
    "maximum": maximum,
    "convert_unit": convert_unit,
    "in_interval": in_interval,
    "interval_overlap": interval_overlap,
}
