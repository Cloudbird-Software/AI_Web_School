"""T-W4-002 估计器运行注册表 ORM（架构 v2 §4.7 / 宪法 D6）.

estimator_run 行 = 一次估计器版本登记（ActiveModelPointer 的持久化形态）。
每次估计运行绑定 model_version + 代码 digest + 输入数据快照 + 图谱 release
（D6 可重放与可追溯）；ActiveModelPointer.set_active 在此表登记活跃版本并
给旧版本记录退役时间戳。

为什么不是 append-only 三本账之一：本表是估计器版本操作的元数据账，
不是内容版本账/作答事件账/校验签发账（D1 三本账）。retired_at 由 set_active
UPDATE 写入（退役即打时间戳），不套 raise_append_only_error 触发器——
但 (purpose_scope) 上有「至多一个 retired_at IS NULL」的偏唯一索引，
保证每个场景同一时刻只有一个活跃估计器版本。

列与 alembic/versions/0016_estimator_run.py 逐字对齐。
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Index,
    Text,
    UniqueConstraint,
    text as sa_text,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from src.core.models._base import Base


class EstimatorRun(Base):
    """估计器版本登记行 ORM 映射.

    宪法 D6 估计器可替换：每次估计运行绑定 model_version + code_digest +
    输入数据快照 + 图谱 release；历史报告永远引用当时版本（get_params 按
    timestamp 回溯到当时活跃的 model_version）。
    D5 分场景：purpose_scope 必填，活跃指针每场景独立。
    """

    __tablename__ = "estimator_run"

    run_id: Mapped[str] = mapped_column(Text, primary_key=True)
    purpose_scope: Mapped[str] = mapped_column(Text, nullable=False)
    model_version: Mapped[str] = mapped_column(Text, nullable=False)
    # 估计器代码 digest（SHA256）——同 model_version 代码变更即换 digest，
    # 保证历史运行可追溯到确切代码（D6 可重放）。
    code_digest: Mapped[str] = mapped_column(Text, nullable=False)
    # 输入数据快照标识（as_of + 数据指纹）——估计运行所读数据快照的引用
    input_snapshot_id: Mapped[str] = mapped_column(Text, nullable=False)
    # 知识图谱 release 标识（估计时所用图谱版本）
    graph_release_id: Mapped[str] = mapped_column(Text, nullable=False)
    activated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    # 退役时间戳：NULL=当前活跃；被 set_active 后续版本打戳退役
    retired_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    __table_args__ = (
        CheckConstraint(
            "purpose_scope IN ('practice', 'diagnosis', 'measurement')",
            name="ck_estimator_run_purpose_scope_domain",
        ),
        # 同场景同版本同激活时刻不重复登记（幂等保护）
        UniqueConstraint(
            "purpose_scope",
            "model_version",
            "activated_at",
            name="uq_estimator_run_identity",
        ),
        # 偏唯一索引：每个场景至多一个 retired_at IS NULL 的活跃版本
        Index(
            "uq_estimator_run_one_active_per_scope",
            "purpose_scope",
            unique=True,
            postgresql_where=sa_text("retired_at IS NULL"),
        ),
    )

    def __repr__(self) -> str:  # pragma: no cover - 调试辅助
        return (
            f"EstimatorRun(run_id={self.run_id!r}, "
            f"purpose_scope={self.purpose_scope!r}, "
            f"model_version={self.model_version!r}, "
            f"retired_at={self.retired_at!r})"
        )


__all__ = ["EstimatorRun"]
