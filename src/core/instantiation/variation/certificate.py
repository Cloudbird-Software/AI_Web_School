"""VariantCertificate：受控变式目标不变性证书（T-W2-005）.

记录变式过程中 objective 保持不变的证据（ADR §4.1 数学验算第三层：
objective 为槽值显式函数 + 技能集合恒等校验）。

设计要点：
  1. **content-addressed**：certificate_id 由 (operator_id, axis_id, variant_ids,
     objective_signature) 哈希得出，同一组变式 + 同一 objective 必得同一证书 id。
  2. **invariant_evidence**：三类证据
     - objective_signature：objective 的 kp_set + cognitive_level + gradeband 的哈希
     - kp_set_unchanged：所有变式的 kp_set 与母题一致（True=通过）
     - skill_set_unchanged：cognitive_level + gradeband 未变（True=通过）
  3. **certified**：True=已认证（受控变式且不变性验证通过）；False=UNPROVEN
     （AI 自由改写、或 objective 依赖槽被变更、或不变性校验失败）。
  4. **operator_id**：变式操作者标识
     - "controlled-variation-engine"：引擎受控变式
     - 其他：AI 自由改写 / 人工改写（必为 UNPROVEN）

宪法 X6：本模块不 import 任何学科包/学段包。
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

# 操作者 id 常量
CONTROLLED_VARIATION_OPERATOR: str = "controlled-variation-engine"


def _canonical_json(obj: Any) -> str:
    """规范化 JSON 序列化（与 content_addressing._canonical_json 一致）."""
    return json.dumps(
        obj,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _sha256_hex(payload: str) -> str:
    """计算 SHA-256 hex 摘要，加 sha256: 前缀."""
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def compute_objective_signature(objective: Any) -> str:
    """计算 objective 的技能集合签名.

    签名内容：kp_set 编码（升序）+ kp_set_mode + cognitive_level + gradeband。
    这四项定义了"考什么技能"，变式过程中必须不变（ADR §4.1 纪律①）。

    Args:
        objective: Objective Pydantic 模型或 dict。

    Returns:
        "sha256:" + hex 摘要。
    """
    if hasattr(objective, "model_dump"):
        obj = objective.model_dump()  # type: ignore[union-attr]
    elif isinstance(objective, dict):
        obj = objective
    else:
        raise TypeError(
            f"objective 必须为 dict 或 Pydantic 模型，实际为 {type(objective).__name__}"
        )
    kp_codes = sorted(kp["code"] for kp in obj["kp_set"])
    payload = _canonical_json({
        "kp_codes": kp_codes,
        "kp_set_mode": obj["kp_set_mode"],
        "cognitive_level": obj["cognitive_level"],
        "gradeband": obj["gradeband"],
    })
    return _sha256_hex(payload)


class VariantCertificate(BaseModel):
    """受控变式目标不变性证书.

    验收对照：
        §2 VariantCertificate 含 invariant_evidence + operator_id ✅
        §3 对 objective 依赖变更槽的变式，certified=False（UNPROVEN）✅
    """

    model_config = ConfigDict(extra="forbid")

    certificate_id: str = Field(
        ..., description="证书 id（内容寻址：operator+axis+variants+objective_sig）"
    )
    operator_id: str = Field(
        ...,
        description="变式操作者标识（controlled-variation-engine / AI 改写者）",
    )
    axis_id: str = Field(
        default="", description="变式轴 id（AI 自由改写时为空）"
    )
    certified: bool = Field(
        ..., description="True=已认证；False=UNPROVEN"
    )
    reason: str = Field(
        ..., description="认证或拒绝原因（审计留痕）"
    )
    invariant_evidence: dict[str, Any] = Field(
        ...,
        description=(
            "目标不变性证据：objective_signature / kp_set_unchanged / "
            "skill_set_unchanged / axis_slots / frozen_slots"
        ),
    )
    variant_ids: list[str] = Field(
        default_factory=list,
        description="生成的变式实例 item_version_id 列表",
    )

    @property
    def is_unproven(self) -> bool:
        """是否为 UNPROVEN（未认证）."""
        return not self.certified


def issue_certificate(
    *,
    operator_id: str,
    axis_id: str,
    certified: bool,
    reason: str,
    objective_signature: str,
    kp_set_unchanged: bool,
    skill_set_unchanged: bool,
    axis_slots: list[str],
    frozen_slots: list[str],
    variant_ids: list[str],
) -> VariantCertificate:
    """构造 VariantCertificate 并自动计算 certificate_id.

    为什么用工厂函数而非直接构造：certificate_id 需由其他字段派生，
    用工厂函数保证 id 一致性（避免调用方手算 id 出错）。

    Args:
        operator_id: 操作者标识。
        axis_id: 变式轴 id。
        certified: 是否已认证。
        reason: 认证/拒绝原因。
        objective_signature: objective 技能集合签名。
        kp_set_unchanged: kp_set 是否未变。
        skill_set_unchanged: 技能集合（cognitive_level+gradeband）是否未变。
        axis_slots: 被重采样的槽名列表。
        frozen_slots: 被冻结的槽名列表。
        variant_ids: 变式实例 id 列表。

    Returns:
        VariantCertificate 实例。
    """
    invariant_evidence = {
        "objective_signature": objective_signature,
        "kp_set_unchanged": kp_set_unchanged,
        "skill_set_unchanged": skill_set_unchanged,
        "axis_slots": sorted(axis_slots),
        "frozen_slots": sorted(frozen_slots),
    }
    # certificate_id = H(operator_id, axis_id, variant_ids, objective_signature, certified)
    # 为什么不含 reason：reason 是人类可读描述，同一逻辑结果可能有不同措辞；
    # id 应稳定，只依赖确定性字段。
    cert_payload = _canonical_json({
        "op": operator_id,
        "axis": axis_id,
        "vids": variant_ids,
        "osig": objective_signature,
        "cert": certified,
    })
    certificate_id = _sha256_hex(cert_payload)
    return VariantCertificate(
        certificate_id=certificate_id,
        operator_id=operator_id,
        axis_id=axis_id,
        certified=certified,
        reason=reason,
        invariant_evidence=invariant_evidence,
        variant_ids=variant_ids,
    )


def mark_unproven(
    *,
    operator_id: str,
    reason: str,
    objective_signature: str = "",
    axis_id: str = "",
    axis_slots: list[str] | None = None,
    frozen_slots: list[str] | None = None,
    variant_ids: list[str] | None = None,
) -> VariantCertificate:
    """标记变式为 UNPROVEN（未认证）.

    用于两种场景（验收 §3 / §4）：
      - objective 依赖槽被变更 → 拒绝发证
      - AI 自由改写 → 永远 UNPROVEN

    Args:
        operator_id: 操作者标识。
        reason: 拒绝原因。
        objective_signature: objective 签名（AI 改写时可能为空，表示无法证明不变）。
        axis_id: 变式轴 id（AI 改写时为空）。
        axis_slots: 被变更的槽名列表。
        frozen_slots: 冻结槽名列表。
        variant_ids: 变式实例 id 列表。

    Returns:
        VariantCertificate（certified=False）。
    """
    return issue_certificate(
        operator_id=operator_id,
        axis_id=axis_id,
        certified=False,
        reason=reason,
        objective_signature=objective_signature,
        # UNPROVEN 时不变性证据为"无法证明"，不是"已验证不变"
        kp_set_unchanged=False,
        skill_set_unchanged=False,
        axis_slots=axis_slots or [],
        frozen_slots=frozen_slots or [],
        variant_ids=variant_ids or [],
    )


__all__ = [
    "CONTROLLED_VARIATION_OPERATOR",
    "VariantCertificate",
    "compute_objective_signature",
    "issue_certificate",
    "mark_unproven",
]
