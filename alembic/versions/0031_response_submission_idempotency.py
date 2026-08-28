"""T-W5-018 作答提交幂等键防线（response_submission 登记账）.

board 验收：同一 (session, item, 作答指纹) 重复提交幂等成功返回原事件 id、
不重复落账；并发提交恰一条 response_event 入账、current_index 恰推进 1。
并发临界区由 core/session 的 per-session advisory xact lock + 会话行
FOR UPDATE 承担（与 core/estimator / core/compliance 同一惯例），本表复合
主键 pk_response_submission (session_id, item_version_id, answer_digest)
是最后一道防线：绕过应用锁的重复写入在 23505 处被明确拒绝。

为什么是独立登记账而不是给 response_event 补部分唯一索引：response_event
是按 created_at RANGE 分区的事件账（0003，D1 append-only），PostgreSQL 对
分区表的唯一索引强制包含全部分区键——created_at 入键即每事件各占一行，
幂等判定失效；「不含分区键的部分唯一索引」物理上不可建。幂等键落在非
分区的登记账上：answer_digest 为作答提交指纹（core/gate/validators.
ContentDigest 规范化摘要口径，键序/空白不敏感，CHECK 物理锚定其形状）；
event_id + event_created_at 复合回指 response_event 主键（0003 实现注记：
分区表事件以 (event_id, created_at) 复合键引用）。

全加性：不改 response_event 一列一索引（冻结契约 §1 十三列零漂移）。
可逆性（make migrate-check / migrate-go-check）：upgrade→downgrade→upgrade 全绿。

链序说明：down_revision 指 0030（T-W5-004 会话题序不可变，同一波次先行卡）；
单 head 线性链，golang-migrate 主源按版本号排序，不受合入顺序影响。
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0031"
down_revision = "0030"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """作答提交幂等登记账：幂等键三元组 + 原事件复合回指 + 指纹形状锚定."""
    op.create_table(
        "response_submission",
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("item_version_id", sa.Text(), nullable=False),
        sa.Column("answer_digest", sa.Text(), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("event_created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint(
            "session_id",
            "item_version_id",
            "answer_digest",
            name="pk_response_submission",
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["practice_session.session_id"],
            name="fk_response_submission_session",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["event_id", "event_created_at"],
            ["response_event.event_id", "response_event.created_at"],
            name="fk_response_submission_event",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            sa.text("answer_digest ~ '^sha256:[0-9a-f]{64}$'"),
            name="ck_response_submission_digest_shape",
        ),
    )


def downgrade() -> None:
    """逆序还原：幂等登记账整表移除（全加性迁移的可逆面）."""
    op.drop_table("response_submission")
