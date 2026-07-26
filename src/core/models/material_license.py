"""§2.4 material_license 素材许可表 ORM + Pydantic（T-W1-003 支撑表）.

R-Q-18：素材/语料库入库必须持 approved 状态的许可；来源不合规无法入库。
本表为 material_version.license_id 与 corpus_version.license_id 的 FK 目标。

为什么本表也建 ORM：MaterialVersion 与 CorpusVersion 的 ORM 列声明了
ForeignKey("material_license.license_id", ...)；SQLAlchemy 在 flush 时需要
解析 FK 目标表以排序依赖，无 ORM 模型则 metadata 中无此表，触发
NoReferencedTableError。本表虽不在「九实体」之列（九实体是统一内容模型的
核心实体），但是它们的 FK 依赖目标，必须建模。

列与 alembic/versions/0002_item_model.py::_create_material_license 逐字对齐：
- license_id text PK
- source text nullable（来源）
- rights_holder text nullable（权利人）
- scope text nullable（用途范围）
- expires_at timestamptz nullable（期限）
- decision material_license_decision_enum NOT NULL（approved/rejected/expired）
- created_at timestamptz NOT NULL server_default now()
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict
from sqlalchemy import DateTime, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from src.core.models._base import Base, material_license_decision_enum


class MaterialLicense(Base):
    """§2.4 material_license ORM 映射.

    一行 = 一条素材许可决策记录；decision=approved 的行可被 material_version /
    corpus_version 引用（R-Q-18/R-G-03）。
    """

    __tablename__ = "material_license"

    license_id: Mapped[str] = mapped_column(Text, primary_key=True)
    source: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    rights_holder: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    scope: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    expires_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    decision: Mapped[str] = mapped_column(
        material_license_decision_enum, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    def __repr__(self) -> str:  # pragma: no cover - 调试辅助
        return (
            f"MaterialLicense(license_id={self.license_id!r}, "
            f"decision={self.decision!r})"
        )


class MaterialLicensePydantic(BaseModel):
    """MaterialLicense 的 Pydantic 表示。"""

    model_config = ConfigDict(extra="forbid")

    license_id: str
    source: Optional[str] = None
    rights_holder: Optional[str] = None
    scope: Optional[str] = None
    expires_at: Optional[datetime] = None
    decision: Literal["approved", "rejected", "expired"]
    created_at: Optional[datetime] = None


__all__ = ["MaterialLicense", "MaterialLicensePydantic"]
