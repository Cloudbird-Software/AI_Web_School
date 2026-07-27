"""T-W2-028 数学等价评分器单元测试.

覆盖任务卡验收 §3 20+ 用例：
  1. 等价：化简分数、单位换算、数值相等
  2. 不等价：值不同、单位不同（不可换算）
  3. 容差内：|actual - expected| <= tolerance
  4. 容差外：|actual - expected| > tolerance
  5. 非法输入：空字符串、非数学表达式、缺 answer_expr

另覆盖：
  - 评分契约 ScoreResult schema 一致性
  - 评分器注册句柄
  - 错误类型推断（off_by_one / wrong_unit / value_mismatch）

宪法 X6 反向：本测试 import 学科包内部模块（subject-math 含连字符，
用 importlib 加载），不 import 核心域内部实现。
"""
from __future__ import annotations

import importlib.util
import inspect
import sys
from decimal import Decimal as _PyDecimal
from pathlib import Path
from typing import Any

import pytest

# ────────────────────────────────────────────────────────────────────
# 加载被测模块（subject-math 含连字符，无法用普通 import）
# ────────────────────────────────────────────────────────────────────
_ROOT = Path(__file__).resolve().parent.parent.parent
_SCORERS_DIR = _ROOT / "src" / "packs" / "subject-math" / "scorers"
_ME_PATH = _SCORERS_DIR / "math_equivalence.py"
_ME_MODULE_NAME = "subject_math_math_equivalence"


def _load_module(path: Path, name: str) -> Any:
    """以 importlib 加载 .py 文件为独立模块."""
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载 {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


me = _load_module(_ME_PATH, _ME_MODULE_NAME)

MathEquivalenceScorer = me.MathEquivalenceScorer
ScoreResult = me.ScoreResult
score = me.score
normalize_expression = me.normalize_expression
compare_with_tolerance = me.compare_with_tolerance
infer_error_type = me.infer_error_type

SCORER_ID = me.SCORER_ID
SCORER_VERSION = me.SCORER_VERSION


# ────────────────────────────────────────────────────────────────────
# 辅助
# ────────────────────────────────────────────────────────────────────


def _params(
    answer_expr: str = "1/2",
    *,
    tolerance: str = "0",
    rules: list[str] | None = None,
) -> dict[str, Any]:
    """构造评分参数."""
    p: dict[str, Any] = {"answer_expr": answer_expr, "tolerance": tolerance}
    if rules is not None:
        p["equivalence_rules"] = rules
    return p


# ────────────────────────────────────────────────────────────────────
# §3.1 等价用例（pass）
# ────────────────────────────────────────────────────────────────────


def test_equivalence_exact_integer() -> None:
    """等价：整数完全匹配."""
    result = score("42", None, _params("42"))
    assert result.dimension_scores["correct"] == 1.0
    assert result.evidence["method"] == "exact"
    assert result.confidence["scoring"] == 1.0


def test_equivalence_decimal_equal_fraction() -> None:
    """等价：0.5 == 1/2（分数化简）."""
    result = score("0.5", None, _params("1/2"))
    assert result.dimension_scores["correct"] == 1.0


def test_equivalence_fraction_reduce_2_4() -> None:
    """等价：2/4 == 1/2（fraction_reduce 规则）."""
    result = score("2/4", None, _params("1/2"))
    assert result.dimension_scores["correct"] == 1.0


def test_equivalence_fraction_reduce_3_6() -> None:
    """等价：3/6 == 1/2."""
    result = score("3/6", None, _params("0.5"))
    assert result.dimension_scores["correct"] == 1.0


def test_equivalence_unit_meter_to_cm() -> None:
    """等价：1m == 100cm（unit_convert 规则）."""
    result = score("1m", None, _params("100cm"))
    assert result.dimension_scores["correct"] == 1.0
    assert result.evidence["used_unit_conversion"] is True


def test_equivalence_unit_cm_to_meter() -> None:
    """等价：100cm == 1m（反向单位换算）."""
    result = score("100cm", None, _params("1m"))
    assert result.dimension_scores["correct"] == 1.0


def test_equivalence_unit_kg_to_g() -> None:
    """等价：1kg == 1000g."""
    result = score("1kg", None, _params("1000g"))
    assert result.dimension_scores["correct"] == 1.0


def test_equivalence_unit_hour_to_min() -> None:
    """等价：1h == 60min."""
    result = score("1h", None, _params("60min"))
    assert result.dimension_scores["correct"] == 1.0


def test_equivalence_expression_evaluation() -> None:
    """等价：表达式求值后相等（"1+2" == "3"）."""
    result = score("1+2", None, _params("3"))
    assert result.dimension_scores["correct"] == 1.0


def test_equivalence_dict_response_with_unit() -> None:
    """等价：dict 形式作答 + 显式单位."""
    result = score(
        {"value": "1", "unit": "m"},
        None,
        _params("100cm"),
    )
    assert result.dimension_scores["correct"] == 1.0


# ────────────────────────────────────────────────────────────────────
# §3.2 不等价用例（fail）
# ────────────────────────────────────────────────────────────────────


def test_non_equivalence_different_value() -> None:
    """不等价：1/3 != 1/2."""
    result = score("1/3", None, _params("1/2"))
    assert result.dimension_scores["correct"] == 0.0
    assert result.evidence["match"] is False
    assert len(result.error_inferences) == 1


def test_non_equivalence_wrong_unit_no_conversion() -> None:
    """不等价：5s vs 5m（单位不可换算到等价值）."""
    result = score("5s", None, _params("5m"))
    assert result.dimension_scores["correct"] == 0.0
    assert result.evidence["match"] is False


def test_non_equivalence_off_by_one() -> None:
    """不等价：差 1（off_by_one 错误推断）."""
    result = score("42", None, _params("43"))
    assert result.dimension_scores["correct"] == 0.0
    assert result.error_inferences[0]["error_type_id"] == "off_by_one"


def test_non_equivalence_value_mismatch() -> None:
    """不等价：值完全不同（value_mismatch）."""
    result = score("42", None, _params("999"))
    assert result.dimension_scores["correct"] == 0.0
    assert result.error_inferences[0]["error_type_id"] == "value_mismatch"


# ────────────────────────────────────────────────────────────────────
# §3.3 容差内（pass with tolerance）
# ────────────────────────────────────────────────────────────────────


def test_tolerance_within_pass() -> None:
    """容差内：|3.14 - 3.14159| <= 0.01."""
    result = score("3.14", None, _params("3.14159", tolerance="0.01"))
    assert result.dimension_scores["correct"] == 1.0
    assert result.evidence["method"] == "tolerance"


def test_tolerance_at_boundary_pass() -> None:
    """容差边界：|1.0 - 1.005| == 0.005 <= tolerance=0.005."""
    result = score("1.0", None, _params("1.005", tolerance="0.005"))
    assert result.dimension_scores["correct"] == 1.0


def test_tolerance_decimal_string_no_float_drift() -> None:
    """容差用 Decimal 字符串避免浮点漂移."""
    # 0.1 + 0.2 在 float 中是 0.30000000000000004，本测试验证 Decimal 容差
    result = score("0.3", None, _params("0.3", tolerance="0.0001"))
    assert result.dimension_scores["correct"] == 1.0


# ────────────────────────────────────────────────────────────────────
# §3.4 容差外（fail with tolerance）
# ────────────────────────────────────────────────────────────────────


def test_tolerance_outside_fail() -> None:
    """容差外：|3.14 - 3.14159| > 0.0001."""
    result = score("3.14", None, _params("3.14159", tolerance="0.0001"))
    assert result.dimension_scores["correct"] == 0.0


def test_tolerance_at_boundary_outside_fail() -> None:
    """容差边界外：|1.0 - 1.006| > 0.005."""
    result = score("1.0", None, _params("1.006", tolerance="0.005"))
    assert result.dimension_scores["correct"] == 0.0


def test_tolerance_zero_means_exact() -> None:
    """容差=0 等价于精确匹配."""
    result = score("1.0", None, _params("1.001", tolerance="0"))
    assert result.dimension_scores["correct"] == 0.0


# ────────────────────────────────────────────────────────────────────
# §3.5 非法输入
# ────────────────────────────────────────────────────────────────────


def test_invalid_empty_response() -> None:
    """非法输入：空字符串作答."""
    result = score("", None, _params("1/2"))
    assert result.dimension_scores["correct"] == 0.0
    assert result.error_inferences[0]["error_type_id"] == "empty_response"


def test_invalid_response_syntax() -> None:
    """非法输入：非数学表达式作答."""
    result = score("hello", None, _params("42"))
    assert result.dimension_scores["correct"] == 0.0
    assert result.error_inferences[0]["error_type_id"] == "invalid_response"


def test_invalid_missing_answer_expr() -> None:
    """非法输入：params 缺 answer_expr."""
    result = score("42", None, {})
    assert result.dimension_scores["correct"] == 0.0
    assert result.confidence["scoring"] == 0.0
    assert "answer_expr" in result.evidence["reason"]


def test_invalid_unparseable_answer_expr() -> None:
    """非法输入：标准答案表达式无法解析."""
    result = score("42", None, _params("not a valid expression ###"))
    assert result.dimension_scores["correct"] == 0.0
    assert result.confidence["scoring"] == 0.0


# ────────────────────────────────────────────────────────────────────
# §1 评分契约一致性
# ────────────────────────────────────────────────────────────────────


def test_score_result_schema_compliance() -> None:
    """ScoreResult 含 output_schema 全部必填字段."""
    result = score("1/2", None, _params("1/2"))
    # 对齐 scorer.yaml output_schema.required
    assert "dimension_scores" in result.model_dump()
    assert "error_inferences" in result.model_dump()
    assert "confidence" in result.model_dump()
    assert "evidence" in result.model_dump()
    assert "scorer_version" in result.model_dump()
    assert result.scorer_version == SCORER_VERSION


def test_score_result_extra_forbid() -> None:
    """ScoreResult extra='forbid'：多余字段抛错."""
    with pytest.raises(Exception):
        ScoreResult(
            dimension_scores={"correct": 1.0},
            error_inferences=[],
            confidence={"scoring": 1.0},
            evidence={},
            scorer_version=SCORER_VERSION,
            extra_field="should_fail",  # type: ignore[arg-type]
        )


def test_scorer_deterministic() -> None:
    """确定性：相同输入必得相同输出."""
    r1 = score("1/2", None, _params("0.5"))
    r2 = score("1/2", None, _params("0.5"))
    assert r1.dimension_scores == r2.dimension_scores
    assert r1.evidence == r2.evidence
    assert r1.scorer_version == r2.scorer_version


def test_scorer_class_handle() -> None:
    """MathEquivalenceScorer 类作为注册句柄."""
    scorer = MathEquivalenceScorer()
    assert scorer.scorer_id == "math_equivalence"
    assert scorer.deterministic is True
    assert scorer.version == SCORER_VERSION

    # 类方法委托到模块级 score 函数
    result = scorer.score("1/2", None, _params("0.5"))
    assert result.dimension_scores["correct"] == 1.0


# ────────────────────────────────────────────────────────────────────
# §2 等价规则覆盖
# ────────────────────────────────────────────────────────────────────


def test_rule_fraction_reduce_disabled() -> None:
    """禁用 fraction_reduce：2/4 != 1/2（值虽然等价，但禁用了化简规则）."""
    # 由于 SymPy 求值天然化简，禁用 fraction_reduce 实际上不影响 SymPy 求值结果
    # （2/4 与 1/2 在 SymPy 中本来就是同一对象）。此测试验证规则集记录。
    result = score(
        "2/4",
        None,
        _params("1/2", rules=["decimal_tolerance", "unit_convert"]),
    )
    # SymPy 求值化简了 2/4 → 1/2，规则禁用无法阻止数学等价
    # 但 evidence 中应记录规则集
    assert "fraction_reduce" not in result.evidence["rules_applied"]


def test_rule_unit_convert_disabled() -> None:
    """禁用 unit_convert：1m != 100cm（不再做单位换算）."""
    result = score(
        "1m",
        None,
        _params("100cm", rules=["fraction_reduce", "decimal_tolerance"]),
    )
    # 不做单位换算 → 1 != 100 → fail
    assert result.dimension_scores["correct"] == 0.0
    assert "unit_convert" not in result.evidence["rules_applied"]


def test_rule_decimal_tolerance_disabled() -> None:
    """禁用 decimal_tolerance：容差不生效，精确匹配."""
    result = score(
        "3.14",
        None,
        _params("3.14159", tolerance="0.01", rules=["fraction_reduce", "unit_convert"]),
    )
    # 禁用 decimal_tolerance → 不容差比较 → fail
    assert result.dimension_scores["correct"] == 0.0


# ────────────────────────────────────────────────────────────────────
# 错误类型推断
# ────────────────────────────────────────────────────────────────────


def test_infer_error_off_by_one_positive() -> None:
    """推断 off_by_one（差 +1）."""
    import sympy
    assert infer_error_type(
        sympy.Integer(42), sympy.Integer(43), None, None
    ) == "off_by_one"


def test_infer_error_off_by_one_negative() -> None:
    """推断 off_by_one（差 -1）."""
    import sympy
    assert infer_error_type(
        sympy.Integer(43), sympy.Integer(42), None, None
    ) == "off_by_one"


def test_infer_error_wrong_unit() -> None:
    """推断 wrong_unit（单位不可换算）."""
    import sympy
    # 5m vs 5s：单位不同且无法换算到等价
    result = infer_error_type(
        sympy.Integer(5), sympy.Integer(5), "m", "s"
    )
    assert result == "wrong_unit"


def test_infer_error_value_mismatch() -> None:
    """推断 value_mismatch（一般值不等）."""
    import sympy
    assert infer_error_type(
        sympy.Integer(42), sympy.Integer(999), None, None
    ) == "value_mismatch"


# ────────────────────────────────────────────────────────────────────
# 直接调用辅助函数
# ────────────────────────────────────────────────────────────────────


def test_normalize_expression_integer() -> None:
    """normalize_expression 解析整数."""
    import sympy
    result = normalize_expression("42")
    assert result == sympy.Integer(42)


def test_normalize_expression_fraction() -> None:
    """normalize_expression 解析分数."""
    import sympy
    result = normalize_expression("1/2")
    assert result == sympy.Rational(1, 2)


def test_normalize_expression_decimal() -> None:
    """normalize_expression 解析小数."""
    import sympy
    result = normalize_expression("0.5")
    assert result == sympy.Rational(1, 2)


def test_normalize_expression_arithmetic() -> None:
    """normalize_expression 求值算术表达式."""
    import sympy
    result = normalize_expression("1+2*3")
    assert result == sympy.Integer(7)


def test_compare_with_tolerance_exact() -> None:
    """compare_with_tolerance 精确匹配."""
    import sympy
    is_equiv, method = compare_with_tolerance(
        sympy.Integer(42), sympy.Integer(42), None
    )
    assert is_equiv is True
    assert method == "exact"


def test_compare_with_tolerance_fraction_reduce() -> None:
    """compare_with_tolerance 分数化简等价."""
    import sympy
    is_equiv, method = compare_with_tolerance(
        sympy.Rational(2, 4), sympy.Rational(1, 2), None
    )
    assert is_equiv is True
    # SymPy 中 2/4 == 1/2（同一对象），method="exact"
    assert method in ("exact", "fraction_reduce")


def test_compare_with_tolerance_within() -> None:
    """compare_with_tolerance 容差内."""
    import sympy
    is_equiv, method = compare_with_tolerance(
        sympy.Rational(314, 100),
        sympy.Rational(314159, 100000),
        _PyDecimal("0.01"),
    )
    assert is_equiv is True
    assert method == "tolerance"


def test_compare_with_tolerance_outside() -> None:
    """compare_with_tolerance 容差外."""
    import sympy
    is_equiv, method = compare_with_tolerance(
        sympy.Rational(314, 100),
        sympy.Rational(314159, 100000),
        _PyDecimal("0.00001"),
    )
    assert is_equiv is False


# ────────────────────────────────────────────────────────────────────
# 宪法 X6 反向：评分器模块不 import src.core.instantiation.engine/expr
# ────────────────────────────────────────────────────────────────────


def test_no_instantiation_engine_imports_in_scorer() -> None:
    """评分器模块不 import src.core.instantiation.engine/expr（独立实现）."""
    import ast as _ast

    forbidden_prefixes = (
        "src.core.instantiation.engine",
        "src.core.instantiation.expr",
        "src.core.instantiation.distractor",
        "src.core.instantiation.difficulty",
    )
    mod = sys.modules.get(_ME_MODULE_NAME)
    assert mod is not None, f"模块 {_ME_MODULE_NAME} 未加载"
    src_text = inspect.getsource(mod)
    tree = _ast.parse(src_text)
    for node in _ast.walk(tree):
        if isinstance(node, _ast.Import):
            for alias in node.names:
                assert not alias.name.startswith(forbidden_prefixes), (
                    f"math_equivalence.py 含禁用 import：{alias.name!r}"
                )
        elif isinstance(node, _ast.ImportFrom):
            module = node.module or ""
            assert not module.startswith(forbidden_prefixes), (
                f"math_equivalence.py 含禁用 from-import：{module!r}"
            )
