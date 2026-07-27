"""T-W2-027 数学目标不变性验证器.

架构 v2 §4.3 / §5.2：校验变式过程中 objective 的不变性，配合 T-W2-005 的
VariantCertificate 在门编排阶段做最终阻断。

设计要点：
  1. **三道校验**：
     - **objective_signature 一致**：variant 的 objective 签名 == parent 签名。
     - **kp_set 恒等**：kp_set 编码集合（升序）严格相等。
     - **skill_set 恒等**：cognitive_level + gradeband 严格相等。
  2. **槽依赖检测**：解析 answer_program.expression，提取引用的槽名；
     若变式轴覆盖的槽中含被表达式引用的 choice 槽，则该变式可能改变
     考查目标（如 choice 切换运算符），判 fail。
  3. **难度相关槽约束**：变式轴覆盖的槽必须 difficulty_relevant=True
     （只有难度相关的槽允许变式，考查目标相关的槽禁止变式）。
  4. **VariantCertificate 兼容**：若 ctx 携带 variant_certificate 字段，
     优先采信证书的 invariant_evidence；未提供时本验证器独立重算。

宪法 X6 反向：本模块只 import 核心域暴露的 gate.validator 框架与
instantiation.variation.certificate.compute_objective_signature（核心域公开 API），
不 import 任何 instantiation.engine/expr 内部实现（与 dual_check 独立）。
"""
from __future__ import annotations

import ast
import time
from decimal import Decimal as _PyDecimal
from typing import Any

from src.core.gate.validator import (
    GateContext,
    Validator,
    ValidatorResult,
    register_validator,
)
from src.core.instantiation.variation.certificate import (
    compute_objective_signature,
)

__all__ = [
    "ObjectiveInvarianceValidator",
    "check_objective_invariance",
    "ObjectiveInvarianceReport",
]


# ────────────────────────────────────────────────────────────────────
# 辅助：解析表达式引用的槽名（与 variation.engine._extract_referenced_slots 同语义，
# 但本模块独立实现——不引用引擎代码，避免学科包→核心域内部反向依赖）
# ────────────────────────────────────────────────────────────────────


def _extract_referenced_slots(expression: str) -> set[str]:
    """解析表达式，提取引用的槽名集合.

    用 ast.walk 遍历所有 ast.Name 节点；与引擎同名函数独立实现，
    避免学科包反向 import 核心域内部模块（宪法 X6 反向）。
    """
    if not isinstance(expression, str):
        return set()
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError:
        return set()
    return {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}


# ────────────────────────────────────────────────────────────────────
# 不变性校验核心
# ────────────────────────────────────────────────────────────────────


class ObjectiveInvarianceReport:
    """目标不变性校验报告（纯数据载体，避免与 ValidatorResult 耦合）.

    字段：
        objective_signature_match: parent 与 variant 的 objective_signature 是否一致
        kp_set_unchanged: kp_set 编码集合是否恒等
        skill_set_unchanged: cognitive_level + gradeband 是否恒等
        axis_slots_objective_dependent: 轴中含 objective 依赖槽的列表（应为空）
        axis_slots_non_difficulty: 轴中 difficulty_relevant=False 的槽列表（应为空）
        parent_signature: parent objective 签名
        variant_signature: variant objective 签名
    """

    __slots__ = (
        "objective_signature_match",
        "kp_set_unchanged",
        "skill_set_unchanged",
        "axis_slots_objective_dependent",
        "axis_slots_non_difficulty",
        "parent_signature",
        "variant_signature",
    )

    def __init__(
        self,
        *,
        objective_signature_match: bool,
        kp_set_unchanged: bool,
        skill_set_unchanged: bool,
        axis_slots_objective_dependent: list[str],
        axis_slots_non_difficulty: list[str],
        parent_signature: str,
        variant_signature: str,
    ) -> None:
        self.objective_signature_match = objective_signature_match
        self.kp_set_unchanged = kp_set_unchanged
        self.skill_set_unchanged = skill_set_unchanged
        self.axis_slots_objective_dependent = list(axis_slots_objective_dependent)
        self.axis_slots_non_difficulty = list(axis_slots_non_difficulty)
        self.parent_signature = parent_signature
        self.variant_signature = variant_signature

    @property
    def is_invariant(self) -> bool:
        """是否完全不变（所有校验通过）."""
        return (
            self.objective_signature_match
            and self.kp_set_unchanged
            and self.skill_set_unchanged
            and not self.axis_slots_objective_dependent
            and not self.axis_slots_non_difficulty
        )

    def to_evidence(self) -> dict[str, Any]:
        """转 evidence dict（落 ValidatorResult.evidence）."""
        return {
            "objective_signature_match": self.objective_signature_match,
            "kp_set_unchanged": self.kp_set_unchanged,
            "skill_set_unchanged": self.skill_set_unchanged,
            "axis_slots_objective_dependent": self.axis_slots_objective_dependent,
            "axis_slots_non_difficulty": self.axis_slots_non_difficulty,
            "parent_signature": self.parent_signature,
            "variant_signature": self.variant_signature,
            "is_invariant": self.is_invariant,
        }


def _get_kp_codes(objective: Any) -> set[str]:
    """从 objective 提取 kp 编码集合（兼容 dict / Pydantic 模型）."""
    if hasattr(objective, "model_dump"):
        obj = objective.model_dump()  # type: ignore[union-attr]
    elif isinstance(objective, dict):
        obj = objective
    else:
        return set()
    return {kp["code"] for kp in obj.get("kp_set", [])}


def _get_skill_set(objective: Any) -> tuple[str, str]:
    """从 objective 提取技能集合（cognitive_level, gradeband）."""
    if hasattr(objective, "model_dump"):
        obj = objective.model_dump()  # type: ignore[union-attr]
    elif isinstance(objective, dict):
        obj = objective
    else:
        return ("", "")
    return (obj.get("cognitive_level", ""), obj.get("gradeband", ""))


def _get_slot_difficulty_relevant(slots: Any, slot_name: str) -> bool:
    """从 slots 字典/Pydantic 模型取槽的 difficulty_relevant 标志.

    Args:
        slots: spec.slots（dict[str, Slot] 或 dict[str, dict]）。
        slot_name: 槽名。

    Returns:
        difficulty_relevant 值；槽不存在时返回 False（视为非难度相关，触发 fail）。
    """
    if not isinstance(slots, dict):
        return False
    slot = slots.get(slot_name)
    if slot is None:
        return False
    if hasattr(slot, "difficulty_relevant"):
        return bool(slot.difficulty_relevant)  # type: ignore[union-attr]
    if isinstance(slot, dict):
        return bool(slot.get("difficulty_relevant", False))
    return False


def _get_slot_type(slots: Any, slot_name: str) -> str:
    """从 slots 取槽的 type 字符串；槽不存在返回空串."""
    if not isinstance(slots, dict):
        return ""
    slot = slots.get(slot_name)
    if slot is None:
        return ""
    if hasattr(slot, "type"):
        return str(slot.type)  # type: ignore[union-attr]
    if isinstance(slot, dict):
        return str(slot.get("type", ""))
    return ""


def check_objective_invariance(
    *,
    parent_objective: Any,
    variant_objective: Any,
    slots: Any,
    axis_slots: list[str],
    answer_program_expression: str | None = None,
) -> ObjectiveInvarianceReport:
    """校验 variant 的 objective 是否相对 parent 保持不变.

    Args:
        parent_objective: parent 母题的 objective（dict 或 Pydantic 模型）。
        variant_objective: 变式实例的 objective（dict 或 Pydantic 模型）。
        slots: 母题 spec.slots（dict[str, Slot] 或 dict[str, dict]）。
        axis_slots: 变式轴覆盖的槽名列表。
        answer_program_expression: 可选，answer_program.expression；
            若提供则检测轴槽中是否含被表达式引用的 choice 槽（objective 依赖）。

    Returns:
        ObjectiveInvarianceReport：包含每项校验结果。
    """
    parent_sig = compute_objective_signature(parent_objective)
    variant_sig = compute_objective_signature(variant_objective)

    sig_match = parent_sig == variant_sig
    kp_unchanged = _get_kp_codes(parent_objective) == _get_kp_codes(variant_objective)
    skill_unchanged = _get_skill_set(parent_objective) == _get_skill_set(variant_objective)

    # 轴槽难度相关性：变式轴覆盖的槽必须 difficulty_relevant=True
    non_diff_axis: list[str] = []
    dependent_axis: list[str] = []
    expr_slots = (
        _extract_referenced_slots(answer_program_expression)
        if answer_program_expression
        else set()
    )
    for slot_name in axis_slots:
        if not _get_slot_difficulty_relevant(slots, slot_name):
            non_diff_axis.append(slot_name)
        # choice 槽进表达式 → objective 依赖
        if _get_slot_type(slots, slot_name) == "choice" and slot_name in expr_slots:
            if slot_name not in dependent_axis:
                dependent_axis.append(slot_name)

    return ObjectiveInvarianceReport(
        objective_signature_match=sig_match,
        kp_set_unchanged=kp_unchanged,
        skill_set_unchanged=skill_unchanged,
        axis_slots_objective_dependent=dependent_axis,
        axis_slots_non_difficulty=non_diff_axis,
        parent_signature=parent_sig,
        variant_signature=variant_sig,
    )


# ────────────────────────────────────────────────────────────────────
# 验证器
# ────────────────────────────────────────────────────────────────────


class ObjectiveInvarianceValidator(Validator):
    """目标不变性验证器.

    配合 T-W2-005 的 VariantCertificate 在门编排阶段做最终阻断。
    校验三件事：
      1. variant 的 objective_signature 与 parent 一致。
      2. kp_set 编码集合恒等。
      3. cognitive_level + gradeband 恒等。
    另校验变式轴槽约束：
      - 轴槽必须 difficulty_relevant=True
      - 轴槽不得含被 answer_program 引用的 choice 槽（objective 依赖槽）

    ctx.artifact_payload 期望字段：
    - parent_objective: parent 母题的 objective（dict/Pydantic）
    - variant_objective: 变式实例的 objective（dict/Pydantic）
    - slots: 母题 spec.slots
    - axis_slots: 变式轴覆盖的槽名列表（默认 []）
    - answer_program_expression: 可选，answer_program.expression

    verdict 规则：
    - review：payload 缺字段（无法校验）
    - pass：所有不变性校验通过
    - fail：objective 改变 / kp_set 改变 / skill_set 改变 / 轴含 objective 依赖槽
    """

    validator_id = "objective_invariance"
    version = "1.0.0+subject-math"
    blocking = True
    cost_tier = "cheap"

    async def validate(
        self, artifact_ref: str, ctx: GateContext
    ) -> ValidatorResult:
        start = time.monotonic()
        elapsed_ms = lambda: int((time.monotonic() - start) * 1000)

        payload = ctx.artifact_payload
        if payload is None:
            return self._timed_result(
                verdict="review",
                evidence={"reason": "artifact_payload 为 None"},
                confidence=_PyDecimal("0.000"),
                elapsed_ms=elapsed_ms(),
            )

        parent_obj = payload.get("parent_objective")
        variant_obj = payload.get("variant_objective")
        slots = payload.get("slots")
        axis_slots = list(payload.get("axis_slots", []) or [])
        expr = payload.get("answer_program_expression")

        if parent_obj is None or variant_obj is None:
            return self._timed_result(
                verdict="review",
                evidence={
                    "reason": "payload 缺少 parent_objective 或 variant_objective"
                },
                confidence=_PyDecimal("0.000"),
                elapsed_ms=elapsed_ms(),
            )
        if not isinstance(slots, dict):
            return self._timed_result(
                verdict="review",
                evidence={"reason": "payload 缺少 slots(dict)"},
                confidence=_PyDecimal("0.000"),
                elapsed_ms=elapsed_ms(),
            )

        report = check_objective_invariance(
            parent_objective=parent_obj,
            variant_objective=variant_obj,
            slots=slots,
            axis_slots=axis_slots,
            answer_program_expression=expr,
        )

        evidence = report.to_evidence()

        if report.is_invariant:
            return self._timed_result(
                verdict="pass",
                evidence=evidence,
                confidence=_PyDecimal("1.000"),
                elapsed_ms=elapsed_ms(),
            )

        # 失败原因分类
        reasons: list[str] = []
        if not report.objective_signature_match:
            reasons.append("objective_signature 不一致")
        if not report.kp_set_unchanged:
            reasons.append("kp_set 编码集合改变")
        if not report.skill_set_unchanged:
            reasons.append("skill_set（cognitive_level/gradeband）改变")
        if report.axis_slots_objective_dependent:
            reasons.append(
                f"变式轴含 objective 依赖槽：{report.axis_slots_objective_dependent}"
            )
        if report.axis_slots_non_difficulty:
            reasons.append(
                f"变式轴含非难度相关槽：{report.axis_slots_non_difficulty}"
            )

        return self._timed_result(
            verdict="fail",
            evidence={**evidence, "reason": "；".join(reasons)},
            confidence=_PyDecimal("1.000"),
            elapsed_ms=elapsed_ms(),
        )


# 模块加载时注册（pack_id='subject-math'）
register_validator("subject-math", ObjectiveInvarianceValidator)
