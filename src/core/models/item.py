"""§2.1 item 不变身份表 ORM + Pydantic（T-W1-003）.

宪法 D1：item 行只增不改（current_version_id 除外，仅发布事务可前移）；
宪法 A7：tier 为谱系字段，A/B/C/D 四级生产线对等。

列与 alembic/versions/0002_item_model.py::_create_item 逐字对齐：
- item_id text PK
- pack_id text NOT NULL
- tier item_tier_enum NOT NULL
- template_version_id text FK→item_template_version（A/B 级实例的母题来源；C/D 级为 NULL）
- current_version_id text FK→item_version（DEFERRABLE INITIALLY DEFERRED，循环外键）
- created_at timestamptz NOT NULL server_default now()
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict
from sqlalchemy import DateTime, ForeignKey, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from src.core.models._base import Base, item_tier_enum


class Item(Base):
    """§2.1 item 不变身份 ORM 映射.

    一行 = 一个题目的"身份"（item_id），跨版本不变；
    内容随时间产生多个 item_version 行，current_version_id 指向最新 published 版本。

    宪法 D1：除 current_version_id 外，本表行只增不改；
    宪法 A5/A7：本 ORM 不 import 任何学科包/学段包。
    """

    __tablename__ = "item"

    item_id: Mapped[str] = mapped_column(Text, primary_key=True)
    pack_id: Mapped[str] = mapped_column(Text, nullable=False)
    tier: Mapped[str] = mapped_column(item_tier_enum, nullable=False)
    template_version_id: Mapped[Optional[str]] = mapped_column(
        Text,
        ForeignKey(
            "item_template_version.template_version_id",
            name="fk_item_template_version",
        ),
        nullable=True,
    )
    # 循环外键：item_version.item_id → item.item_id 与本列形成环；
    # DEFERRABLE INITIALLY DEFERRED 让发布事务可在 COMMIT 时再检查（契约 §6.1）
    current_version_id: Mapped[Optional[str]] = mapped_column(
        Text,
        ForeignKey(
            "item_version.item_version_id",
            name="fk_item_current_version",
            deferrable=True,
            initially="DEFERRED",
        ),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    def __repr__(self) -> str:  # pragma: no cover - 调试辅助
        return (
            f"Item(item_id={self.item_id!r}, pack_id={self.pack_id!r}, "
            f"tier={self.tier!r}, current_version_id={self.current_version_id!r})"
        )


class ItemPydantic(BaseModel):
    """Item 的 Pydantic 表示（用于 API/序列化/校验）.

    与 ORM 列一一对应；extra='forbid' 防止调用方误传字段。
    """

    model_config = ConfigDict(extra="forbid")

    item_id: str
    pack_id: str
    tier: Literal["A", "B", "C", "D"]
    template_version_id: Optional[str] = None
    current_version_id: Optional[str] = None
    created_at: Optional[datetime] = None


__all__ = ["Item", "ItemPydantic"]
