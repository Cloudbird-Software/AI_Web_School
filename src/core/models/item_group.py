"""§2.5 item_group 题组/testlet 表 ORM + Pydantic（T-W1-003）.

R-Z-06：题组 ≤6 题（DB 层 CHECK 兜底 array_length(item_version_ids, 1) <= 6）；
D1：引用素材版本（material_version_id）而非素材身份，保证历史试卷可精确回溯。

列与 alembic/versions/0002_item_model.py::_create_item_group 逐字对齐：
- item_group_id text PK
- material_version_id text nullable FK→material_version（一材多题的"材"版本）
- item_version_ids text[] NOT NULL（题组内题目版本列表，组内顺序由 ordered 决定）
- ordered bool NOT NULL default false（true=固定顺序；false=可乱序）
- testlet bool NOT NULL default false（true=testlet 单元；false=普通题组）
- created_at timestamptz NOT NULL server_default now()

DB CHECK：ck_ig_max_six_items = array_length(item_version_ids, 1) <= 6。
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from src.core.models._base import Base


class ItemGroup(Base):
    """§2.5 item_group 题组/testlet ORM 映射.

    一行 = 一个题组（一材多题或纯题目集合）；引用素材版本（非素材身份）保证
    历史试卷可精确回溯（D1）。
    """

    __tablename__ = "item_group"

    item_group_id: Mapped[str] = mapped_column(Text, primary_key=True)
    material_version_id: Mapped[Optional[str]] = mapped_column(
        Text,
        ForeignKey(
            "material_version.material_version_id",
            name="fk_ig_material_version",
        ),
        nullable=True,
    )
    # text[]：题组内题目版本 id 列表，组内顺序由 ordered 决定
    item_version_ids: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False
    )
    ordered: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=func.false()
    )
    testlet: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=func.false()
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    # R-Z-06：题组 ≤6 题（DB 层 CHECK 兜底，迁移 0002 已创建）
    __table_args__ = (
        CheckConstraint(
            "array_length(item_version_ids, 1) <= 6",
            name="ck_ig_max_six_items",
        ),
    )

    def __repr__(self) -> str:  # pragma: no cover - 调试辅助
        return (
            f"ItemGroup(item_group_id={self.item_group_id!r}, "
            f"ordered={self.ordered!r}, testlet={self.testlet!r})"
        )


class ItemGroupPydantic(BaseModel):
    """ItemGroup 的 Pydantic 表示。

    用 Field(max_length=6) 在应用层前置校验题组 ≤6 题（R-Z-06），
    DB CHECK 是兜底；两层校验避免越界数据落库。
    """

    model_config = ConfigDict(extra="forbid")

    item_group_id: str
    material_version_id: Optional[str] = None
    item_version_ids: list[str] = Field(..., max_length=6)
    ordered: bool = False
    testlet: bool = False
    created_at: Optional[datetime] = None


__all__ = ["ItemGroup", "ItemGroupPydantic"]
