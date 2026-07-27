"""T-W2-027 数学目标不变性验证器单元测试.

覆盖任务卡验收 §3 三种情况：
  1. 目标不变：parent == variant → pass
  2. 目标变化：kp_set / cognitive_level / gradeband 任一改变 → fail
  3. 技能集合变化：cognitive_level 或 gradeband 改变 → fail

另覆盖：
  - 变式轴含 objective 依赖槽（choice 槽进表达式）→ fail
  - 变式轴含非难度相关槽（difficulty_relevant=False）→ fail
  - payload 缺字段 → review
  - 验证器注册到 subject-math pack

宪法 X6 反向：本测试 import 核心域的 compute_objective_signature 与
gate.validator 框架，均为核心域公开 API；不 import 任何 instantiation.engine
内部模块。
"""
from __future__ import annotations

import importlib.util
import inspect
import sys
from pathlib import Path
from typing import Any

import pytest

from src.core.gate.validator import GateContext, get_validator, list_validators

# ────────────────────────────────────────────────────────────────────
# 加载被测模块（subject-math 含连字符，无法用普通 import）
# ────────────────────────────────────────────────────────────────────
_ROOT = Path(__file__).resolve().parent.parent.parent
_VALIDATORS_DIR = _ROOT / "src" / "packs" / "subject-math" / "validators"
_OI_PATH = _VALIDATORS_DIR / "objective_invariance.py"
_OI_MODULE_NAME = "subject_math_objective_invariance"


def _load_module(path: Path, name: str) -> Any:
    """以 importlib 加载 .py 文件为独立模块（与 dual_check 测试同模式）."""
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载 {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


oi = _load_module(_OI_PATH, _OI_MODULE_NAME)

ObjectiveInvarianceValidator = oi.ObjectiveInvarianceValidator
check_objective_invariance = oi.check_objective_invariance
ObjectiveInvarianceReport = oi.ObjectiveInvarianceReport


# ────────────────────────────────────────────────────────────────────
# autouse fixture：保证 subject-math 验证器在每个测试前已注册
# ────────────────────────────────────────────────────────────────────
# 为什么需要：test_gate_validator_base.py 的 reset_registry() 会清空整个注册表，
# 导致本测试的 list_validators('subject-math') 返回空集合。


@pytest.fixture(autouse=True)
def _ensure_subject_math_validators_registered() -> Any:
    """每个测试前重注册 objective_invariance 验证器."""
    from src.core.gate.validator import register_validator

    register_validator("subject-math", ObjectiveInvarianceValidator)
    yield


# ────────────────────────────────────────────────────────────────────
# 辅助：构造测试用 objective / slots
# ────────────────────────────────────────────────────────────────────


def _objective(
    *,
    code: str = "math.nal.int.add",
    cognitive_level: str = "apply",
    gradeband: str = "L",
    kp_set_mode: str = "single",
) -> dict[str, Any]:
    """构造最小 objective dict."""
    return {
        "kp_set": [{"dimension": "kp", "code": code}],
        "kp_set_mode": kp_set_mode,
        "cognitive_level": cognitive_level,
        "gradeband": gradeband,
        "graph_release": "2026.1",
    }


def _slots(
    *,
    a_difficulty: bool = True,
    b_difficulty: bool = True,
    op_type: str = "int",
) -> dict[str, dict[str, Any]]:
    """构造最小 slots dict（两槽 + 可选 choice 槽）."""
    slots: dict[str, dict[str, Any]] = {
        "a": {"type": "int", "difficulty_relevant": a_difficulty},
        "b": {"type": "int", "difficulty_relevant": b_difficulty},
    }
    if op_type is not None:
        slots["op"] = {"type": "choice", "difficulty_relevant": False, "choices": ["+", "-"]}
    return slots


# ────────────────────────────────────────────────────────────────────
# 验收 §3 场景 1：目标不变 → pass
# ────────────────────────────────────────────────────────────────────


async def test_objective_invariance_pass_when_identical() -> None:
    """parent 与 variant 的 objective 完全一致 → pass."""
    parent_obj = _objective()
    variant_obj = _objective()
    slots = _slots(op_type=None)

    ctx = GateContext(
        artifact_type="item",
        pack_id="subject-math",
        artifact_payload={
            "parent_objective": parent_obj,
            "variant_objective": variant_obj,
            "slots": slots,
            "axis_slots": ["a"],  # 仅难度相关槽变式
        },
    )
    result = await ObjectiveInvarianceValidator().validate("sha256:test-identical", ctx)

    assert result.verdict == "pass"
    assert result.validator_id == "objective_invariance"
    assert result.version == "1.0.0+subject-math"
    assert result.evidence["objective_signature_match"] is True
    assert result.evidence["kp_set_unchanged"] is True
    assert result.evidence["skill_set_unchanged"] is True
    assert result.evidence["is_invariant"] is True
    assert result.confidence == 1.0


async def test_objective_invariance_pass_with_axis_slots() -> None:
    """轴槽全部 difficulty_relevant=True → pass."""
    parent_obj = _objective()
    variant_obj = _objective()
    slots = _slots(op_type=None)

    ctx = GateContext(
        artifact_type="item",
        pack_id="subject-math",
        artifact_payload={
            "parent_objective": parent_obj,
            "variant_objective": variant_obj,
            "slots": slots,
            "axis_slots": ["a", "b"],  # 两槽均 difficulty_relevant
        },
    )
    result = await ObjectiveInvarianceValidator().validate("sha256:test-axis", ctx)
    assert result.verdict == "pass"


# ────────────────────────────────────────────────────────────────────
# 验收 §3 场景 2：目标变化 → fail
# ────────────────────────────────────────────────────────────────────


async def test_objective_invariance_fail_when_kp_set_changed() -> None:
    """kp_set 改变 → fail（考查目标变化）."""
    parent_obj = _objective(code="math.nal.int.add")
    variant_obj = _objective(code="math.nal.int.sub")  # 改了知识点
    slots = _slots(op_type=None)

    ctx = GateContext(
        artifact_type="item",
        pack_id="subject-math",
        artifact_payload={
            "parent_objective": parent_obj,
            "variant_objective": variant_obj,
            "slots": slots,
            "axis_slots": [],
        },
    )
    result = await ObjectiveInvarianceValidator().validate("sha256:test-kp-change", ctx)

    assert result.verdict == "fail"
    assert "kp_set" in result.evidence["reason"]
    assert result.evidence["kp_set_unchanged"] is False
    assert result.evidence["objective_signature_match"] is False
    assert result.evidence["is_invariant"] is False


async def test_objective_invariance_fail_when_gradeband_changed() -> None:
    """gradeband 改变 → fail."""
    parent_obj = _objective(gradeband="L")
    variant_obj = _objective(gradeband="M")  # 改了学段
    slots = _slots(op_type=None)

    ctx = GateContext(
        artifact_type="item",
        pack_id="subject-math",
        artifact_payload={
            "parent_objective": parent_obj,
            "variant_objective": variant_obj,
            "slots": slots,
            "axis_slots": [],
        },
    )
    result = await ObjectiveInvarianceValidator().validate("sha256:test-gradeband", ctx)

    assert result.verdict == "fail"
    assert "skill_set" in result.evidence["reason"]
    assert result.evidence["skill_set_unchanged"] is False


# ────────────────────────────────────────────────────────────────────
# 验收 §3 场景 3：技能集合变化 → fail
# ────────────────────────────────────────────────────────────────────


async def test_objective_invariance_fail_when_cognitive_level_changed() -> None:
    """cognitive_level 改变 → fail（技能集合变化）."""
    parent_obj = _objective(cognitive_level="apply")
    variant_obj = _objective(cognitive_level="analyze")
    slots = _slots(op_type=None)

    ctx = GateContext(
        artifact_type="item",
        pack_id="subject-math",
        artifact_payload={
            "parent_objective": parent_obj,
            "variant_objective": variant_obj,
            "slots": slots,
            "axis_slots": [],
        },
    )
    result = await ObjectiveInvarianceValidator().validate(
        "sha256:test-cognitive", ctx
    )

    assert result.verdict == "fail"
    assert "skill_set" in result.evidence["reason"]
    assert result.evidence["skill_set_unchanged"] is False


# ────────────────────────────────────────────────────────────────────
# 槽约束：objective 依赖槽 / 非难度相关槽
# ────────────────────────────────────────────────────────────────────


async def test_objective_invariance_fail_when_axis_contains_choice_in_expr() -> None:
    """变式轴含 choice 槽且该槽被 answer_program 引用 → fail（objective 依赖）."""
    parent_obj = _objective()
    variant_obj = _objective()
    slots = _slots(op_type="choice")
    # 表达式含 op 槽（条件分支），op 是 choice → 变式改变运算符 → 改变考查目标

    ctx = GateContext(
        artifact_type="item",
        pack_id="subject-math",
        artifact_payload={
            "parent_objective": parent_obj,
            "variant_objective": variant_obj,
            "slots": slots,
            "axis_slots": ["op"],  # 变式 choice 槽
            # 合法 Python 表达式：op 出现在条件中，变式改变运算符
            "answer_program_expression": "a + b if op == '+' else a - b",
        },
    )
    result = await ObjectiveInvarianceValidator().validate(
        "sha256:test-choice-expr", ctx
    )

    assert result.verdict == "fail"
    assert "objective 依赖" in result.evidence["reason"]
    assert "op" in result.evidence["axis_slots_objective_dependent"]


async def test_objective_invariance_fail_when_axis_slot_not_difficulty_relevant() -> None:
    """变式轴含 difficulty_relevant=False 的槽 → fail（非难度相关槽禁止变式）."""
    parent_obj = _objective()
    variant_obj = _objective()
    slots = {
        "a": {"type": "int", "difficulty_relevant": True},
        "b": {"type": "int", "difficulty_relevant": False},  # 非难度相关
    }

    ctx = GateContext(
        artifact_type="item",
        pack_id="subject-math",
        artifact_payload={
            "parent_objective": parent_obj,
            "variant_objective": variant_obj,
            "slots": slots,
            "axis_slots": ["b"],  # 变式非难度槽
        },
    )
    result = await ObjectiveInvarianceValidator().validate(
        "sha256:test-non-diff", ctx
    )

    assert result.verdict == "fail"
    assert "非难度相关" in result.evidence["reason"]
    assert "b" in result.evidence["axis_slots_non_difficulty"]


# ────────────────────────────────────────────────────────────────────
# payload 缺字段 → review
# ────────────────────────────────────────────────────────────────────


async def test_objective_invariance_review_when_payload_none() -> None:
    """artifact_payload 为 None → review."""
    ctx = GateContext(
        artifact_type="item",
        pack_id="subject-math",
        artifact_payload=None,
    )
    result = await ObjectiveInvarianceValidator().validate("sha256:test-none", ctx)
    assert result.verdict == "review"
    assert result.confidence == 0.0


async def test_objective_invariance_review_when_missing_objective() -> None:
    """缺 parent_objective 或 variant_objective → review."""
    ctx = GateContext(
        artifact_type="item",
        pack_id="subject-math",
        artifact_payload={
            "parent_objective": _objective(),
            # 缺 variant_objective
            "slots": _slots(op_type=None),
        },
    )
    result = await ObjectiveInvarianceValidator().validate(
        "sha256:test-missing", ctx
    )
    assert result.verdict == "review"


async def test_objective_invariance_review_when_slots_not_dict() -> None:
    """slots 非 dict → review."""
    ctx = GateContext(
        artifact_type="item",
        pack_id="subject-math",
        artifact_payload={
            "parent_objective": _objective(),
            "variant_objective": _objective(),
            "slots": "not-a-dict",
        },
    )
    result = await ObjectiveInvarianceValidator().validate(
        "sha256:test-bad-slots", ctx
    )
    assert result.verdict == "review"


# ────────────────────────────────────────────────────────────────────
# 直接调用 check_objective_invariance（不通过 ValidatorResult）
# ────────────────────────────────────────────────────────────────────


def test_check_objective_invariance_returns_report() -> None:
    """直接调用 check_objective_invariance 返回 ObjectiveInvarianceReport."""
    parent_obj = _objective()
    variant_obj = _objective(code="math.nal.int.sub")
    slots = _slots(op_type=None)

    report = check_objective_invariance(
        parent_objective=parent_obj,
        variant_objective=variant_obj,
        slots=slots,
        axis_slots=["a"],
    )

    assert isinstance(report, ObjectiveInvarianceReport)
    assert report.is_invariant is False
    assert report.kp_set_unchanged is False
    assert report.parent_signature != report.variant_signature


def test_check_objective_invariance_invariant_report() -> None:
    """parent == variant → is_invariant=True."""
    parent_obj = _objective()
    variant_obj = _objective()
    slots = _slots(op_type=None)

    report = check_objective_invariance(
        parent_objective=parent_obj,
        variant_objective=variant_obj,
        slots=slots,
        axis_slots=["a", "b"],  # 两槽均 difficulty_relevant=True
    )

    assert report.is_invariant is True
    assert report.parent_signature == report.variant_signature


def test_check_objective_invariance_evidence_dict() -> None:
    """to_evidence 返回 dict 包含全部字段."""
    parent_obj = _objective()
    variant_obj = _objective()
    slots = _slots(op_type=None)

    report = check_objective_invariance(
        parent_objective=parent_obj,
        variant_objective=variant_obj,
        slots=slots,
        axis_slots=["a"],
    )
    evidence = report.to_evidence()

    assert "objective_signature_match" in evidence
    assert "kp_set_unchanged" in evidence
    assert "skill_set_unchanged" in evidence
    assert "axis_slots_objective_dependent" in evidence
    assert "axis_slots_non_difficulty" in evidence
    assert "parent_signature" in evidence
    assert "variant_signature" in evidence
    assert "is_invariant" in evidence


# ────────────────────────────────────────────────────────────────────
# 验收 §1：objective.kp_set 与 slots 的依赖关系解析
# ────────────────────────────────────────────────────────────────────


def test_extract_referenced_slots_from_expression() -> None:
    """_extract_referenced_slots 解析表达式中的槽名."""
    # 直接通过模块属性访问（连字符目录无法用普通 import）
    extract = oi._extract_referenced_slots
    assert extract("a + b") == {"a", "b"}
    assert extract("a * b + c") == {"a", "b", "c"}
    assert extract("42") == set()
    # abs 既是函数名也是 Name 节点，与引擎 _extract_referenced_slots 同语义
    assert extract("abs(a)") == {"abs", "a"}
    assert extract("") == set()
    assert extract("not a valid expression") == set()  # SyntaxError → 空


def test_dependency_check_for_choice_slot_in_expression() -> None:
    """choice 槽进表达式 → 出现在 axis_slots_objective_dependent."""
    parent_obj = _objective()
    variant_obj = _objective()
    slots = _slots(op_type="choice")

    report = check_objective_invariance(
        parent_objective=parent_obj,
        variant_objective=variant_obj,
        slots=slots,
        axis_slots=["op"],
        # 合法 Python 表达式：op 出现在条件中，变式改变运算符
        answer_program_expression="a + b if op == '+' else a - b",
    )

    assert "op" in report.axis_slots_objective_dependent
    assert report.is_invariant is False


# ────────────────────────────────────────────────────────────────────
# 验证器注册
# ────────────────────────────────────────────────────────────────────


def test_objective_invariance_registered_for_subject_math() -> None:
    """objective_invariance 已注册到 subject-math pack."""
    registered = set(list_validators("subject-math"))
    assert "objective_invariance" in registered

    v = get_validator("subject-math", "objective_invariance")
    assert isinstance(v, ObjectiveInvarianceValidator)
    assert v.validator_id == "objective_invariance"
    assert v.blocking is True
    assert v.cost_tier == "cheap"


# ────────────────────────────────────────────────────────────────────
# 宪法 X6 反向：验证器模块不 import src.core.instantiation.engine/expr
# ────────────────────────────────────────────────────────────────────


def test_no_instantiation_engine_imports_in_validator() -> None:
    """验证器模块不 import src.core.instantiation.engine/expr（独立实现）.

    允许 import src.core.instantiation.variation.certificate.compute_objective_signature
    （核心域公开 API）。
    """
    import ast as _ast

    forbidden_prefixes = (
        "src.core.instantiation.engine",
        "src.core.instantiation.expr",
        "src.core.instantiation.distractor",
        "src.core.instantiation.difficulty",
    )
    mod = sys.modules.get(_OI_MODULE_NAME)
    assert mod is not None, f"模块 {_OI_MODULE_NAME} 未加载"
    src_text = inspect.getsource(mod)
    tree = _ast.parse(src_text)
    for node in _ast.walk(tree):
        if isinstance(node, _ast.Import):
            for alias in node.names:
                assert not alias.name.startswith(forbidden_prefixes), (
                    f"objective_invariance.py 含禁用 import：{alias.name!r}"
                )
        elif isinstance(node, _ast.ImportFrom):
            module = node.module or ""
            assert not module.startswith(forbidden_prefixes), (
                f"objective_invariance.py 含禁用 from-import：{module!r}"
            )
