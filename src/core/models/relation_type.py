"""§4.2 relation_type 关系类型元数据表 ORM + Pydantic（T-W2-012）.

架构 v2 §4.2：relation_type 元数据（directed/transitive/acyclic/symmetric）
承载图算法语义——
- directed=true：边有方向（默认）
- transitive=true：触发 kp_closure 闭包展开（T-W2-013）
- acyclic=true：闭包计算时校验无环（防止先修/组成出现循环依赖）
- symmetric=true：对称边自动反向存在（如 confusable 易混淆）

宪法 A5/A7：本包不 import 任何学科包/学段包（学科零特判）。

列与 alembic/versions/0006_knowledge_graph.py::_create_relation_type 逐字对齐：
- rel_type text PK（如 'prerequisite' / 'confusable' / 'composes'）
- pack_id text（NULL = 平台级通用）
- directed bool NOT NULL default true
- transitive bool NOT NULL default false
- acyclic bool NOT NULL default true
- symmetric bool NOT NULL default false
- description text
- created_at timestamptz NOT NULL server_default now()
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict
from sqlalchemy import Boolean, DateTime, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from src.core.models._base import Base


class RelationType(Base):
    """§4.2 relation_type 关系类型元数据 ORM 映射.

    一行 = 一种边类型的元数据定义（rel_type 主键）；
    不可变语义——rel_type 一经发布冻结（与 kp_node.code 同纪律）。
    """

    __tablename__ = "relation_type"

    rel_type: Mapped[str] = mapped_column(Text, primary_key=True)
    pack_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    directed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=func.true(),
    )
    transitive: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=func.false(),
    )
    acyclic: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=func.true(),
    )
    symmetric: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=func.false(),
    )
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    def __repr__(self) -> str:  # pragma: no cover - 调试辅助
        return (
            f"RelationType(rel_type={self.rel_type!r}, "
            f"directed={self.directed!r}, transitive={self.transitive!r}, "
            f"acyclic={self.acyclic!r}, symmetric={self.symmetric!r})"
        )


class RelationTypePydantic(BaseModel):
    """RelationType 的 Pydantic 表示（API 入参/序列化用）."""

    model_config = ConfigDict(extra="forbid")

    rel_type: str
    pack_id: Optional[str] = None
    directed: bool = True
    transitive: bool = False
    acyclic: bool = True
    symmetric: bool = False
    description: Optional[str] = None
    created_at: Optional[datetime] = None


__all__ = ["RelationType", "RelationTypePydantic"]
