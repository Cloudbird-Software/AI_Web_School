"""§2.3 item_template 母题不变身份表 ORM + Pydantic（T-W1-003）.

宪法 D1：item_template 行只增不改（current_version_id 除外，仅发布事务前移）；
宪法 A7：A/B 级生产线对等——母题身份与题目身份同构（两段式：身份+版本）。

列与 alembic/versions/0002_item_model.py::_create_item_template 逐字对齐：
- template_id text PK
- pack_id text NOT NULL
- current_version_id text FK→item_template_version（DEFERRABLE，循环外键）
- created_at timestamptz NOT NULL server_default now()
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict
from sqlalchemy import DateTime, ForeignKey, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from src.core.models._base import Base


class ItemTemplate(Base):
    """§2.3 item_template 母题不变身份 ORM 映射.

    一行 = 一个母题的"身份"（template_id），跨版本不变；
    母题定义随时间产生多个 item_template_version 行，current_version_id 指向
    最新 published 版本。
    """

    __tablename__ = "item_template"

    template_id: Mapped[str] = mapped_column(Text, primary_key=True)
    pack_id: Mapped[str] = mapped_column(Text, nullable=False)
    # 循环外键：item_template_version.template_id → item_template.template_id 与
    # 本列形成环；DEFERRABLE INITIALLY DEFERRED（契约 §6.1）
    current_version_id: Mapped[Optional[str]] = mapped_column(
        Text,
        ForeignKey(
            "item_template_version.template_version_id",
            name="fk_item_template_current_version",
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
            f"ItemTemplate(template_id={self.template_id!r}, "
            f"pack_id={self.pack_id!r})"
        )


class ItemTemplatePydantic(BaseModel):
    """ItemTemplate 的 Pydantic 表示。"""

    model_config = ConfigDict(extra="forbid")

    template_id: str
    pack_id: str
    current_version_id: Optional[str] = None
    created_at: Optional[datetime] = None


__all__ = ["ItemTemplate", "ItemTemplatePydantic"]
