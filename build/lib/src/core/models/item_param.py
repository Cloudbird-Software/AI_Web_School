"""架构 v2 §4.7 item_param 参数标定表 ORM（W3 S8）.

item_param 行 = 一次估计运行对某题版本在某场景下的参数产出；
表只增不改（新估计 = 新行，as_of + method_version 区分；D6 估计器可替换）。

列与 alembic/versions/0010_item_param.py::_create_item_param 逐字对齐：
- param_id text PK
- item_version_id text NOT NULL FK→item_version
- purpose_scope text NOT NULL（practice/diagnosis/measurement，D5 禁混估）
- source text NOT NULL（prior_rule/prior_expert/measured_*）
- params jsonb NOT NULL（{difficulty, discrimination…}；开放题按维度内层键）
- sample_size int NOT NULL（>=0）
- method_version text NOT NULL
- as_of timestamptz NOT NULL
- created_at timestamptz NOT NULL server_default now()
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from src.core.models._base import Base


class ItemParam(Base):
    """题目参数标定行 ORM 映射.

    宪法 D5：source（先验/实测）与 purpose_scope（场景）分开存储、分开估计；
    UNIQUE(item_version_id, purpose_scope, source, method_version, as_of)
    承载同运行的幂等写入。本表只增不改（D1 风格，DB 触发器物理强制）。
    """

    __tablename__ = "item_param"

    param_id: Mapped[str] = mapped_column(Text, primary_key=True)
    item_version_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey(
            "item_version.item_version_id",
            name="fk_item_param_item_version",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    purpose_scope: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    params: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    sample_size: Mapped[int] = mapped_column(Integer, nullable=False)
    method_version: Mapped[str] = mapped_column(Text, nullable=False)
    as_of: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    __table_args__ = (
        UniqueConstraint(
            "item_version_id",
            "purpose_scope",
            "source",
            "method_version",
            "as_of",
            name="uq_item_param_identity",
        ),
        CheckConstraint(
            "purpose_scope IN ('practice', 'diagnosis', 'measurement')",
            name="ck_item_param_purpose_scope_domain",
        ),
        CheckConstraint(
            "source ~ '^(prior_rule|prior_expert|measured_.+)$'",
            name="ck_item_param_source_domain",
        ),
        CheckConstraint(
            "sample_size >= 0",
            name="ck_item_param_sample_size_nonneg",
        ),
    )

    def __repr__(self) -> str:  # pragma: no cover - 调试辅助
        return (
            f"ItemParam(param_id={self.param_id!r}, "
            f"item_version_id={self.item_version_id!r}, "
            f"purpose_scope={self.purpose_scope!r}, source={self.source!r})"
        )


__all__ = ["ItemParam"]
