"""W3 S5 弱项报告 Pydantic 模型（API 响应契约）."""
from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class WeaknessItem(BaseModel):
    """单错误类型的弱项条目.

    status：
    - concluded：证据达阈值，可归因（confidence 为贝叶斯后验）
    - insufficient_evidence：证据不足，不给定论（confidence 仍返回当前
      后验供参考，但消费方不得当作结论呈现；§4.7 允许输出证据不足）
    recommended_item_version_ids：针对性练习 5 题小卷（仅 concluded 时非空——
    没有定论的推荐是误导）；已剔除产生过该错误证据的题目版本。
    """

    model_config = ConfigDict(extra="forbid")

    error_type_id: str
    status: Literal["concluded", "insufficient_evidence"]
    evidence_count: int = Field(..., ge=0)
    confidence: float = Field(..., ge=0.0, le=1.0)
    recommended_item_version_ids: list[str] = Field(default_factory=list)


class WeaknessReport(BaseModel):
    """弱项报告 v1：按错误类型聚合作答事件的归因报告.

    scene：本报告的取数场景（None=未过滤，跨场景汇总）；D5 禁止混估——
    需要分场景口径时调用方必须显式传 scene，报告如实回显取数口径。
    items 按 evidence_count 降序、error_type_id 升序（确定性）。
    """

    model_config = ConfigDict(extra="forbid")

    student_alias_id: UUID
    scene: Optional[Literal["practice", "diagnosis", "measurement"]] = None
    min_evidence: int = Field(..., ge=1)
    generated_at: datetime
    items: list[WeaknessItem]


__all__ = ["WeaknessItem", "WeaknessReport"]
