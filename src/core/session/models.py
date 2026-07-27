"""W3-S3 practice_session ORM + 会话 DTO.

practice_session 是在线作答会话的**运行态**表（迁移 0010）：
- 不在宪法 D1 三本账（内容版本/作答事件/校验签发）之内——进度字段随作答
  原地 UPDATE；每次作答的历史事实由 response_event（append-only）承载。
- item_sequence 会话开始时快照固化（确定性：开始之后题目集合与顺序不变）。

列与 alembic/versions/0010_practice_session.py::_create_practice_session 逐字对齐。
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    Text,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from src.core.models._base import Base


class PracticeSession(Base):
    """在线作答会话 ORM 映射（运行态可变，非账本）.

    - status：active / rest_prompted（时长保护触发，待休息确认）/
      completed（主序列+错题回测走完）/ abandoned（中途放弃）。
    - item_sequence：[{item_version_id, placement_token, item_number}]，
      开始时快照固化；placement_token 仅静态卷会话有（实例池序列为 None）。
    - wrong_marks：错题回测标记 [{item_version_id, error_type_ids,
      first_seen_at, retest_status}]，retest_status ∈
      pending（待回测）/ served（已出示待作答）/ passed / failed /
      off（会话未开启回测，仅标记）。
    - time_limit_sec：时长保护阈值快照（建会话时按 gradeband 定型落列，
      阈值策略后续调整不回溯影响进行中的会话）。
    - last_resume_at：时长保护计时锚点（开始=started_at；休息确认后重置）。
    """

    __tablename__ = "practice_session"

    session_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    student_alias_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    scene: Mapped[str] = mapped_column(Text, nullable=False)
    gradeband: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="active"
    )
    paper_id: Mapped[Optional[str]] = mapped_column(
        Text,
        ForeignKey("paper.paper_id", name="fk_practice_session_paper", ondelete="RESTRICT"),
        nullable=True,
    )
    item_sequence: Mapped[list] = mapped_column(JSONB, nullable=False)
    current_index: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )
    retest_wrong: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false"
    )
    wrong_marks: Mapped[list] = mapped_column(
        JSONB, nullable=False, server_default="'[]'::jsonb"
    )
    time_limit_sec: Mapped[int] = mapped_column(Integer, nullable=False)
    answered_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )
    correct_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    last_resume_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    last_activity_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "scene IN ('practice', 'diagnosis')",
            name="ck_practice_session_scene_domain",
        ),
        CheckConstraint(
            "gradeband IN ('L', 'M', 'H')",
            name="ck_practice_session_gradeband_domain",
        ),
        CheckConstraint(
            "status IN ('active', 'rest_prompted', 'completed', 'abandoned')",
            name="ck_practice_session_status_domain",
        ),
        CheckConstraint(
            "current_index >= 0",
            name="ck_practice_session_current_index_nonneg",
        ),
        CheckConstraint(
            "time_limit_sec > 0",
            name="ck_practice_session_time_limit_positive",
        ),
    )

    def __repr__(self) -> str:  # pragma: no cover - 调试辅助
        return (
            f"PracticeSession(session_id={self.session_id!r}, "
            f"status={self.status!r}, scene={self.scene!r})"
        )


# ────────────────────────────────────────────────────────────────────
# DTO（服务层出口；API 层序列化同源）
# ────────────────────────────────────────────────────────────────────

class SessionState(BaseModel):
    """会话状态（进度/已用时长/时长保护余量）."""

    model_config = ConfigDict(extra="forbid")

    session_id: uuid.UUID
    status: str
    scene: str
    gradeband: str
    paper_id: Optional[str] = None
    total: int = Field(..., description="主序列题量")
    main_answered: int = Field(..., description="主序列已作答数（=current_index）")
    answered_count: int = Field(..., description="累计作答数（含回测轮）")
    correct_count: int
    wrong_count: int = Field(..., description="错题标记数")
    retest_pending: int = Field(..., description="待回测错题数")
    elapsed_active_sec: int = Field(..., description="距上次休息确认以来的连续作答秒数")
    time_limit_sec: int
    remaining_sec: int = Field(..., description="距时长保护触发的剩余秒数（可为负=已超）")
    started_at: datetime
    completed_at: Optional[datetime] = None


class NextItem(BaseModel):
    """取下一题出口载荷（不含答案/评分参数——评分信息只出不进客户端）."""

    model_config = ConfigDict(extra="forbid")

    session_id: uuid.UUID
    round: str = Field(..., description="main（主序列）/ retest（错题回测）")
    position: int = Field(..., description="主序列内题号（1-based）")
    total: int
    item_version_id: str
    interaction_id: str
    content_blocks: list[dict[str, Any]]
    options: Optional[list[dict[str, str]]] = Field(
        default=None,
        description="选择题选项（确定性乱序；非选择题为 None）",
    )


class Feedback(BaseModel):
    """提交作答的即时反馈（含按错误类型展示的解析）."""

    model_config = ConfigDict(extra="forbid")

    event_id: uuid.UUID = Field(..., description="response_event 事件 id")
    correct: bool
    dimension_scores: dict[str, float]
    error_inferences: list[dict[str, Any]]
    error_feedback: list[dict[str, Any]] = Field(
        default_factory=list,
        description="按错误类型展示的反馈（error_type_id + 干扰项设计 label + 置信度）",
    )
    explanation: Optional[list[str]] = Field(
        default=None, description="题目解析（content 中 explanation/solution/analysis 块）"
    )
    progress: dict[str, int]
    session_status: str


__all__ = [
    "Feedback",
    "NextItem",
    "PracticeSession",
    "SessionState",
]
