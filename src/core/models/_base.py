"""核心域 ORM 基类与共享 PG ENUM 类型（T-W1-003）.

宪法 A5/A7：核心域零学科特判——本包不 import 任何学科包/学段包；
本基类仅承载统一内容模型（item 族 / material 族 / corpus 族 / item_group）
的声明式基类与跨表复用的 PostgreSQL ENUM 类型。

为什么用 DeclarativeBase（SQLAlchemy 2.0 风格）：项目 pyproject 锁定
sqlalchemy[asyncio]>=2.0；新代码采用 2.0 的 Mapped/mapped_column 声明式，
旧 declarative_base() 在 2.0 仍可用但已 deprecated。

为什么 ENUM 用 create_type=False：所有 PG ENUM 已由迁移 0002 创建
（ ENUM_DEFINITIONS in alembic/versions/0002_item_model.py ）；ORM 端
若再尝试 CREATE TYPE 会报 duplicate_object，故显式禁用类型创建。
"""
from __future__ import annotations

from sqlalchemy.dialects.postgresql import ENUM as PG_ENUM
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """所有核心域 ORM 实体的声明式基类。

    宪法 D1 三本账只增不改：item/item_version/material_version/corpus_version
    等内容版本表均为只增；ORM 层不暴露 update/delete 便捷方法（应用层直写由
    DB 触发器兜底拒绝，见迁移 0003/0004）。本基类仅提供声明式入口，不增减
    任何行为。
    """


# ────────────────────────────────────────────────────────────────────
# 跨表复用的 PG ENUM 类型（迁移 0002 已 CREATE TYPE，此处仅声明引用）
# ────────────────────────────────────────────────────────────────────

# A7：四级生产线对等，tier 为谱系字段（不是分区键）
item_tier_enum: PG_ENUM = PG_ENUM(
    "A", "B", "C", "D",
    name="item_tier_enum",
    create_type=False,
)

# §4 状态机：draft → quarantined → published → retired（无回边）
# 被 item_version / material_version / corpus_version 三表复用
item_version_status_enum: PG_ENUM = PG_ENUM(
    "draft", "quarantined", "published", "retired",
    name="item_version_status_enum",
    create_type=False,
)

# §2.3 母题版本状态：draft/published/retired（无 quarantined，母题不直接过门）
item_template_version_status_enum: PG_ENUM = PG_ENUM(
    "draft", "published", "retired",
    name="item_template_version_status_enum",
    create_type=False,
)

# §2.4 素材类型
material_kind_enum: PG_ENUM = PG_ENUM(
    "passage", "image", "table", "audio",
    name="material_kind_enum",
    create_type=False,
)

# §2.4 许可决策（material_license.decision 用）
material_license_decision_enum: PG_ENUM = PG_ENUM(
    "approved", "rejected", "expired",
    name="material_license_decision_enum",
    create_type=False,
)


__all__ = [
    "Base",
    "item_tier_enum",
    "item_version_status_enum",
    "item_template_version_status_enum",
    "material_kind_enum",
    "material_license_decision_enum",
]
