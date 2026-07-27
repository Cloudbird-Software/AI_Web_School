"""数学变量类型（T-W2-025）。

按 SubjectPack 契约（架构 v2 §5.1）落地数学 DSL 槽位类型：
  - MathInteger：大整数（任意精度，无浮点漂移）
  - Fraction：分数（符号化简至最简形式）
  - Decimal：定点小数（避免浮点漂移；规范字符串表示）
  - Quantity：带单位量（数值 + 单位标识符，单位换算由 functions.convert_unit 承载）
  - Interval：区间（开/闭/半开，端点可为 Number 或本模块定义的任意类型）

设计原则：
  1. 全部 immutable（hashable），便于作为 DSL env 注入值并保证求值确定性。
  2. normalized() 返回等价的规范形式，__eq__/__hash__ 基于 normalized() 结果。
     这样 1/2 与 2/4、0.5 与 0.50 等可被识别为相等。
  3. 不依赖核心域未暴露的任何模块（宪法 X6：核心域禁 import 学科包；
     反向：学科包可依赖标准库与自身暴露的协议，不私接核心域）。
  4. 数值类型禁止使用 float 内部存储——IEEE 754 双精度会引入 0.1+0.2!=0.3 类
     漂移；用 int / fractions.Fraction / decimal.Decimal 替代。
"""
from __future__ import annotations

from decimal import Decimal as _PyDecimal
from decimal import InvalidOperation
from fractions import Fraction as _PyFraction
from functools import total_ordering
from typing import Any, Union

__all__ = [
    "MathInteger",
    "Fraction",
    "Decimal",
    "Quantity",
    "Interval",
    "VariableType",
    "Number",
]

# 数值联合类型：用于 Interval 端点标注。
# 限定为本模块定义的 5 个类型 + Python 内置数值（int/str 转 MathInteger/Decimal）。
Number = Union[int, str, "MathInteger", "Fraction", "Decimal", "Quantity"]

# 所有变量类型的公共协议（运行时为 duck-typed；这里仅作类型提示）。
VariableType = Union[
    "MathInteger", "Fraction", "Decimal", "Quantity", "Interval"
]


# ────────────────────────────────────────────────────────────────────
# MathInteger：大整数
# ────────────────────────────────────────────────────────────────────
@total_ordering
class MathInteger:
    """大整数（任意精度，无浮点漂移）。

    用 int 存储以保证任意精度。接收 int 或字符串（如 "12345678901234567890"）。
    规范化：去除前导零、统一符号（-0 视为 0）。
    """

    __slots__ = ("_value",)

    def __init__(self, value: int | str) -> None:
        """构造大整数。

        Args:
            value: int 或可被 int 解析的字符串。

        Raises:
            TypeError: value 类型不支持。
            ValueError: 字符串无法解析为整数。
        """
        if isinstance(value, bool):
            # bool 是 int 子类，但语义上 MathInteger 不应接受布尔。
            raise TypeError(f"MathInteger 不接受 bool，得到 {value!r}")
        if isinstance(value, int):
            self._value: int = value
        elif isinstance(value, str):
            s = value.strip()
            if not s:
                raise ValueError(f"空字符串无法解析为整数：{value!r}")
            try:
                self._value = int(s)
            except ValueError as e:
                raise ValueError(f"字符串无法解析为整数：{value!r}") from e
        else:
            raise TypeError(
                f"MathInteger 需要 int 或 str，得到 {type(value).__name__}"
            )

    @property
    def value(self) -> int:
        """返回内部 int 值（只读）。"""
        return self._value

    def normalized(self) -> "MathInteger":
        """返回规范化形式（int 已规范，直接返回 self）。"""
        return self

    def __eq__(self, other: object) -> bool:
        if isinstance(other, MathInteger):
            return self._value == other._value
        if isinstance(other, bool):
            return False
        if isinstance(other, int):
            return self._value == other
        return NotImplemented

    def __lt__(self, other: object) -> bool:
        # Why: total_ordering 仅需 __eq__ + __lt__；MathInteger 与 int 互比
        # 是 Interval.contains 的核心路径（端点规范化为 MathInteger 后，
        # value 可能仍为 int）。
        if isinstance(other, MathInteger):
            return self._value < other._value
        if isinstance(other, bool):
            return NotImplemented
        if isinstance(other, int):
            return self._value < other
        return NotImplemented

    def __hash__(self) -> int:
        return hash(("MathInteger", self._value))

    def __add__(self, other: object) -> "MathInteger":
        if isinstance(other, MathInteger):
            return MathInteger(self._value + other._value)
        if isinstance(other, bool):
            return NotImplemented
        if isinstance(other, int):
            return MathInteger(self._value + other)
        return NotImplemented

    __radd__ = __add__

    def __sub__(self, other: object) -> "MathInteger":
        if isinstance(other, MathInteger):
            return MathInteger(self._value - other._value)
        if isinstance(other, bool):
            return NotImplemented
        if isinstance(other, int):
            return MathInteger(self._value - other)
        return NotImplemented

    def __rsub__(self, other: object) -> "MathInteger":
        # other - self：仅当 other 为 int/MathInteger
        if isinstance(other, MathInteger):
            return MathInteger(other._value - self._value)
        if isinstance(other, bool):
            return NotImplemented
        if isinstance(other, int):
            return MathInteger(other - self._value)
        return NotImplemented

    def __mul__(self, other: object) -> "MathInteger":
        if isinstance(other, MathInteger):
            return MathInteger(self._value * other._value)
        if isinstance(other, bool):
            return NotImplemented
        if isinstance(other, int):
            return MathInteger(self._value * other)
        return NotImplemented

    __rmul__ = __mul__

    def __repr__(self) -> str:
        return f"MathInteger({self._value!r})"

    def __str__(self) -> str:
        return str(self._value)


# ────────────────────────────────────────────────────────────────────
# Fraction：分数
# ────────────────────────────────────────────────────────────────────
@total_ordering
class Fraction:
    """分数（符号化简至最简形式）。

    用 fractions.Fraction 存储以保证自动约分与符号规范化。
    规范化：分母为正、分子分母互质、零分数表示为 0/1。
    """

    __slots__ = ("_value",)

    def __init__(
        self, numerator: int | str, denominator: int | str = 1
    ) -> None:
        """构造分数。

        Args:
            numerator: 分子（int 或可解析字符串）。
            denominator: 分母（int 或可解析字符串，默认 1）。

        Raises:
            TypeError: 类型不支持。
            ValueError: 分母为零或字符串无法解析。
        """
        if isinstance(numerator, bool) or isinstance(denominator, bool):
            raise TypeError("Fraction 不接受 bool 作为分子或分母")
        try:
            num = int(numerator) if not isinstance(numerator, int) else numerator
            den = int(denominator) if not isinstance(denominator, int) else denominator
        except (TypeError, ValueError) as e:
            raise ValueError(
                f"Fraction 分子/分母无法解析为整数：{numerator!r}/{denominator!r}"
            ) from e
        if den == 0:
            raise ZeroDivisionError(f"Fraction 分母不能为零：{num}/{den}")
        # fractions.Fraction 自动约分并规范化符号（分母为正）
        self._value: _PyFraction = _PyFraction(num, den)

    @property
    def numerator(self) -> int:
        """规范化后的分子（与分母互质，含符号）。"""
        return self._value.numerator

    @property
    def denominator(self) -> int:
        """规范化后的分母（正整数）。"""
        return self._value.denominator

    def normalized(self) -> "Fraction":
        """返回规范化形式（已规范，直接返回 self）。"""
        return self

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Fraction):
            return self._value == other._value
        if isinstance(other, _PyFraction):
            return self._value == other
        if isinstance(other, bool):
            return False
        if isinstance(other, int):
            return self._value == other
        return NotImplemented

    def __lt__(self, other: object) -> bool:
        if isinstance(other, Fraction):
            return self._value < other._value
        if isinstance(other, _PyFraction):
            return self._value < other
        if isinstance(other, bool):
            return NotImplemented
        if isinstance(other, int):
            return self._value < other
        return NotImplemented

    def __hash__(self) -> int:
        return hash(("Fraction", self._value))

    def __add__(self, other: object) -> "Fraction":
        if isinstance(other, Fraction):
            return Fraction.from_pyfraction(self._value + other._value)
        if isinstance(other, int) and not isinstance(other, bool):
            return Fraction.from_pyfraction(self._value + other)
        return NotImplemented

    __radd__ = __add__

    def __sub__(self, other: object) -> "Fraction":
        if isinstance(other, Fraction):
            return Fraction.from_pyfraction(self._value - other._value)
        if isinstance(other, int) and not isinstance(other, bool):
            return Fraction.from_pyfraction(self._value - other)
        return NotImplemented

    @classmethod
    def from_pyfraction(cls, f: _PyFraction) -> "Fraction":
        """从 fractions.Fraction 构造 Fraction（避免重复约分）。"""
        obj = cls.__new__(cls)
        obj._value = f  # type: ignore[attr-defined]
        return obj

    def __repr__(self) -> str:
        return f"Fraction({self._value.numerator}, {self._value.denominator})"

    def __str__(self) -> str:
        if self._value.denominator == 1:
            return str(self._value.numerator)
        return f"{self._value.numerator}/{self._value.denominator}"


# ────────────────────────────────────────────────────────────────────
# Decimal：定点小数
# ────────────────────────────────────────────────────────────────────
@total_ordering
class Decimal:
    """定点小数（避免浮点漂移；规范字符串表示）。

    用 decimal.Decimal 存储以避免 IEEE 754 漂移。
    规范化：去除前导/尾随零、统一符号、零统一表示为 "0"。
    接收 str 或 decimal.Decimal；禁止直接接收 float（避免 0.1 漂移）。
    """

    __slots__ = ("_value",)

    def __init__(self, value: str | _PyDecimal) -> None:
        """构造定点小数。

        Args:
            value: 字符串或 decimal.Decimal（不接受 float；float 需先 str() 转换）。

        Raises:
            TypeError: 类型不支持（含 float 直接传入）。
            ValueError: 字符串无法解析为小数。
        """
        if isinstance(value, float):
            raise TypeError(
                "Decimal 禁止直接接收 float（避免漂移）；请用 str(float) 或字符串字面量"
            )
        if isinstance(value, _PyDecimal):
            self._value: _PyDecimal = value
        elif isinstance(value, str):
            s = value.strip()
            if not s:
                raise ValueError(f"空字符串无法解析为小数：{value!r}")
            try:
                self._value = _PyDecimal(s)
            except InvalidOperation as e:
                raise ValueError(f"字符串无法解析为小数：{value!r}") from e
        else:
            raise TypeError(
                f"Decimal 需要 str 或 decimal.Decimal，得到 {type(value).__name__}"
            )

    @property
    def value(self) -> _PyDecimal:
        """返回内部 decimal.Decimal 值（只读）。"""
        return self._value

    def normalized(self) -> "Decimal":
        """返回规范化形式。

        规则：去除前导/尾随零、统一 +0/-0 为 0、移除无意义符号。
        保留有效精度，不主动截断或四舍五入（避免引入语义偏移）。
        """
        # normalize() 会移除尾随零与无意义前导零
        # 但 normalize() 把 1E+1 等科学计数法也规范了，需要转回普通形式
        norm = self._value.normalize()
        # 把 -0 转为 0（Decimal 的 sign() 返回 1 表示负零）
        if norm.is_zero() and norm.is_signed():
            norm = _PyDecimal(0)
        return Decimal(norm)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Decimal):
            # 比较规范化形式，使 0.50 == 0.5
            return self.normalized()._value == other.normalized()._value
        if isinstance(other, _PyDecimal):
            return self.normalized()._value == other.normalize()
        if isinstance(other, bool):
            return False
        if isinstance(other, int):
            return self.normalized()._value == _PyDecimal(other)
        return NotImplemented

    def __lt__(self, other: object) -> bool:
        if isinstance(other, Decimal):
            return self.normalized()._value < other.normalized()._value
        if isinstance(other, _PyDecimal):
            return self.normalized()._value < other
        if isinstance(other, bool):
            return NotImplemented
        if isinstance(other, int):
            return self.normalized()._value < _PyDecimal(other)
        return NotImplemented

    def __hash__(self) -> int:
        # 基于规范化形式哈希，保证 0.50 与 0.5 同哈希
        return hash(("Decimal", self.normalized()._value))

    def __repr__(self) -> str:
        return f"Decimal({str(self._value)!r})"

    def __str__(self) -> str:
        # 规范化后的字符串表示
        norm = self.normalized()._value
        # to_integral_value 在无小数部分时返回整数表示，否则保留小数
        return str(norm)


# ────────────────────────────────────────────────────────────────────
# Quantity：带单位量
# ────────────────────────────────────────────────────────────────────
class Quantity:
    """带单位量（数值 + 单位标识符）。

    数值类型可为 MathInteger/Fraction/Decimal/int/str；单位为字符串标识符
    （如 "cm"、"kg"、"min"）。单位换算由 functions.convert_unit 承载，
    本类不内置换算表（保持纯数据语义）。

    规范化：单位字符串去除空白并 lowercase；数值规范化。
    相等比较要求单位与数值均相等（不同单位视为不同量，即使数值相同）。
    """

    __slots__ = ("_value", "_unit")

    def __init__(self, value: Number, unit: str) -> None:
        """构造带单位量。

        Args:
            value: 数值（int/str/MathInteger/Fraction/Decimal/decimal.Decimal）。
                接受 stdlib decimal.Decimal 以便 convert_unit 直接传递；
                内部存储为本模块 Decimal（避免类型混用）。
            unit: 单位标识符（非空字符串）。

        Raises:
            TypeError: value/unit 类型不支持。
            ValueError: unit 为空。
        """
        if not isinstance(unit, str):
            raise TypeError(
                f"Quantity.unit 需要 str，得到 {type(unit).__name__}"
            )
        u = unit.strip()
        if not u:
            raise ValueError("Quantity.unit 不能为空字符串")
        self._unit: str = u

        if isinstance(value, (MathInteger, Fraction, Decimal)):
            self._value: Any = value.normalized()
        elif isinstance(value, bool):
            raise TypeError("Quantity 不接受 bool 作为数值")
        elif isinstance(value, float):
            raise TypeError(
                "Quantity.value 禁止直接接收 float（避免 IEEE 754 漂移）；"
                "请用 str(float) 或字符串字面量"
            )
        elif isinstance(value, _PyDecimal):
            # 接受 stdlib decimal.Decimal（如 convert_unit 的计算结果），
            # 包装为本模块 Decimal 以统一类型。
            self._value = Decimal(value).normalized()
        elif isinstance(value, int):
            self._value = MathInteger(value)
        elif isinstance(value, str):
            # 字符串尝试解析为整数，失败则解析为小数
            try:
                self._value = MathInteger(value)
            except ValueError:
                self._value = Decimal(value)
        else:
            raise TypeError(
                f"Quantity.value 需要 int/str/MathInteger/Fraction/Decimal，"
                f"得到 {type(value).__name__}"
            )

    @property
    def value(self) -> Any:
        """返回规范化后的数值（MathInteger/Fraction/Decimal 之一）。"""
        return self._value

    @property
    def unit(self) -> str:
        """返回单位标识符（已 trim 但保留原大小写以便人类阅读；
        规范化时再 lowercase）。"""
        return self._unit

    def normalized(self) -> "Quantity":
        """返回规范化形式（单位 lowercase，数值规范化）。"""
        return Quantity(self._value, self._unit.lower())

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Quantity):
            # 单位与规范化数值都相等才相等
            return (
                self._unit.lower() == other._unit.lower()
                and self._value == other._value
            )
        return NotImplemented

    def __hash__(self) -> int:
        return hash(("Quantity", self._unit.lower(), self._value))

    def __add__(self, other: object) -> "Quantity":
        # Why: Quantity 是数学值类型，应当支持算术；同单位相加返回 Quantity，
        # 供 functions.add 等复用 Python 操作符。
        if isinstance(other, Quantity):
            if self._unit.lower() != other._unit.lower():
                raise TypeError(
                    f"Quantity 单位不同无法相加：{self._unit} vs {other._unit}"
                )
            return Quantity(self._value + other._value, self._unit)
        return NotImplemented

    def __sub__(self, other: object) -> "Quantity":
        if isinstance(other, Quantity):
            if self._unit.lower() != other._unit.lower():
                raise TypeError(
                    f"Quantity 单位不同无法相减：{self._unit} vs {other._unit}"
                )
            return Quantity(self._value - other._value, self._unit)
        return NotImplemented

    def __repr__(self) -> str:
        return f"Quantity({self._value!r}, {self._unit!r})"

    def __str__(self) -> str:
        return f"{self._value} {self._unit}"


# ────────────────────────────────────────────────────────────────────
# Interval：区间
# ────────────────────────────────────────────────────────────────────
class Interval:
    """区间（开/闭/半开，端点可为 Number 或 VariableType）。

    支持四种区间类型：
      - [a, b]：闭区间（low_closed=True, high_closed=True）
      - (a, b)：开区间（low_closed=False, high_closed=False）
      - [a, b)：半开左闭右开
      - (a, b]：半开左开右闭

    规范化：端点规范化、空区间规范化为 (None, None, False, False)。
    相等比较：端点与闭合性均一致。

    Note: 端点比较使用端点类型自身的 __eq__/__lt__；不支持复杂跨类型
    比较（如 MathInteger(1) 与 Decimal("1.0") 比较），调用方需先规范化
    到同一类型。
    """

    __slots__ = ("_low", "_high", "_low_closed", "_high_closed")

    def __init__(
        self,
        low: Any,
        high: Any,
        low_closed: bool = True,
        high_closed: bool = True,
    ) -> None:
        """构造区间。

        Args:
            low: 下端点（Number 或 VariableType）。
            high: 上端点（同上）。
            low_closed: 下端点是否闭合（True=[, False=(）。
            high_closed: 上端点是否闭合（True=], False=)）。

        Raises:
            TypeError: low_closed/high_closed 非 bool。
            ValueError: low > high（端点顺序错误）。
        """
        if not isinstance(low_closed, bool) or not isinstance(
            high_closed, bool
        ):
            raise TypeError("low_closed/high_closed 必须为 bool")
        # 规范化端点：MathInteger/Fraction/Decimal/Quantity 调 normalized()
        low_norm = _normalize_endpoint(low)
        high_norm = _normalize_endpoint(high)
        # 检查端点顺序（仅当两端可比较时）
        try:
            if low_norm is not None and high_norm is not None:
                if low_norm > high_norm:  # type: ignore[operator]
                    raise ValueError(
                        f"区间端点顺序错误：low={low!r} > high={high!r}"
                    )
        except TypeError:
            # 跨类型不可比较：跳过顺序检查，调用方负责语义正确
            pass
        self._low = low_norm
        self._high = high_norm
        self._low_closed = low_closed
        self._high_closed = high_closed

    @property
    def low(self) -> Any:
        return self._low

    @property
    def high(self) -> Any:
        return self._high

    @property
    def low_closed(self) -> bool:
        return self._low_closed

    @property
    def high_closed(self) -> bool:
        return self._high_closed

    def normalized(self) -> "Interval":
        """返回规范化形式（端点已规范化，闭合性保留）。"""
        return Interval(
            self._low, self._high, self._low_closed, self._high_closed
        )

    def contains(self, value: Any) -> bool:
        """判断 value 是否在本区间内。

        Args:
            value: 待判断的值（与端点同类型或可比较）。

        Returns:
            True 若 value 在区间内；False 若不在或类型不可比较。
        """
        low_ok: bool
        high_ok: bool
        try:
            if self._low is None:
                low_ok = True  # 无下界
            elif self._low_closed:
                low_ok = value >= self._low
            else:
                low_ok = value > self._low
            if self._high is None:
                high_ok = True  # 无上界
            elif self._high_closed:
                high_ok = value <= self._high
            else:
                high_ok = value < self._high
        except TypeError:
            return False
        return low_ok and high_ok

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Interval):
            return (
                self._low == other._low
                and self._high == other._high
                and self._low_closed == other._low_closed
                and self._high_closed == other._high_closed
            )
        return NotImplemented

    def __hash__(self) -> int:
        return hash(
            ("Interval", self._low, self._high, self._low_closed, self._high_closed)
        )

    def __repr__(self) -> str:
        lb = "[" if self._low_closed else "("
        rb = "]" if self._high_closed else ")"
        return f"Interval({lb}{self._low!r}, {self._high!r}{rb})"


# ────────────────────────────────────────────────────────────────────
# 辅助：端点规范化
# ────────────────────────────────────────────────────────────────────
def _normalize_endpoint(endpoint: Any) -> Any:
    """规范化区间端点。

    - None 保留（表示无界）
    - MathInteger/Fraction/Decimal/Quantity：调用 normalized()
    - int：转为 MathInteger
    - str：尝试 MathInteger，失败转 Decimal
    - 其他类型：原样返回（调用方负责语义）
    """
    if endpoint is None:
        return None
    if isinstance(endpoint, (MathInteger, Fraction, Decimal, Quantity)):
        return endpoint.normalized()
    if isinstance(endpoint, bool):
        raise TypeError("Interval 端点不接受 bool")
    if isinstance(endpoint, int):
        return MathInteger(endpoint)
    if isinstance(endpoint, str):
        try:
            return MathInteger(endpoint)
        except ValueError:
            return Decimal(endpoint)
    return endpoint
