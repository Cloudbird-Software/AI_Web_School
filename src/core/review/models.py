"""W3 S6 复习排程 ORM：review_policy / review_queue_entry（迁移 0010）.

- ReviewPolicy：策略版本账（只增，DB 触发器强制）——一行 = 策略族 × 版本。
- ReviewQueueEntry：派生队列（非三本账，允许 UPDATE）——正确性由
  scheduler.rebuild_queue 的「事件流 × 策略版本」纯函数重放保证。

宪法 A5：本模块不 import 任何学科包/学段包。
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict
from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Integer,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from src.core.models._base import Base


class ReviewPolicy(Base):
    """复习策略版本账（只增）.

    PK (policy_id, policy_version)：同族多版本并存，队列行按版本引用，
    重建时取行上记录的版本对应的间隔表。
    """

    __tablename__ = "review_policy"

    policy_id: Mapped[str] = mapped_column(Text, primary_key=True)
    policy_version: Mapped[str] = mapped_column(Text, primary_key=True)
    intervals_days: Mapped[list] = mapped_column(JSONB, nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    __table_args__ = (
        CheckConstraint(
            "jsonb_typeof(intervals_days) = 'array' "
            "AND jsonb_array_length(intervals_days) > 0",
            name="ck_review_policy_intervals_nonempty_array",
        ),
    )


class ReviewQueueEntry(Base):
    """复习队列派生表（状态机行，允许 UPDATE；非三本账）.

    UNIQUE(student_alias_id, item_version_id, policy_id, policy_version)：
    一个学生的一道题在同一策略版本下至多一条在队记录。
    """

    __tablename__ = "review_queue_entry"

    entry_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    student_alias_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), nullable=False
    )
    item_version_id: Mapped[str] = mapped_column(Text, nullable=False)
    policy_id: Mapped[str] = mapped_column(Text, nullable=False)
    policy_version: Mapped[str] = mapped_column(Text, nullable=False)
    stage: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    source_error_type_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    last_event_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True), nullable=True
    )
    enqueued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["policy_id", "policy_version"],
            ["review_policy.policy_id", "review_policy.policy_version"],
            name="fk_review_queue_entry_policy",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "student_alias_id",
            "item_version_id",
            "policy_id",
            "policy_version",
            name="uq_review_queue_entry_student_item_policy",
        ),
        CheckConstraint(
            "status IN ('pending', 'done')",
            name="ck_review_queue_entry_status_domain",
        ),
        CheckConstraint(
            "stage >= 0",
            name="ck_review_queue_entry_stage_nonnegative",
        ),
    )


# ── Pydantic（API/序列化用） ─────────────────────────────────────────


class ReviewQueueEntryPydantic(BaseModel):
    """ReviewQueueEntry 的 Pydantic 表示（到期取题接口的响应元素）."""

    model_config = ConfigDict(extra="forbid")

    entry_id: UUID
    student_alias_id: UUID
    item_version_id: str
    policy_id: str
    policy_version: str
    stage: int
    status: str
    source_error_type_id: Optional[str] = None
    enqueued_at: datetime
    due_at: datetime


__all__ = ["ReviewPolicy", "ReviewQueueEntry", "ReviewQueueEntryPydantic"]
