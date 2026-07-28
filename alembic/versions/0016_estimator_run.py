"""T-W4-002 估计器运行注册表（ActiveModelPointer 持久化形态）.

按架构 v2 §4.7 / 宪法 D6 落地估计器版本登记表：
- estimator_run(run_id, purpose_scope, model_version, code_digest,
  input_snapshot_id, graph_release_id, activated_at, retired_at, created_at)
- D6 估计器可替换：每次估计运行绑定 model_version + 代码 digest + 输入数据
  快照 + 图谱 release；历史报告按 timestamp 回溯当时活跃 model_version。
- D5 分场景：purpose_scope 三值域；偏唯一索引保证每场景至多一个活跃版本
  （retired_at IS NULL）。
- retired_at 由 ActiveModelPointer.set_active UPDATE 写入退役时间戳——本表
  是估计器版本操作元数据账，非 D1 三本账，不套 append-only 触发器。

迁移可逆性（make migrate-check）：upgrade→downgrade→upgrade 全绿。
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0016"
down_revision: Union[str, None] = "0015"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _create_estimator_run() -> None:
    """估计器运行登记表.

    - run_id：行 id（应用层 ULID）
    - purpose_scope：场景域 practice/diagnosis/measurement（D5）
    - model_version：估计方法版本（如 'ctt-v1'/'rasch-v1'，D6 可替换）
    - code_digest：估计器代码 SHA256（代码变更即换 digest，可追溯）
    - input_snapshot_id：输入数据快照标识（as_of + 数据指纹）
    - graph_release_id：估计时所用知识图谱 release 标识
    - activated_at：本版本被登记为活跃的时刻
    - retired_at：被后续版本取代时打戳的退役时刻（NULL=当前活跃）
    - created_at：行写入时刻
    """
    op.create_table(
        "estimator_run",
        sa.Column("run_id", sa.Text(), primary_key=True),
        sa.Column("purpose_scope", sa.Text(), nullable=False),
        sa.Column("model_version", sa.Text(), nullable=False),
        sa.Column("code_digest", sa.Text(), nullable=False),
        sa.Column("input_snapshot_id", sa.Text(), nullable=False),
        sa.Column("graph_release_id", sa.Text(), nullable=False),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "purpose_scope IN ('practice', 'diagnosis', 'measurement')",
            name="ck_estimator_run_purpose_scope_domain",
        ),
        sa.UniqueConstraint(
            "purpose_scope",
            "model_version",
            "activated_at",
            name="uq_estimator_run_identity",
        ),
    )


def _create_indexes() -> None:
    """常用查询索引 + 偏唯一索引（每场景至多一个活跃版本）."""
    op.create_index(
        "ix_estimator_run_purpose_scope",
        "estimator_run",
        ["purpose_scope"],
    )
    # 偏唯一：每个 purpose_scope 至多一个 retired_at IS NULL 的活跃版本
    op.create_index(
        "uq_estimator_run_one_active_per_scope",
        "estimator_run",
        ["purpose_scope"],
        unique=True,
        postgresql_where=sa.text("retired_at IS NULL"),
    )


def upgrade() -> None:
    """创建 estimator_run + 索引."""
    _create_estimator_run()
    _create_indexes()


def downgrade() -> None:
    """删除 estimator_run + 索引."""
    op.drop_index(
        "uq_estimator_run_one_active_per_scope", table_name="estimator_run"
    )
    op.drop_index("ix_estimator_run_purpose_scope", table_name="estimator_run")
    op.drop_table("estimator_run")
