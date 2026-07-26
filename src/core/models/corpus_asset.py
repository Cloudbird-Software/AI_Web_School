"""§2.5 corpus_asset 语料库身份表 ORM + Pydantic（T-W1-003）.

宪法 D1 全版本化：语料库与 Item/Material 同构——corpus_asset 是不变身份，
corpus_version 是不可变内容快照。
宪法 A5：pack_id 为 'platform' 表示跨学科通用语料库。

列与 alembic/versions/0002_item_model.py::_create_corpus_asset 逐字对齐：
- asset_id text PK
- kind text NOT NULL（字/词/篇/句/词表/音标/函数/图库——未走 enum，自由文本）
- pack_id text nullable
- current_version_id text FK→corpus_version（DEFERRABLE，循环外键）
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


class CorpusAsset(Base):
    """§2.5 corpus_asset 语料库身份 ORM 映射.

    一行 = 一个语料库的"身份"（asset_id），跨版本不变；
    内容随时间产生多个 corpus_version 行，current_version_id 指向最新版本。
    """

    __tablename__ = "corpus_asset"

    asset_id: Mapped[str] = mapped_column(Text, primary_key=True)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    pack_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # 循环外键：corpus_version.asset_id → corpus_asset.asset_id 与本列形成环；
    # DEFERRABLE INITIALLY DEFERRED（契约 §6.1）
    current_version_id: Mapped[Optional[str]] = mapped_column(
        Text,
        ForeignKey(
            "corpus_version.version_id",
            name="fk_corpus_asset_current_version",
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
            f"CorpusAsset(asset_id={self.asset_id!r}, "
            f"kind={self.kind!r}, pack_id={self.pack_id!r})"
        )


class CorpusAssetPydantic(BaseModel):
    """CorpusAsset 的 Pydantic 表示。"""

    model_config = ConfigDict(extra="forbid")

    asset_id: str
    kind: str
    pack_id: Optional[str] = None
    current_version_id: Optional[str] = None
    created_at: Optional[datetime] = None


__all__ = ["CorpusAsset", "CorpusAssetPydantic"]
