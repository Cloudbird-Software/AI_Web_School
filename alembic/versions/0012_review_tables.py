"""W3-report S6 复习排程表：review_policy + review_queue_entry.

按 ADR/母题与知识体系-工程架构设计方案v2.md#4.4 复习排程落地：
「纯函数策略接口 + 版本化 ReviewPolicy 产出可重建 ReviewQueue；
v1=固定间隔表（1/3/7/21 天）」。

两表职责分离：
- review_policy：策略版本账（只增不改，沿用 append-only 触发器模式）。
  一行 = 一个策略的某个版本；间隔表存 JSONB（如 [1, 3, 7, 21]）。
  队列重建 = 事件流 × 策略版本的纯函数回放，策略必须版本化才可重建。
- review_queue_entry：派生队列（非三本账，允许 UPDATE——队列状态随作答
  推进，其正确性由「可重建」保证：同一事件流 + 同一策略版本重放必得同态）。
  UNIQUE(student_alias_id, item_version_id, policy_id, policy_version)
  保证一个学生的一道题在同一策略版本下至多一条在队记录。

为什么 review_queue_entry 不加 append-only 触发器：三本账（内容版本/作答事件/
校验签发）之外的派生状态表不在 D1 范围；队列推进本质是状态机迁移，
且 review_policy 版本 + response_event 事件流可随时权威重建。

为什么迁移内置 v1.0.0 策略种子行：固定间隔表（1/3/7/21 天）是架构 §4.4
钦定的 v1 策略，落库种子保证任何环境下 rebuild 输入完整、行为确定。

迁移可逆性（make migrate-check）：upgrade→downgrade→upgrade 全绿。
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0012"
down_revision: Union[str, None] = "0011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# ────────────────────────────────────────────────────────────────────
# 触发器函数：review_policy 只增（策略版本账，沿用同模式）
# ────────────────────────────────────────────────────────────────────
_ENSURE_TRIGGER_FUNCTION_SQL = """
CREATE OR REPLACE FUNCTION raise_review_policy_append_only_error() RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'review_policy table is append-only: UPDATE/DELETE forbidden';
END;
$$ LANGUAGE plpgsql;
"""

_TRIGGER_REVIEW_POLICY_SQL = """
CREATE TRIGGER trg_review_policy_append_only
    BEFORE UPDATE OR DELETE ON review_policy
    FOR EACH STATEMENT
    EXECUTE FUNCTION raise_review_policy_append_only_error();
"""


# ────────────────────────────────────────────────────────────────────
# 表创建
# ────────────────────────────────────────────────────────────────────

def _create_review_policy() -> None:
    """复习策略版本表（只增）.

    - policy_id：策略族 id（v1 只有 'fixed-interval'；v2 候选 FSRS 另起 id）
    - policy_version：语义版本（同族内递增；PK 含版本 → 同族多版本并存）
    - intervals_days：固定间隔表（天），如 [1, 3, 7, 21]；JSONB 数组，
      元素为正整数、严格递增（应用层校验，DB CHECK 兜底非空数组）
    """
    op.create_table(
        "review_policy",
        sa.Column("policy_id", sa.Text(), nullable=False),
        sa.Column("policy_version", sa.Text(), nullable=False),
        sa.Column(
            "intervals_days",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint(
            "policy_id", "policy_version", name="pk_review_policy"
        ),
        sa.CheckConstraint(
            "jsonb_typeof(intervals_days) = 'array' "
            "AND jsonb_array_length(intervals_days) > 0",
            name="ck_review_policy_intervals_nonempty_array",
        ),
    )


def _create_review_queue_entry() -> None:
    """复习队列派生表（可 UPDATE 的状态机；非三本账）.

    - entry_id：队列记录 id（应用层 uuid）
    - stage：当前间隔索引（0-based，指向 policy.intervals_days）
    - status：pending=在队待复习 / done=走完最后一个间隔出队
    - due_at：下次到期时间；stage 推进时重算
    - source_error_type_id：入队/重置时的主要错误归因（报告联动用，可空）
    - last_event_id：驱动最近一次状态迁移的 response_event.event_id
      （幂等重建的定位锚；NULL=从未被事件推进过，仅初始入队）
    """
    op.create_table(
        "review_queue_entry",
        sa.Column("entry_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("student_alias_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("item_version_id", sa.Text(), nullable=False),
        sa.Column("policy_id", sa.Text(), nullable=False),
        sa.Column("policy_version", sa.Text(), nullable=False),
        sa.Column("stage", sa.Integer(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("source_error_type_id", sa.Text(), nullable=True),
        sa.Column("last_event_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("enqueued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["policy_id", "policy_version"],
            ["review_policy.policy_id", "review_policy.policy_version"],
            name="fk_review_queue_entry_policy",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "student_alias_id",
            "item_version_id",
            "policy_id",
            "policy_version",
            name="uq_review_queue_entry_student_item_policy",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'done')",
            name="ck_review_queue_entry_status_domain",
        ),
        sa.CheckConstraint(
            "stage >= 0",
            name="ck_review_queue_entry_stage_nonnegative",
        ),
    )


def _create_indexes() -> None:
    """到期取题热路径：按学生 + 状态 + 到期时间扫描."""
    op.create_index(
        "ix_review_queue_entry_due",
        "review_queue_entry",
        ["student_alias_id", "status", "due_at"],
    )


def _seed_policy_v1() -> None:
    """内置 v1.0.0 固定间隔策略（1/3/7/21 天，架构 §4.4 钦定 v1）."""
    op.execute(
        sa.text(
            "INSERT INTO review_policy "
            "(policy_id, policy_version, intervals_days, description) "
            "VALUES (:pid, :ver, CAST(:intervals AS jsonb), :desc)"
        ).bindparams(
            pid="fixed-interval",
            ver="1.0.0",
            intervals="[1, 3, 7, 21]",
            desc="W3 S6 复习排程 v1：固定间隔表 1/3/7/21 天（架构 §4.4）",
        )
    )


# ────────────────────────────────────────────────────────────────────
# Alembic 入口
# ────────────────────────────────────────────────────────────────────

def upgrade() -> None:
    """创建 review_policy/review_queue_entry + 触发器 + 索引 + v1 策略种子."""
    op.execute(_ENSURE_TRIGGER_FUNCTION_SQL)
    _create_review_policy()
    _create_review_queue_entry()
    _create_indexes()
    op.execute(_TRIGGER_REVIEW_POLICY_SQL)
    _seed_policy_v1()


def downgrade() -> None:
    """删除复习排程两表 + 触发器 + 函数."""
    op.execute(
        "DROP TRIGGER IF EXISTS trg_review_policy_append_only ON review_policy"
    )
    op.drop_table("review_queue_entry")
    op.drop_table("review_policy")
    op.execute("DROP FUNCTION IF EXISTS raise_review_policy_append_only_error()")
