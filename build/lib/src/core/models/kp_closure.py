"""§4.2 kp_closure 传递闭包扁平表 ORM + Pydantic（T-W2-013）.

架构 v2 §4.2：热路径只读扁平表，递归 CTE 仅管理查询。
闭包预计算后，先修链查询退化为单表过滤 O(1) 扫描。

宪法 A5/A7：本包不 import 任何学科包/学段包（学科零特判）。

列与 alembic/versions/0007_kp_closure_graph_release.py::_create_kp_closure 逐字对齐：
- closure_id BIGINT Identity PK
- graph_release_id text NOT NULL FK→graph_release
- src_node_id / dst_node_id text NOT NULL FK→kp_node
- rel_type text NOT NULL FK→relation_type
- depth int NOT NULL（1=直接边；>1=经传递展开的多跳可达）
- path_count int NOT NULL default 1（同 src→dst 同 depth 的不同路径数）
- created_at timestamptz NOT NULL server_default now()
- 唯一约束：(graph_release_id, src, dst, rel_type, depth)
- CHECK：depth>=1, path_count>=1, src<>dst
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict
from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, Identity, Integer, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from src.core.models._base import Base


class KpClosure(Base):
    """§4.2 kp_closure 传递闭包 ORM 映射.

    一行 = 一次闭包计算结果条目（按 graph_release 版本缓存）；
    同 (graph_release, src, dst, rel_type, depth) 唯一，path_count 承载多路径。
    """

    __tablename__ = "kp_closure"
    __table_args__ = (
        UniqueConstraint(
            "graph_release_id", "src_node_id", "dst_node_id", "rel_type", "depth",
            name="uq_kpc_release_src_dst_rel_depth",
        ),
        CheckConstraint("depth >= 1", name="ck_kpc_depth_positive"),
        CheckConstraint("path_count >= 1", name="ck_kpc_path_count_positive"),
        CheckConstraint("src_node_id <> dst_node_id", name="ck_kpc_no_self_loop"),
    )

    closure_id: Mapped[int] = mapped_column(
        BigInteger,
        Identity(always=True),
        primary_key=True,
    )
    graph_release_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("graph_release.release_id", name="fk_kpc_graph_release"),
        nullable=False,
    )
    src_node_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("kp_node.node_id", name="fk_kpc_src"),
        nullable=False,
    )
    dst_node_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("kp_node.node_id", name="fk_kpc_dst"),
        nullable=False,
    )
    rel_type: Mapped[str] = mapped_column(
        Text,
        ForeignKey("relation_type.rel_type", name="fk_kpc_rel_type"),
        nullable=False,
    )
    depth: Mapped[int] = mapped_column(Integer, nullable=False)
    path_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=func.text("1"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    def __repr__(self) -> str:  # pragma: no cover - 调试辅助
        return (
            f"KpClosure(closure_id={self.closure_id!r}, "
            f"graph_release_id={self.graph_release_id!r}, "
            f"src={self.src_node_id!r}, dst={self.dst_node_id!r}, "
            f"rel_type={self.rel_type!r}, depth={self.depth!r})"
        )


class KpClosurePydantic(BaseModel):
    """KpClosure 的 Pydantic 表示."""

    model_config = ConfigDict(extra="forbid")

    closure_id: Optional[int] = None
    graph_release_id: str
    src_node_id: str
    dst_node_id: str
    rel_type: str
    depth: int
    path_count: int = 1
    created_at: Optional[datetime] = None


__all__ = ["KpClosure", "KpClosurePydantic"]
