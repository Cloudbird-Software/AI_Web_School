"""T-W4-004 题目生命周期状态机 ORM（架构 v2 §4.7 / 宪法 D1）.

item_lifecycle_transition 行 = 一次状态变更。append-only 账（迁移 0018 触发器
物理强制禁 UPDATE/DELETE）。当前状态 = 该 item 最新 transition 的 to_state。

四态状态机（架构 v2 §4.7）：
    ACTIVE ↔ WATCH（自动，基于健康度）
    WATCH → QUARANTINED（需门证书）
    QUARANTINED → WATCH（需门证书，释放回观察）
    QUARANTINED → RETIRED（需门证书）
    ACTIVE/WATCH → RETIRED（需门证书）
    RETIRED 为终态，无任何回边

退役不删除（D1）：RETIRED 的 item 历史版本保留，查询活跃池时排除 RETIRED。

列与 alembic/versions/0018_item_lifecycle.py 逐字对齐（created_at 默认值
例外：0018 建表时为 now()，0023 修正为 clock_timestamp()，见列注释）。

宪法 A5/X6：本 ORM 是核心域，禁止 import 任何学科包/学段包。
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Optional
from enum import Enum

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    Text,
)
from sqlalchemy.dialects.postgresql import ENUM as PG_ENUM
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from src.core.models._base import Base


# ────────────────────────────────────────────────────────────────────
# 生命周期四态（Python 枚举）
# ────────────────────────────────────────────────────────────────────


class ItemLifecycleState(str, Enum):
    """题目生命周期四态.

    - ACTIVE：活跃池，正常使用
    - WATCH：观察态，健康度异常但未隔离（自动转换）
    - QUARANTINED：隔离态，需门证书进入；不在活跃池
    - RETIRED：退役终态，需门证书进入；历史版本保留但不在活跃池
    """

    ACTIVE = "ACTIVE"
    WATCH = "WATCH"
    QUARANTINED = "QUARANTINED"
    RETIRED = "RETIRED"


# 合法状态集合（用于校验）
LIFECYCLE_STATES: frozenset[str] = frozenset(
    s.value for s in ItemLifecycleState
)

# 活跃池状态集合（查询活跃池时用）：排除 QUARANTINED 与 RETIRED
ACTIVE_POOL_STATES: frozenset[str] = frozenset(
    {ItemLifecycleState.ACTIVE.value, ItemLifecycleState.WATCH.value}
)

# 终态集合（无任何回边）
TERMINAL_STATES: frozenset[str] = frozenset({ItemLifecycleState.RETIRED.value})


class ItemLifecycleTransition(Base):
    """§4.7 生命周期状态变更账 ORM 映射.

    一行 = 一次状态变更；append-only（D1 物理强制）。
    当前状态 = 该 item_id 最新 transition（按 created_at 排序）的 to_state。
    """

    __tablename__ = "item_lifecycle_transition"

    transition_id: Mapped[str] = mapped_column(Text, primary_key=True)
    item_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("item.item_id", name="fk_ilt_item", ondelete="RESTRICT"),
        nullable=False,
    )
    # from_state NULL = 初始 INSERT（首次进入 ACTIVE）
    from_state: Mapped[Optional[str]] = mapped_column(
        PG_ENUM(
            "ACTIVE", "WATCH", "QUARANTINED", "RETIRED",
            name="item_lifecycle_state_enum",
            create_type=False,
        ),
        nullable=True,
    )
    to_state: Mapped[str] = mapped_column(
        PG_ENUM(
            "ACTIVE", "WATCH", "QUARANTINED", "RETIRED",
            name="item_lifecycle_state_enum",
            create_type=False,
        ),
        nullable=False,
    )
    # 门证书引用（WATCH→QUARANTINED、任何→RETIRED 必填；ACTIVE↔WATCH 可空）
    # 不挂 ORM FK：与 item_version.gate_certificate_id 同手法，应用层校验合法性
    gate_certificate_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # 变更时刻健康度快照（审计用）
    health_score: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(4, 3), nullable=True
    )
    anomaly_tags: Mapped[Optional[list]] = mapped_column(JSONB, nullable=True)
    # clock_timestamp() 而非 now()（nightly #58 根因）：now() 是事务级稳定时间戳，
    # 同一事务内多次状态变更（铁律 9：一次业务写入=一个事务）created_at 完全相同，
    # "最新 transition" 排序退化为 ULID tiebreak——ulid-py 同毫秒内随机部分与生成
    # 顺序无关，~50% 倒挂 → RETIRED 后仍被算进活跃池。clock_timestamp() 是语句级
    # 真实时钟，PG 保证同事务内先后语句严格递增，排序与插入顺序恒一致（迁移 0023）。
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.clock_timestamp(),
    )

    __table_args__ = (
        CheckConstraint(
            "health_score IS NULL OR (health_score >= 0 AND health_score <= 1)",
            name="ck_ilt_health_score_domain",
        ),
        Index(
            "ix_item_lifecycle_item_created",
            "item_id",
            "created_at",
        ),
        Index("ix_item_lifecycle_to_state", "to_state"),
    )

    def __repr__(self) -> str:  # pragma: no cover - 调试辅助
        return (
            f"ItemLifecycleTransition(transition_id={self.transition_id!r}, "
            f"item_id={self.item_id!r}, "
            f"from_state={self.from_state!r}, to_state={self.to_state!r})"
        )


__all__ = [
    "ItemLifecycleState",
    "ItemLifecycleTransition",
    "LIFECYCLE_STATES",
    "ACTIVE_POOL_STATES",
    "TERMINAL_STATES",
]
