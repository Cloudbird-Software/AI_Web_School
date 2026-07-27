"""§4.2 graph_release 图谱版本表 ORM + Pydantic（T-W2-013）.

架构 v2 §4.2：图谱版本化缓存，支持"按当时图谱/映射到当前图谱"双模式查询。
- release_id 冻结语义（与 kp_node.code 同纪律）
- 演进：draft→active→frozen→superseded，superseded_by 自引用指向前版本

宪法 A5/A7：本包不 import 任何学科包/学段包（学科零特判）。

列与 alembic/versions/0007_kp_closure_graph_release.py::_create_graph_release 逐字对齐：
- release_id text PK（如 '2026.1'）
- status graph_release_status_enum NOT NULL default 'draft'
- valid_from / valid_to timestamptz
- superseded_by text FK→graph_release（DEFERRABLE 自引用）
- description text
- created_at timestamptz NOT NULL server_default now()
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict
from sqlalchemy import DateTime, ForeignKey, Text
from sqlalchemy.dialects.postgresql import ENUM as PG_ENUM
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from src.core.models._base import Base


# 图谱版本状态枚举（迁移 0007 已 CREATE TYPE，此处仅声明引用）
graph_release_status_enum: PG_ENUM = PG_ENUM(
    "draft", "active", "frozen", "superseded",
    name="graph_release_status_enum",
    create_type=False,
)


class GraphRelease(Base):
    """§4.2 graph_release 图谱版本 ORM 映射.

    一行 = 一个图谱版本（release_id），跨版本不变；
    superseded_by 指向取代它的新版本（演进纪律）。
    """

    __tablename__ = "graph_release"

    release_id: Mapped[str] = mapped_column(Text, primary_key=True)
    status: Mapped[str] = mapped_column(
        graph_release_status_enum, nullable=False, server_default=func.text("'draft'"),
    )
    valid_from: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    valid_to: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # 循环外键：superseded_by → graph_release.release_id（DEFERRABLE）
    superseded_by: Mapped[Optional[str]] = mapped_column(
        Text,
        ForeignKey(
            "graph_release.release_id",
            name="fk_graph_release_superseded_by",
            deferrable=True,
            initially="DEFERRED",
        ),
        nullable=True,
    )
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    def __repr__(self) -> str:  # pragma: no cover - 调试辅助
        return (
            f"GraphRelease(release_id={self.release_id!r}, "
            f"status={self.status!r})"
        )


class GraphReleasePydantic(BaseModel):
    """GraphRelease 的 Pydantic 表示."""

    model_config = ConfigDict(extra="forbid")

    release_id: str
    status: Literal["draft", "active", "frozen", "superseded"] = "draft"
    valid_from: Optional[datetime] = None
    valid_to: Optional[datetime] = None
    superseded_by: Optional[str] = None
    description: Optional[str] = None
    created_at: Optional[datetime] = None


__all__ = ["GraphRelease", "GraphReleasePydantic", "graph_release_status_enum"]
