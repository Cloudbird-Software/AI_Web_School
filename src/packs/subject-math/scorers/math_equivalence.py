"""T-W2-028 数学等价评分器.

实现 scorer.yaml 中的 math_equivalence 契约（specs/contracts/registries/scorer.yaml §72）：
  - 分数化简（fraction_reduce）：2/4 ≡ 1/2，1/2 ≡ 0.5
  - 单位换算（unit_convert）：1m ≡ 100cm，1kg ≡ 1000g
  - 数值容差（decimal_tolerance）：1.0001 ≈ 1.0000（容差可配置）

设计要点：
  1. **统一契约**：`score(response, item_version, params)` → ScoreResult
     遵循 scorer.yaml output_schema（dimension_scores/error_inferences/confidence/
     evidence/scorer_version）。
  2. **零浮点漂移**：用 SymPy 精确算术 + decimal.Decimal 容差比较，
     禁止 float 直接 ==（契约 §notes）。
  3. **双实现独立**：本模块不 import src.core.instantiation.*；
     表达式求值用 SymPy + AST 转换器（与 dual_check.py 同源，但学科包内复用）。
  4. **规范化输入**：response 与 answer_expr 在评分前都规范化为 SymPy 表达式，
     避免字符串比较（"1/2" vs "0.5" vs "2/4"）。
  5. **错误推断**：差异类型→error_type_id 映射（off_by_one / wrong_unit /
     unsimplified_fraction / value_mismatch），供教研回溯。

宪法 X6 反向：学科包只依赖核心域公开 API（gate.validator 注册表）；
不 import 核心域内部模块（instantiation.engine/expr）。
"""
from __future__ import annotations

import importlib.util
import re
import sys
import time
from decimal import Decimal as _PyDecimal
from pathlib import Path
from typing import Any

import sympy
from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "MathEquivalenceScorer",
    "ScoreResult",
    "score",
    "normalize_expression",
    "compare_with_tolerance",
    "infer_error_type",
]


# ────────────────────────────────────────────────────────────────────
# 加载同包 dual_check.py 的 SymPy 求值助手（连字符目录无法用普通 import）
# ────────────────────────────────────────────────────────────────────
_DC_MODULE_NAME = "subject_math_dual_check"
_DC_PATH = Path(__file__).resolve().parent.parent / "validators" / "dual_check.py"


def _load_dual_check() -> Any:
    """以 importlib 加载 dual_check.py，复用 SymPy 求值助手."""
    if _DC_MODULE_NAME in sys.modules:
        return sys.modules[_DC_MODULE_NAME]
    spec = importlib.util.spec_from_file_location(_DC_MODULE_NAME, _DC_PATH)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载 {_DC_PATH}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[_DC_MODULE_NAME] = mod
    spec.loader.exec_module(mod)
    return mod


_dc = _load_dual_check()
_DivisionByZeroMarker = _dc._DivisionByZeroMarker
_build_sympy_env = _dc._build_sympy_env
evaluate_with_sympy = _dc.evaluate_with_sympy
_answers_equal = _dc._answers_equal


# ────────────────────────────────────────────────────────────────────
# 内置单位换算表（与 functions.py _DEFAULT_UNIT_TABLE 同语义）
# ────────────────────────────────────────────────────────────────────
# 复制而非 import：避免学科包内部产生 variable-types ↔ scorers 循环依赖；
# 表内容与 functions.py 同源，由本模块测试验证一致性。
_UNIT_TABLE: dict[str, dict[str, str]] = {
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


# ────────────────────────────────────────────────────────────────────
# 评分结果（对齐 scorer.yaml output_schema）
# ────────────────────────────────────────────────────────────────────


class ScoreResult(BaseModel):
    """数学等价评分结果.

    对齐 specs/contracts/registries/scorer.yaml §20-48 output_schema：
    - dimension_scores: { dimension_id: score }（客观题单维度 correct: 0|1）
    - error_inferences: 错误类型推断列表（每条含 error_type_id/confidence/rule_version）
    - confidence: { scoring: number }（本评分器确定性，scoring=1.0）
    - evidence: 评分证据（规范化前后值、规则命中、对比方法）
    - scorer_version: 评分器版本串
    """

    model_config = ConfigDict(extra="forbid")

    dimension_scores: dict[str, float] = Field(
        ..., description="维度分（correct: 0.0 / 1.0）"
    )
    error_inferences: list[dict[str, Any]] = Field(
        default_factory=list, description="错误类型推断"
    )
    confidence: dict[str, float] = Field(
        ..., description="置信度（scoring: 0.0~1.0）"
    )
    evidence: dict[str, Any] = Field(
        default_factory=dict, description="评分证据"
    )
    scorer_version: str = Field(
        ..., description="评分器版本（含代码 digest）"
    )


# ────────────────────────────────────────────────────────────────────
# 表达式规范化
# ────────────────────────────────────────────────────────────────────


# 单位识别正则：数字 + 单位（如 "1m", "100cm", "0.5kg"）
# 用非贪婪匹配，避免误匹配（如 "5min" 而非 "5m"）
_UNIT_PATTERN = re.compile(
    r"^\s*(?P<value>-?\d+(?:\.\d+)?(?:/\d+)?)\s*(?P<unit>[a-zA-Z]+)\s*$"
)


def _parse_response_value(response: str) -> tuple[str, str | None]:
    """从学生作答中提取数值与可选单位.

    支持形态：
      - "1/2" → ("1/2", None)
      - "0.5" → ("0.5", None)
      - "100cm" → ("100", "cm")
      - "1m" → ("1", "m")

    Args:
        response: 学生作答字符串。

    Returns:
        (value_str, unit_str|None)。无法解析单位时返回 (response, None)。
    """
    s = response.strip()
    if not s:
        return (response, None)
    # 优先匹配 单位后缀
    m = _UNIT_PATTERN.match(s)
    if m:
        return (m.group("value"), m.group("unit").lower())
    # 无单位：原样返回，交给 SymPy 求值
    return (s, None)


def normalize_expression(expr: str) -> sympy.Basic:
    """把表达式字符串规范化为 SymPy 精确值.

    支持形态：
      - 整数： "42" → Integer(42)
      - 小数： "0.5" → Rational(1, 2)
      - 分数： "1/2" → Rational(1, 2)
      - 算术表达式： "1+2" → Integer(3)

    本函数与 dual_check.evaluate_with_sympy 同语义，但简化为单变量解析
    （无 env），专供评分器使用：学生作答通常是单一数值，不含变量。
    """
    s = expr.strip()
    if not s:
        raise ValueError("表达式为空")
    # 直接交给 SymPy 求值（无变量环境）
    return evaluate_with_sympy(s, env={})


# ────────────────────────────────────────────────────────────────────
# 容差比较
# ────────────────────────────────────────────────────────────────────


def compare_with_tolerance(
    actual: sympy.Basic,
    expected: sympy.Basic,
    tolerance: _PyDecimal | None = None,
) -> tuple[bool, str]:
    """比较两个 SymPy 值是否等价（含容差）.

    Args:
        actual: 学生作答的 SymPy 值。
        expected: 标准答案的 SymPy 值。
        tolerance: 数值容差（Decimal 字符串）；None 表示精确比较。

    Returns:
        (is_equivalent, method)
        - is_equivalent: True 等价
        - method: "exact" | "tolerance" | "fraction_reduce"
    """
    # 精确比较（分数化简在 SymPy 中天然发生）
    if _answers_equal(actual, expected):
        # 判断是否原本就化简等价（如 2/4 vs 1/2）
        if actual != expected:
            return True, "fraction_reduce"
        return True, "exact"

    # 容差比较
    if tolerance is not None:
        try:
            actual_dec = _PyDecimal(str(actual.evalf()))
            expected_dec = _PyDecimal(str(expected.evalf()))
            diff = abs(actual_dec - expected_dec)
            if diff <= tolerance:
                return True, "tolerance"
        except Exception:
            pass

    return False, "mismatch"


# ────────────────────────────────────────────────────────────────────
# 错误类型推断
# ────────────────────────────────────────────────────────────────────


def infer_error_type(
    actual: sympy.Basic,
    expected: sympy.Basic,
    actual_unit: str | None,
    expected_unit: str | None,
) -> str | None:
    """根据差异推断错误类型.

    Args:
        actual: 学生作答值。
        expected: 标准答案值。
        actual_unit: 学生作答单位。
        expected_unit: 标准答案单位。

    Returns:
        error_type_id 或 None（无明显错误类型）。
    """
    # 单位错误
    if actual_unit and expected_unit and actual_unit != expected_unit:
        # 检查是否能通过单位换算等价
        try:
            actual_dec = _PyDecimal(str(actual.evalf()))
            if expected_unit.lower() in _UNIT_TABLE.get(actual_unit.lower(), {}):
                factor = _PyDecimal(_UNIT_TABLE[actual_unit.lower()][expected_unit.lower()])
                converted = actual_dec * factor
                expected_dec = _PyDecimal(str(expected.evalf()))
                if abs(converted - expected_dec) < _PyDecimal("1e-9"):
                    return None  # 数值等价，仅表示形式不同
            return "wrong_unit"
        except Exception:
            return "wrong_unit"

    # off-by-one 错误（差 1）
    try:
        diff = sympy.simplify(actual - expected)
        if diff == 1 or diff == -1:
            return "off_by_one"
    except Exception:
        pass

    # 未化简分数（值相同但形式不同）
    if actual != expected and _answers_equal(actual, expected):
        return "unsimplified_fraction"

    return "value_mismatch"


# ────────────────────────────────────────────────────────────────────
# 单位换算
# ────────────────────────────────────────────────────────────────────


def _try_unit_conversion(
    value: sympy.Basic,
    from_unit: str,
    to_unit: str,
) -> sympy.Basic | None:
    """尝试把 value 从 from_unit 换算到 to_unit.

    Returns:
        换算后的 SymPy 值，或 None（无换算规则）。
    """
    if from_unit.lower() == to_unit.lower():
        return value
    table = _UNIT_TABLE.get(from_unit.lower(), {})
    if to_unit.lower() not in table:
        return None
    factor_str = table[to_unit.lower()]
    try:
        factor = sympy.Rational(factor_str)
        return value * factor
    except Exception:
        return None


# ────────────────────────────────────────────────────────────────────
# 主评分入口
# ────────────────────────────────────────────────────────────────────

# 评分器元数据
SCORER_ID = "math_equivalence"
SCORER_VERSION = "1.0.0+subject-math"
DEFAULT_TOLERANCE = _PyDecimal("0")  # 默认精确匹配
DEFAULT_EQUIVALENCE_RULES = frozenset(
    {"fraction_reduce", "unit_convert", "decimal_tolerance"}
)


def score(
    response: Any,
    item_version: Any,
    params: dict[str, Any] | None = None,
) -> ScoreResult:
    """评分主入口.

    Args:
        response: 学生作答。支持：
            - str：表达式字符串（如 "1/2", "0.5", "100cm"）
            - dict: {"value": str, "unit": str|None} 显式分离
        item_version: ItemVersion 快照（用于读取 scoring_ref/params）。
            本评分器主要使用 params 中的 answer_expr；item_version 仅作审计字段。
        params: 评分参数（覆盖 item_version.scoring_params）。期望字段：
            - answer_expr: 标准答案表达式（必填）
            - equivalence_rules: 启用规则列表（默认全部）
            - tolerance: 数值容差（Decimal 字符串，默认 "0"）
            - unit_table_ref: 单位换算表引用（保留扩展点，本实现用内置表）

    Returns:
        ScoreResult：dimension_scores={"correct": 0.0|1.0}。

    Raises:
        ValueError: params 缺 answer_expr 或表达式无法解析。
    """
    start = time.monotonic()
    elapsed_ms = lambda: int((time.monotonic() - start) * 1000)

    params = params or {}
    answer_expr = params.get("answer_expr")
    if not answer_expr:
        return ScoreResult(
            dimension_scores={"correct": 0.0},
            error_inferences=[],
            confidence={"scoring": 0.0},
            evidence={
                "reason": "params 缺 answer_expr",
                "elapsed_ms": elapsed_ms(),
            },
            scorer_version=SCORER_VERSION,
        )

    rules = set(params.get("equivalence_rules") or DEFAULT_EQUIVALENCE_RULES)
    tolerance_str = params.get("tolerance", "0")
    try:
        tolerance = _PyDecimal(str(tolerance_str))
    except Exception:
        tolerance = DEFAULT_TOLERANCE

    # 解析学生作答（始终尝试分离数值与单位，便于单位校验）
    if isinstance(response, dict):
        actual_value_str = str(response.get("value", "")).strip()
        actual_unit = response.get("unit")
    elif isinstance(response, str):
        actual_value_str, actual_unit = _parse_response_value(response)
    else:
        actual_value_str, actual_unit = str(response), None

    if not actual_value_str:
        return ScoreResult(
            dimension_scores={"correct": 0.0},
            error_inferences=[{
                "error_type_id": "empty_response",
                "confidence": 1.0,
                "rule_version": SCORER_VERSION,
            }],
            confidence={"scoring": 1.0},
            evidence={
                "reason": "学生作答为空",
                "rules_applied": sorted(rules),
            },
            scorer_version=SCORER_VERSION,
        )

    # 解析标准答案（始终尝试分离数值与单位，便于单位校验）
    expected_unit: str | None = None
    expected_value_str = str(answer_expr).strip()
    expected_value_str, expected_unit = _parse_response_value(expected_value_str)

    # SymPy 求值
    try:
        actual = normalize_expression(actual_value_str)
    except Exception as e:
        return ScoreResult(
            dimension_scores={"correct": 0.0},
            error_inferences=[{
                "error_type_id": "invalid_response",
                "confidence": 1.0,
                "rule_version": SCORER_VERSION,
            }],
            confidence={"scoring": 1.0},
            evidence={
                "reason": f"学生作答无法解析：{type(e).__name__}: {e}",
                "raw_response": str(response),
                "rules_applied": sorted(rules),
            },
            scorer_version=SCORER_VERSION,
        )

    try:
        expected = normalize_expression(expected_value_str)
    except Exception as e:
        return ScoreResult(
            dimension_scores={"correct": 0.0},
            error_inferences=[],
            confidence={"scoring": 0.0},
            evidence={
                "reason": f"标准答案无法解析：{type(e).__name__}: {e}",
                "raw_answer_expr": str(answer_expr),
                "rules_applied": sorted(rules),
            },
            scorer_version=SCORER_VERSION,
        )

    # 单位处理：若双方都有单位且不同
    # - 启用 unit_convert：尝试换算到 expected 单位
    # - 禁用 unit_convert 或换算失败：双方单位不同 → 不等价
    used_unit_conversion = False
    actual_for_compare = actual
    units_compatible = True  # 单位是否相容（相同或可换算）
    if actual_unit and expected_unit:
        if actual_unit.lower() == expected_unit.lower():
            # 单位相同，无需换算
            pass
        elif "unit_convert" in rules:
            converted = _try_unit_conversion(actual, actual_unit, expected_unit)
            if converted is not None:
                actual_for_compare = converted
                used_unit_conversion = True
            else:
                # 无法换算 → 不等价
                units_compatible = False
        else:
            # 未启用 unit_convert 但单位不同 → 不等价
            units_compatible = False

    # 容差比较
    use_tolerance = tolerance if "decimal_tolerance" in rules and tolerance > 0 else None
    is_equiv, method = compare_with_tolerance(
        actual_for_compare, expected, use_tolerance
    )

    # 综合单位相容性：单位不可换算时强制不等价，即使数值相等
    is_equiv = is_equiv and units_compatible

    # 错误类型推断
    error_type = None
    if not is_equiv:
        error_type = infer_error_type(
            actual_for_compare, expected, actual_unit, expected_unit
        )

    # 构造结果
    dimension_scores = {"correct": 1.0 if is_equiv else 0.0}
    error_inferences: list[dict[str, Any]] = []
    if error_type:
        error_inferences.append({
            "error_type_id": error_type,
            "confidence": 1.0,
            "rule_version": SCORER_VERSION,
        })

    evidence: dict[str, Any] = {
        "actual_raw": str(response),
        "actual_normalized": str(actual),
        "expected_normalized": str(expected),
        "actual_unit": actual_unit,
        "expected_unit": expected_unit,
        "used_unit_conversion": used_unit_conversion,
        "method": method,
        "rules_applied": sorted(rules),
        "tolerance": str(tolerance),
        "elapsed_ms": elapsed_ms(),
    }
    if is_equiv:
        evidence["match"] = True
    else:
        evidence["match"] = False
        evidence["reason"] = f"作答与标准答案不等价（method={method}）"

    return ScoreResult(
        dimension_scores=dimension_scores,
        error_inferences=error_inferences,
        confidence={"scoring": 1.0},  # 确定性评分器
        evidence=evidence,
        scorer_version=SCORER_VERSION,
    )


# ────────────────────────────────────────────────────────────────────
# 评分器类（注册到 scorer 注册表）
# ────────────────────────────────────────────────────────────────────
# 当前平台未统一抽象 Scorer 基类（W2 仅落地注册表契约 + 各学科包评分器实现）。
# 本模块用模块级 score() 函数作为评分入口；类作为注册句柄。


class MathEquivalenceScorer:
    """数学等价评分器句柄.

    本类是评分器注册句柄，实际评分逻辑在模块级 score() 函数。
    注册到 scorer 注册表（id="math_equivalence"）。
    """

    scorer_id: str = SCORER_ID
    version: str = SCORER_VERSION
    deterministic: bool = True

    def score(
        self,
        response: Any,
        item_version: Any,
        params: dict[str, Any] | None = None,
    ) -> ScoreResult:
        """评分入口（委托模块级 score 函数）."""
        return score(response, item_version, params)
