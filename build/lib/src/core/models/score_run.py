"""T-W4-003 score_run 平行重判结果账 ORM（架构 v2 §4.7 / 宪法 D6）.

score_run 行 = 一次重判结果。新 scorer 版本重放历史事件时写平行 score_run，
原 response_event.scoring_trace 永不改动（契约 §3 R-D-05 重判规则）。

为什么是独立账而非挂到 response_event：契约 §3 实现注记明确「rerun_of
属于 score_run，不属于 response_event」——本表是 response_event 的平行账，
不污染原表（原表 append-only，物理强制禁 UPDATE）。

列与 alembic/versions/0017_score_run.py 逐字对齐。

宪法 A5/X6：本 ORM 是核心域，禁止 import 任何学科包/学段包。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Index,
    Text,
    UniqueConstraint,
    text as sa_text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from src.core.models._base import Base


class ScoreRun(Base):
    """§4.7 平行重判结果行 ORM 映射.

    一行 = 一次重判结果；新 scorer 版本重放历史事件时写入；
    原始 response_event.scoring_trace 永不改动（D1 + R-D-05）。
    """

    __tablename__ = "score_run"

    score_run_id: Mapped[str] = mapped_column(Text, primary_key=True)
    event_id: Mapped[UUID] = mapped_column(nullable=False)
    event_created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    # 链式重判：指向上一级 score_run_id；NULL=直接重判原始事件
    rerun_of: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    purpose_scope: Mapped[str] = mapped_column(Text, nullable=False)
    scorer_id: Mapped[str] = mapped_column(Text, nullable=False)
    scorer_version: Mapped[str] = mapped_column(Text, nullable=False)
    # 原始事件当时的评分器版本（response_event.scoring_trace.scorer_version）
    original_scorer_version: Mapped[str] = mapped_column(Text, nullable=False)
    dimension_scores: Mapped[dict] = mapped_column(JSONB, nullable=False)
    scoring_trace: Mapped[dict] = mapped_column(JSONB, nullable=False)
    error_inferences: Mapped[list] = mapped_column(JSONB, nullable=False)
    correct: Mapped[bool] = mapped_column(Boolean, nullable=False)
    run_label: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    input_snapshot_id: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    __table_args__ = (
        # 复合外键 fk_score_run_response_event 由迁移 0017 在 DB 层强制
        # （response_event 无 ORM 模型——它是分区表，写入走裸 SQL record_event；
        # 与 item_version.gate_certificate_id 同手法，不在 ORM 端声明 FK）。
        # 幂等保护：同事件同批次标签不重复写入
        # （不含 scorer_version——后者是 Scorer 自报审计字段；同批次同事件幂等）
        UniqueConstraint(
            "event_id",
            "event_created_at",
            "run_label",
            name="uq_score_run_identity",
        ),
        CheckConstraint(
            "purpose_scope IN ('practice', 'diagnosis', 'measurement')",
            name="ck_score_run_purpose_scope_domain",
        ),
        Index(
            "ix_score_run_event",
            "event_id",
            "event_created_at",
        ),
        Index("ix_score_run_purpose_scope", "purpose_scope"),
        Index("ix_score_run_scorer_version", "scorer_version"),
        Index("ix_score_run_rerun_of", "rerun_of"),
        Index(
            "uq_score_run_identity_nonnull_label",
            "event_id",
            "event_created_at",
            "run_label",
            unique=True,
            postgresql_where=sa_text("run_label IS NOT NULL"),
        ),
    )

    def __repr__(self) -> str:  # pragma: no cover - 调试辅助
        return (
            f"ScoreRun(score_run_id={self.score_run_id!r}, "
            f"event_id={self.event_id!r}, "
            f"scorer_version={self.scorer_version!r}, "
            f"run_label={self.run_label!r})"
        )


__all__ = ["ScoreRun"]
