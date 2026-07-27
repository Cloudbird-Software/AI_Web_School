"""§4.4 曝光账本双轨表 ORM（T-W3-assembly）.

paper_exposure（静态轨）：按 渠道×学科×版本×年级×周队列 记录题目曝光，
服务周更静态批处理的「同队列不重复」互斥（R-Z-02）。

student_exposure（在线轨）：按学生匿名 id 记录题目曝光，
服务在线组卷的「跨期不重复」（D7：只存 student_alias_id，不存 PII）。

两表均只增不改（迁移 0010 触发器物理强制，D1 风格）：
曝光是历史事实，撤销曝光不产生 UPDATE/DELETE。

列与 alembic/versions/0010_exposure_ledger.py 逐字对齐。

宪法 A5/A7：本模块不 import 任何学科包/学段包（学科零特判）。
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict
from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from src.core.models._base import Base


class PaperExposure(Base):
    """静态轨：周队列曝光 ORM 映射.

    一行 = 某题目版本在某 渠道×学科×周队列 的一次曝光；
    UNIQUE(channel, subject_pack_id, week_label, item_version_id)
    在 DB 层兜底「同周队列同渠道不重复发题」。
    """

    __tablename__ = "paper_exposure"

    exposure_id: Mapped[str] = mapped_column(Text, primary_key=True)
    channel: Mapped[str] = mapped_column(Text, nullable=False)
    subject_pack_id: Mapped[str] = mapped_column(Text, nullable=False)
    textbook_version: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    gradeband: Mapped[str] = mapped_column(Text, nullable=False)
    week_label: Mapped[str] = mapped_column(Text, nullable=False)
    item_version_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey(
            "item_version.item_version_id",
            name="fk_paper_exposure_item_version",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    template_version_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    paper_id: Mapped[Optional[str]] = mapped_column(
        Text,
        ForeignKey("paper.paper_id", name="fk_paper_exposure_paper", ondelete="RESTRICT"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    __table_args__ = (
        UniqueConstraint(
            "channel",
            "subject_pack_id",
            "week_label",
            "item_version_id",
            name="uq_paper_exposure_queue_item",
        ),
        CheckConstraint(
            "gradeband IN ('L', 'M', 'H')",
            name="ck_paper_exposure_gradeband_domain",
        ),
    )

    def __repr__(self) -> str:  # pragma: no cover - 调试辅助
        return (
            f"PaperExposure(exposure_id={self.exposure_id!r}, "
            f"channel={self.channel!r}, week_label={self.week_label!r}, "
            f"item_version_id={self.item_version_id!r})"
        )


class StudentExposure(Base):
    """在线轨：学生级曝光 ORM 映射.

    一行 = 某学生（匿名 id）见过某题目版本的一次记录；
    UNIQUE(student_alias_id, item_version_id) 在 DB 层兜底「跨期不重复」。
    """

    __tablename__ = "student_exposure"

    exposure_id: Mapped[str] = mapped_column(Text, primary_key=True)
    student_alias_id: Mapped[str] = mapped_column(Text, nullable=False)
    item_version_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey(
            "item_version.item_version_id",
            name="fk_student_exposure_item_version",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    template_version_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    paper_id: Mapped[Optional[str]] = mapped_column(
        Text,
        ForeignKey("paper.paper_id", name="fk_student_exposure_paper", ondelete="RESTRICT"),
        nullable=True,
    )
    session_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    purpose: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    __table_args__ = (
        UniqueConstraint(
            "student_alias_id",
            "item_version_id",
            name="uq_student_exposure_student_item",
        ),
        CheckConstraint(
            "purpose IN ('practice', 'diagnosis', 'measurement')",
            name="ck_student_exposure_purpose_domain",
        ),
    )

    def __repr__(self) -> str:  # pragma: no cover - 调试辅助
        return (
            f"StudentExposure(exposure_id={self.exposure_id!r}, "
            f"student_alias_id={self.student_alias_id!r}, "
            f"item_version_id={self.item_version_id!r})"
        )


class PaperExposurePydantic(BaseModel):
    """PaperExposure 的 Pydantic 表示（API 入参/序列化用）."""

    model_config = ConfigDict(extra="forbid")

    exposure_id: str
    channel: str
    subject_pack_id: str
    textbook_version: Optional[str] = None
    gradeband: Literal["L", "M", "H"]
    week_label: str
    item_version_id: str
    template_version_id: Optional[str] = None
    paper_id: Optional[str] = None
    created_at: Optional[datetime] = None


class StudentExposurePydantic(BaseModel):
    """StudentExposure 的 Pydantic 表示（API 入参/序列化用）."""

    model_config = ConfigDict(extra="forbid")

    exposure_id: str
    student_alias_id: str
    item_version_id: str
    template_version_id: Optional[str] = None
    paper_id: Optional[str] = None
    session_id: Optional[str] = None
    purpose: Literal["practice", "diagnosis", "measurement"]
    created_at: Optional[datetime] = None


__all__ = [
    "PaperExposure",
    "StudentExposure",
    "PaperExposurePydantic",
    "StudentExposurePydantic",
]
