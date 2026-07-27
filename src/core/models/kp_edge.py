"""§4.2 kp_edge 知识边表 ORM + Pydantic（T-W2-012）.

架构 v2 §4.2 知识域核心实体：节点间的类型化关系。
- src/dst 节点引用（FK→kp_node）
- rel_type 关系类型（FK→relation_type）
- attrs JSONB 关系属性（先修强度/组成权重等）
- provenance JSONB 来源（课标/教研/AI/外部数据）
- 有效期 valid_from/valid_to：支持演进"按当时图谱"双模式查询

宪法 A5/A7：本包不 import 任何学科包/学段包（学科零特判）。

列与 alembic/versions/0006_knowledge_graph.py::_create_kp_edge 逐字对齐：
- edge_id BIGINT Identity PK
- src_node_id text NOT NULL FK→kp_node
- dst_node_id text NOT NULL FK→kp_node
- rel_type text NOT NULL FK→relation_type
- attrs jsonb NOT NULL default '{}'
- provenance jsonb NOT NULL default '{}'
- valid_from / valid_to timestamptz
- created_at timestamptz NOT NULL server_default now()
- 同源同宿同关系类型唯一（uq_kp_edge_src_dst_rel）
- 自环禁止（ck_kp_edge_no_self_loop）
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict
from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, Identity, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from src.core.models._base import Base


class KpEdge(Base):
    """§4.2 kp_edge 知识边 ORM 映射.

    一行 = 节点间的一条类型化关系（src→dst via rel_type）；
    同源同宿同关系类型唯一；自环禁止。
    """

    __tablename__ = "kp_edge"
    __table_args__ = (
        UniqueConstraint("src_node_id", "dst_node_id", "rel_type", name="uq_kp_edge_src_dst_rel"),
        CheckConstraint("src_node_id <> dst_node_id", name="ck_kp_edge_no_self_loop"),
    )

    edge_id: Mapped[int] = mapped_column(
        BigInteger,
        Identity(always=True),
        primary_key=True,
    )
    src_node_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("kp_node.node_id", name="fk_kpe_src"),
        nullable=False,
    )
    dst_node_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("kp_node.node_id", name="fk_kpe_dst"),
        nullable=False,
    )
    rel_type: Mapped[str] = mapped_column(
        Text,
        ForeignKey("relation_type.rel_type", name="fk_kpe_rel_type"),
        nullable=False,
    )
    attrs: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=func.text("'{}'::jsonb"),
    )
    provenance: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=func.text("'{}'::jsonb"),
    )
    valid_from: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    valid_to: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    def __repr__(self) -> str:  # pragma: no cover - 调试辅助
        return (
            f"KpEdge(edge_id={self.edge_id!r}, "
            f"src_node_id={self.src_node_id!r}, dst_node_id={self.dst_node_id!r}, "
            f"rel_type={self.rel_type!r})"
        )


class KpEdgePydantic(BaseModel):
    """KpEdge 的 Pydantic 表示（API 入参/序列化用）."""

    model_config = ConfigDict(extra="forbid")

    edge_id: Optional[int] = None
    src_node_id: str
    dst_node_id: str
    rel_type: str
    attrs: dict[str, Any] = {}
    provenance: dict[str, Any] = {}
    valid_from: Optional[datetime] = None
    valid_to: Optional[datetime] = None
    created_at: Optional[datetime] = None


__all__ = ["KpEdge", "KpEdgePydantic"]
