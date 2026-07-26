"""§2.4 material 素材不变身份表 ORM + Pydantic（T-W1-003）.

宪法 D1 全版本化：素材与 Item 同构——material 是不变身份，material_version 是
不可变内容快照；素材修订产生新版本，旧版本永不覆盖/删除。
宪法 A5：pack_id 为 'platform' 表示跨学科通用素材，核心域不解释其语义。

列与 alembic/versions/0002_item_model.py::_create_material 逐字对齐：
- material_id text PK（ULID）
- kind material_kind_enum NOT NULL（passage/image/table/audio）
- pack_id text nullable（跨学科通用素材为 'platform'）
- current_version_id text FK→material_version（DEFERRABLE，循环外键）
- created_at timestamptz NOT NULL server_default now()
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict
from sqlalchemy import DateTime, ForeignKey, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from src.core.models._base import Base, material_kind_enum


class Material(Base):
    """§2.4 material 素材不变身份 ORM 映射.

    一行 = 一个素材的"身份"（material_id），跨版本不变；
    内容随时间产生多个 material_version 行，current_version_id 指向最新
    published 版本（维护纪律同 item.current_version_id）。
    """

    __tablename__ = "material"

    material_id: Mapped[str] = mapped_column(Text, primary_key=True)
    kind: Mapped[str] = mapped_column(material_kind_enum, nullable=False)
    pack_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # 循环外键：material_version.material_id → material.material_id 与本列形成环；
    # DEFERRABLE INITIALLY DEFERRED（契约 §6.1）
    current_version_id: Mapped[Optional[str]] = mapped_column(
        Text,
        ForeignKey(
            "material_version.material_version_id",
            name="fk_material_current_version",
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
            f"Material(material_id={self.material_id!r}, "
            f"kind={self.kind!r}, pack_id={self.pack_id!r})"
        )


class MaterialPydantic(BaseModel):
    """Material 的 Pydantic 表示。"""

    model_config = ConfigDict(extra="forbid")

    material_id: str
    kind: Literal["passage", "image", "table", "audio"]
    pack_id: Optional[str] = None
    current_version_id: Optional[str] = None
    created_at: Optional[datetime] = None


__all__ = ["Material", "MaterialPydantic"]
