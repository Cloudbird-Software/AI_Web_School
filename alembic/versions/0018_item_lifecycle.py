"""T-W4-004 题目生命周期状态机迁移.

按架构 v2 §4.7「飞轮闭环」落地题目生命周期：
- 健康度评估（正确率异常/区分度低/干扰项无人选/耗时异常）
  → 状态机 ACTIVE→WATCH→QUARANTINED→RETIRED
- 退役不删除（D1 版本账只增不改）；签发制（WATCH→QUARANTINED、任何→RETIRED
  需门证书 gate_certificate_id）

为什么用独立 transition 账而非给 item 表加 lifecycle_state 列：
- item 表是「身份账」（D1 只增不改，除 current_version_id 前移外不可 UPDATE）
- 生命周期状态会随健康度变化（ACTIVE↔WATCH），若挂在 item 列则违反「item 只增不改」
- 独立 append-only transition 账：每次状态变更 INSERT 一行，当前状态 = 最新行的 to_state
- 与 gate_certificate 同手法：append-only 触发器物理强制禁 UPDATE/DELETE

迁移可逆性（make migrate-check）：upgrade→downgrade→upgrade 全绿。
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ENUM as PG_ENUM
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision: str = "0018"
down_revision: Union[str, None] = "0017"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# ────────────────────────────────────────────────────────────────────
# 枚举：生命周期四态
# ────────────────────────────────────────────────────────────────────

_LIFECYCLE_ENUM_NAME = "item_lifecycle_state_enum"
_LIFECYCLE_STATES = ("ACTIVE", "WATCH", "QUARANTINED", "RETIRED")


def _create_enum() -> None:
    """创建 item_lifecycle_state_enum（幂等）."""
    binding = op.get_bind()
    exists = binding.execute(
        sa.text("SELECT 1 FROM pg_type WHERE typname = :n"),
        {"n": _LIFECYCLE_ENUM_NAME},
    ).scalar()
    if exists:
        return
    values_sql = ", ".join(f"'{v}'" for v in _LIFECYCLE_STATES)
    binding.execute(sa.text(f"CREATE TYPE {_LIFECYCLE_ENUM_NAME} AS ENUM ({values_sql})"))


def _drop_enum() -> None:
    binding = op.get_bind()
    binding.execute(sa.text(f"DROP TYPE IF EXISTS {_LIFECYCLE_ENUM_NAME}"))


# ────────────────────────────────────────────────────────────────────
# append-only 触发器（D1 物理强制）
# ────────────────────────────────────────────────────────────────────

_TRIGGER_FUNCTION_SQL = """
CREATE OR REPLACE FUNCTION raise_lifecycle_append_only_error() RETURNS TRIGGER AS $$
BEGIN
    -- D1 生命周期账只增不改：状态变更走 INSERT 新行，禁止 UPDATE/DELETE。
    RAISE EXCEPTION 'item_lifecycle_transition is append-only (D1): UPDATE/DELETE forbidden';
END;
$$ LANGUAGE plpgsql;
"""

_TRIGGER_SQL = """
CREATE TRIGGER trg_item_lifecycle_append_only
    BEFORE UPDATE OR DELETE ON item_lifecycle_transition
    FOR EACH STATEMENT
    EXECUTE FUNCTION raise_lifecycle_append_only_error();
"""


def _create_trigger() -> None:
    binding = op.get_bind()
    binding.execute(sa.text(_TRIGGER_FUNCTION_SQL))
    binding.execute(sa.text(_TRIGGER_SQL))


def _drop_trigger() -> None:
    binding = op.get_bind()
    binding.execute(
        sa.text("DROP TRIGGER IF EXISTS trg_item_lifecycle_append_only ON item_lifecycle_transition")
    )
    binding.execute(sa.text("DROP FUNCTION IF EXISTS raise_lifecycle_append_only_error()"))


# ────────────────────────────────────────────────────────────────────
# 表：item_lifecycle_transition（append-only 状态变更账）
# ────────────────────────────────────────────────────────────────────


def _create_table() -> None:
    """建 item_lifecycle_transition 表.

    - transition_id：应用层 ULID，全局唯一
    - from_state：变更前状态；NULL = 初始 INSERT（ACTIVE 首次进入）
    - to_state：变更后状态（必填）
    - gate_certificate_id：门证书引用（WATCH→QUARANTINED、任何→RETIRED 必填；
      ACTIVE↔WATCH 自动转换可为 NULL）；不挂 FK：gate_certificate.cert_id 是 text，
      引用关系在应用层校验（与 item_version.gate_certificate_id 同手法）
    - reason：变更原因（异常标签 / 人工理由）
    - health_score / anomaly_tags：变更时刻的健康度快照（审计用）
    """
    op.create_table(
        "item_lifecycle_transition",
        sa.Column("transition_id", sa.Text(), primary_key=True),
        sa.Column("item_id", sa.Text(), nullable=False),
        sa.Column(
            "from_state",
            PG_ENUM(*_LIFECYCLE_STATES, name=_LIFECYCLE_ENUM_NAME, create_type=False),
            nullable=True,
        ),
        sa.Column(
            "to_state",
            PG_ENUM(*_LIFECYCLE_STATES, name=_LIFECYCLE_ENUM_NAME, create_type=False),
            nullable=False,
        ),
        sa.Column("gate_certificate_id", sa.Text(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("health_score", sa.Numeric(4, 3), nullable=True),
        sa.Column("anomaly_tags", JSONB(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["item_id"], ["item.item_id"], name="fk_ilt_item",
            ondelete="RESTRICT",
        ),
        # 健康度值域 CHECK（0.000~1.000，与 gate_run.confidence 同口径）
        sa.CheckConstraint(
            "health_score IS NULL OR (health_score >= 0 AND health_score <= 1)",
            name="ck_ilt_health_score_domain",
        ),
    )
    # 查询索引：按 item_id 取最新 transition（当前状态）
    op.create_index(
        "ix_item_lifecycle_item_created",
        "item_lifecycle_transition",
        ["item_id", "created_at"],
    )
    # 按 to_state 查询活跃池（排除 RETIRED 等）
    op.create_index(
        "ix_item_lifecycle_to_state",
        "item_lifecycle_transition",
        ["to_state"],
    )


def _drop_table() -> None:
    op.drop_table("item_lifecycle_transition")


# ────────────────────────────────────────────────────────────────────
# upgrade / downgrade
# ────────────────────────────────────────────────────────────────────


def upgrade() -> None:
    """建 item_lifecycle_transition 表 + 枚举 + append-only 触发器."""
    _create_enum()
    _create_table()
    _create_trigger()


def downgrade() -> None:
    """回滚：删表 + 枚举 + 触发器."""
    _drop_trigger()
    _drop_table()
    _drop_enum()
