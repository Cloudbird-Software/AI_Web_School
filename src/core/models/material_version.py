"""§2.4 material_version 素材不可变内容快照表 ORM + Pydantic（T-W1-003）.

宪法 D1：material_version 行永不 UPDATE/DELETE；
宪法 D3：material_version_id = §3 公式三 H(content_digest)；
宪法 R-Q-18：license_id 必须指向 approved 状态的 material_license（来源不合规无法入库）。

列与 alembic/versions/0002_item_model.py::_create_material_version 逐字对齐：
- material_version_id text PK（H(content_digest)）
- material_id text NOT NULL FK→material
- content_ref text NOT NULL（对象存储引用，内容哈希寻址）
- license_id text NOT NULL FK→material_license
- status item_version_status_enum NOT NULL（draft/quarantined/published/retired，
  与 item_version 共用同一 enum）
- lineage jsonb NOT NULL（生产谱系，同 §2.2.2 结构）
- gate_certificate_id text nullable（唯一真源）
- published_at timestamptz nullable（DB CHECK 强制非空必伴随 gate_certificate_id）
- retired_at timestamptz nullable
- created_at timestamptz NOT NULL server_default now()
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict
from sqlalchemy import DateTime, ForeignKey, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from src.core.models._base import Base, item_version_status_enum
from src.core.models.item_version import Lineage


class MaterialVersion(Base):
    """§2.4 material_version 素材不可变内容快照 ORM 映射.

    一行 = 一个素材的某个版本快照（material_version_id = H(content_digest)），
    永不 UPDATE/DELETE；lineage 复用 §2.2.2 结构（同 item_version.lineage）。
    """

    __tablename__ = "material_version"

    material_version_id: Mapped[str] = mapped_column(Text, primary_key=True)
    material_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("material.material_id", name="fk_mv_material"),
        nullable=False,
    )
    content_ref: Mapped[str] = mapped_column(Text, nullable=False)
    license_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("material_license.license_id", name="fk_mv_license"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(
        item_version_status_enum, nullable=False
    )
    lineage: Mapped[dict] = mapped_column(JSONB, nullable=False)
    gate_certificate_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    published_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    retired_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    def __repr__(self) -> str:  # pragma: no cover - 调试辅助
        return (
            f"MaterialVersion(material_version_id={self.material_version_id!r}, "
            f"material_id={self.material_id!r}, status={self.status!r})"
        )


class MaterialVersionPydantic(BaseModel):
    """MaterialVersion 的 Pydantic 表示.

    lineage 复用 Lineage（同 item_version.lineage 结构，契约 §2.4 注明）。
    """

    model_config = ConfigDict(extra="forbid")

    material_version_id: str
    material_id: str
    content_ref: str
    license_id: str
    status: Literal["draft", "quarantined", "published", "retired"]
    lineage: Lineage
    gate_certificate_id: Optional[str] = None
    published_at: Optional[datetime] = None
    retired_at: Optional[datetime] = None
    created_at: Optional[datetime] = None


__all__ = ["MaterialVersion", "MaterialVersionPydantic"]
