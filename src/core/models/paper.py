"""§4.6 paper 卷追溯表 ORM（T-W2-037）.

paper 表是组卷产物账本：一份卷 = 一次组卷选定的 item_version 集合 + 顺序。
表只增不改（D1 风格）：重新组卷生成新行而非 UPDATE；迁移 0009 触发器物理强制。

列与 alembic/versions/0009_paper_trace.py::_create_paper 逐字对齐：
- paper_id text PK
- paper_code text NOT NULL UNIQUE（人类可读卷码 = ULID + Luhn 校验位）
- paper_spec_id text NOT NULL UNIQUE（卷规格 id，QR 含此 id+校验位）
- paper_title/gradeband/subject_pack_id text NOT NULL
- weekly_batch_id text nullable（周更批次追溯）
- kp_snapshot_ref text NOT NULL（知识点范围快照引用）
- seed bigint NOT NULL（确定性种子）
- rendered_snapshot_path text nullable（PDF 落盘路径）
- created_at timestamptz NOT NULL server_default now()
- created_by text NOT NULL
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from src.core.models._base import Base


class Paper(Base):
    """卷主表 ORM 映射.

    一行 = 一份卷的组卷产物（paper_id）；
    paper_code 是人类可读的卷码（打印在卷面）；
    paper_spec_id 是卷规格 id（QR 含此 id，扫码定位卷）。

    宪法 D1 风格：本表行只增不改；需改题就生成新卷。
    """

    __tablename__ = "paper"

    paper_id: Mapped[str] = mapped_column(Text, primary_key=True)
    paper_code: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    paper_spec_id: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    paper_title: Mapped[str] = mapped_column(Text, nullable=False)
    gradeband: Mapped[str] = mapped_column(Text, nullable=False)
    subject_pack_id: Mapped[str] = mapped_column(Text, nullable=False)
    weekly_batch_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    kp_snapshot_ref: Mapped[str] = mapped_column(Text, nullable=False)
    seed: Mapped[int] = mapped_column(BigInteger, nullable=False)
    rendered_snapshot_path: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    created_by: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        UniqueConstraint("paper_code", name="uq_paper_code"),
        UniqueConstraint("paper_spec_id", name="uq_paper_spec_id"),
        CheckConstraint(
            "gradeband IN ('L', 'M', 'H')",
            name="ck_paper_gradeband_domain",
        ),
        CheckConstraint(
            "subject_pack_id IN ('subject-math', 'subject-chinese', 'subject-english')",
            name="ck_paper_subject_pack_domain",
        ),
    )

    def __repr__(self) -> str:  # pragma: no cover - 调试辅助
        return (
            f"Paper(paper_id={self.paper_id!r}, "
            f"paper_code={self.paper_code!r}, "
            f"subject_pack_id={self.subject_pack_id!r})"
        )


__all__ = ["Paper"]
