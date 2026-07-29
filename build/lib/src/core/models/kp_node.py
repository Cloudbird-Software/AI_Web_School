"""§4.2 kp_node 知识节点表 ORM + Pydantic（T-W2-012）.

架构 v2 §4.2 知识域核心实体：知识图谱的节点。
- dimension 一等公民（'kp' / 'error_type' / 'literacy' 等）
- code 冻结语义：发布后不可改义，修订走 deprecated+supersede 链
- 演进：valid_from/valid_to + supersedes_id 自引用

宪法 A5/A7：本包不 import 任何学科包/学段包（学科零特判）。

列与 alembic/versions/0006_knowledge_graph.py::_create_kp_node 逐字对齐：
- node_id text PK（ULID）
- pack_id text NOT NULL（subject-math 等；学科包 id，核心域不解释语义）
- dimension text NOT NULL（'kp' 等，供图谱分面查询）
- code text NOT NULL（如 'math.nal.decimal.compare'）
- title text NOT NULL
- std_anchor text（课标锚点，如 '课标2022.nal.3-4.3'）
- gradeband text（'L'/'M'/'H'；多学段 'L,M'；NULL = 跨学段通用）
- status kp_node_status_enum NOT NULL（draft/active/deprecated/superseded）
- valid_from / valid_to timestamptz（演进有效期）
- supersedes_id text FK→kp_node（DEFERRABLE 自引用，supersede 链）
- created_at timestamptz NOT NULL server_default now()
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict
from sqlalchemy import DateTime, ForeignKey, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import ENUM as PG_ENUM
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from src.core.models._base import Base


# 知识节点状态枚举（迁移 0006 已 CREATE TYPE，此处仅声明引用）
kp_node_status_enum: PG_ENUM = PG_ENUM(
    "draft", "active", "deprecated", "superseded",
    name="kp_node_status_enum",
    create_type=False,
)


class KpNode(Base):
    """§4.2 kp_node 知识节点 ORM 映射.

    一行 = 知识图谱中的一个节点（node_id ULID），跨版本不变；
    修订=新编码+旧编码 deprecated+supersede（架构 v2 §4.2 演进纪律）。
    """

    __tablename__ = "kp_node"
    __table_args__ = (
        UniqueConstraint("pack_id", "dimension", "code", name="uq_kp_node_pack_dim_code"),
    )

    node_id: Mapped[str] = mapped_column(Text, primary_key=True)
    pack_id: Mapped[str] = mapped_column(Text, nullable=False)
    dimension: Mapped[str] = mapped_column(Text, nullable=False)
    code: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    std_anchor: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    gradeband: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(
        kp_node_status_enum, nullable=False, server_default=func.text("'draft'"),
    )
    valid_from: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    valid_to: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # 循环外键：supersedes_id → kp_node.node_id（DEFERRABLE INITIALLY DEFERRED）
    supersedes_id: Mapped[Optional[str]] = mapped_column(
        Text,
        ForeignKey(
            "kp_node.node_id",
            name="fk_kp_node_supersedes",
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
            f"KpNode(node_id={self.node_id!r}, pack_id={self.pack_id!r}, "
            f"dimension={self.dimension!r}, code={self.code!r}, status={self.status!r})"
        )


class KpNodePydantic(BaseModel):
    """KpNode 的 Pydantic 表示（API 入参/序列化用）."""

    model_config = ConfigDict(extra="forbid")

    node_id: str
    pack_id: str
    dimension: str
    code: str
    title: str
    std_anchor: Optional[str] = None
    gradeband: Optional[str] = None
    status: Literal["draft", "active", "deprecated", "superseded"] = "draft"
    valid_from: Optional[datetime] = None
    valid_to: Optional[datetime] = None
    supersedes_id: Optional[str] = None
    created_at: Optional[datetime] = None


__all__ = ["KpNode", "KpNodePydantic", "kp_node_status_enum"]
