"""§4.4 双向细目表 ORM（T-W4-027）.

spec_table 表 = 一份双向细目表的版本化资产行（D1 只增不改）。
- 复合主键 (spec_table_id, spec_table_version)：D1 版本账自然键
- gradeband text NOT NULL（L/M/H）
- graph_release text NOT NULL（引用的知识图谱 release id）
- cells jsonb NOT NULL（list[SpecCell]；schema 在 Pydantic 层校验）
- created_at timestamptz NOT NULL server_default now()
- created_by text NOT NULL

为什么 PK 复合 (id, version)：D1 版本账语义——同 id 改版递增 version，老版本
保留供历史报告引用；与 item_param 的 (item_version_id, purpose_scope, source,
method_version, as_of) UNIQUE 同手法（身份+版本共同标识一行）。

为什么 cells 用 JSONB 而非展开为单元格子表：
- 单元格数量小（典型 2 知识点 × 2 认知 = 4 格；上限数十格），JSONB 索引/查询足够
- schema 校验在 Pydantic 层（SpecTable 模型），DB 层只做容器；与 item_version
  六大块 JSONB 同手法（D1 内容寻址哈希输入）
- 避免给细目表加子表带来的「表 join 才能取一份细目表」复杂度

为什么不挂 FK 到 kp_node：kp_node 本身随 graph_release 版本化，跨版本 FK 无法
表达；存在性校验在应用层 SpecTable.validate_against_graph 做（运行期对照
graph_release 对应的 kp_node.code 集合）。与 item_version.lineage.graph_release
同手法（不挂 FK，引用关系在应用层校验）。

append-only 触发器（D1 物理强制）：与 item_version / response_event /
gate_certificate / item_lifecycle_transition 同手法——细目表是版本账，
改表 = 新行（新版本），禁止 UPDATE/DELETE 历史。

宪法 A5/A7：本模块不 import 任何学科包/学段包。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    PrimaryKeyConstraint,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from src.core.models._base import Base


class SpecTable(Base):
    """双向细目表 ORM 行.

    一行 = 一份细目表的特定版本（复合主键 spec_table_id + spec_table_version）。
    cells 字段是 list[SpecCell dict]；schema 校验在 Pydantic 层（SpecTable 模型）
    落地，DB 层只保证容器合法（jsonb + NOT NULL）。

    宪法 D1：本表行只增不改；改表 = INSERT 新版本行。
    """

    __tablename__ = "spec_table"

    spec_table_id: Mapped[str] = mapped_column(Text, nullable=False)
    spec_table_version: Mapped[str] = mapped_column(Text, nullable=False)
    gradeband: Mapped[str] = mapped_column(Text, nullable=False)
    graph_release: Mapped[str] = mapped_column(Text, nullable=False)
    cells: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    created_by: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        # D1 版本账自然键：身份 + 版本共同标识一行
        PrimaryKeyConstraint(
            "spec_table_id",
            "spec_table_version",
            name="pk_spec_table",
        ),
        CheckConstraint(
            "gradeband IN ('L', 'M', 'H')",
            name="ck_spec_table_gradeband_domain",
        ),
        # cells 必须是数组（jsonb_typeof 兜底；空数组在应用层校验）
        CheckConstraint(
            "jsonb_typeof(cells) = 'array'",
            name="ck_spec_table_cells_is_array",
        ),
    )

    def __repr__(self) -> str:  # pragma: no cover - 调试辅助
        return (
            f"SpecTable(spec_table_id={self.spec_table_id!r}, "
            f"spec_table_version={self.spec_table_version!r}, "
            f"gradeband={self.gradeband!r})"
        )


__all__ = ["SpecTable"]
