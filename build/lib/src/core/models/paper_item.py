"""§4.6 paper_item 卷内题目表 ORM（T-W2-037）.

paper_item 行 = 一道题在某卷中的位置与短码；表只增不改。
一道题在卷中按 placement_token（如 'q1' / 'q2.sub1'）定位；
item_short_code 是人类可读短码（打印在卷面，扫码查源）。

列与 alembic/versions/0009_paper_trace.py::_create_paper_item 逐字对齐：
- paper_item_id text PK
- paper_id text FK→paper
- item_version_id text FK→item_version
- placement_token text NOT NULL（卷内位置标识）
- item_number int NOT NULL（题号，1-based）
- item_short_code text NOT NULL UNIQUE（纠错短码）
- created_at timestamptz NOT NULL server_default now()
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from src.core.models._base import Base


class PaperItem(Base):
    """卷内题目 ORM 映射.

    一行 = 一道题在某卷中的位置（paper_item_id）；
    placement_token 是卷内位置标识（'q1' / 'q2.sub1'）；
    item_short_code 是纠错短码，扫码可回溯到 item_version 与签发证书。

    宪法 D1 风格：本表行只增不改；改题就生成新卷+新 paper_item。
    """

    __tablename__ = "paper_item"

    paper_item_id: Mapped[str] = mapped_column(Text, primary_key=True)
    paper_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("paper.paper_id", name="fk_paper_item_paper", ondelete="RESTRICT"),
        nullable=False,
    )
    item_version_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey(
            "item_version.item_version_id",
            name="fk_paper_item_item_version",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    placement_token: Mapped[str] = mapped_column(Text, nullable=False)
    item_number: Mapped[int] = mapped_column(Integer, nullable=False)
    item_short_code: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    __table_args__ = (
        UniqueConstraint("item_short_code", name="uq_paper_item_short_code"),
        UniqueConstraint(
            "paper_id",
            "placement_token",
            name="uq_paper_item_paper_placement",
        ),
        CheckConstraint(
            "item_number > 0",
            name="ck_paper_item_number_positive",
        ),
    )

    def __repr__(self) -> str:  # pragma: no cover - 调试辅助
        return (
            f"PaperItem(paper_item_id={self.paper_item_id!r}, "
            f"paper_id={self.paper_id!r}, "
            f"item_number={self.item_number}, "
            f"item_short_code={self.item_short_code!r})"
        )


__all__ = ["PaperItem"]
