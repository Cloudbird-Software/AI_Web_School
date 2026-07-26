"""§2.5 corpus_version 语料库版本表 ORM + Pydantic（T-W1-003）.

宪法 D1：corpus_version 行永不 UPDATE/DELETE；
宪法 D3：version_id = 内容寻址 digest，进入 §3 公式一的 corpus_digests 链。

列与 alembic/versions/0002_item_model.py::_create_corpus_version 逐字对齐：
- version_id text PK（内容寻址 digest）
- asset_id text NOT NULL FK→corpus_asset
- content_ref text NOT NULL（对象存储引用）
- license_id text NOT NULL FK→material_license（语料库同样受 R-Q-18 许可约束）
- lineage jsonb NOT NULL（生产谱系，同 §2.2.2 结构）
- status item_version_status_enum NOT NULL（与 item_version/material_version 共用 enum）
- created_at timestamptz NOT NULL server_default now()

注：corpus_version 当前无 published_at/retired_at/gate_certificate_id 字段
（迁移 0002 未建）；如未来需要，由后续迁移补字段，本 ORM 同步增列。
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


class CorpusVersion(Base):
    """§2.5 corpus_version 语料库版本 ORM 映射.

    一行 = 一个语料库的某个版本快照（version_id = 内容寻址 digest）；
    被生产线与校验门共同消费（架构 v2 §4.1 B 线）；digest 进实例寻址链
    （§3 公式一的 corpus_digests 参数）。
    """

    __tablename__ = "corpus_version"

    version_id: Mapped[str] = mapped_column(Text, primary_key=True)
    asset_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("corpus_asset.asset_id", name="fk_cv_asset"),
        nullable=False,
    )
    content_ref: Mapped[str] = mapped_column(Text, nullable=False)
    license_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("material_license.license_id", name="fk_cv_license"),
        nullable=False,
    )
    lineage: Mapped[dict] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(
        item_version_status_enum, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    def __repr__(self) -> str:  # pragma: no cover - 调试辅助
        return (
            f"CorpusVersion(version_id={self.version_id!r}, "
            f"asset_id={self.asset_id!r}, status={self.status!r})"
        )


class CorpusVersionPydantic(BaseModel):
    """CorpusVersion 的 Pydantic 表示.

    lineage 复用 Lineage（同 item_version.lineage 结构）。
    """

    model_config = ConfigDict(extra="forbid")

    version_id: str
    asset_id: str
    content_ref: str
    license_id: str
    lineage: Lineage
    status: Literal["draft", "quarantined", "published", "retired"]
    created_at: Optional[datetime] = None


__all__ = ["CorpusVersion", "CorpusVersionPydantic"]
