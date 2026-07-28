"""T-W4-032 家长授权记录表（版本化 / 范围 / 时间 / 撤回，append-only）.

按架构 v2 §4.8 与宪法 D7 落地家长授权前置：
- 版本化：每次授权/撤回写新行（version 单调递增），旧版本保留——
  「旧版本标记过期时间戳」由新事件的 created_at 隐式承载（check_consent
  查最新事件判定有效性，无需 UPDATE 旧行）。
- 范围：scope JSONB（数据用途 purpose / 学科 subject / 时段 time_period）。
- 时间：valid_from / valid_until（grant 有效窗口）；revoke 事件 valid_until NULL。
- 撤回：revoke_consent 写新行 event_type='revoke'，原 grant 立即失效
  （check_consent 见 revoke 即返回 False），历史保留。

为什么 append-only（BEFORE UPDATE OR DELETE 触发器）：
- 审计可追溯：授权变更历史永不丢失。
- 与三本账同模式：虽然 parental_consent 不在 D1 三本账（内容/作答/校验）之内，
  但授权记录的审计价值等同于账本——任何变更都应是新事件而非覆盖。

为什么 scope 用 JSONB 而非分离列：
- 范围维度可能扩展（未来加地域/年级等），JSONB 灵活；
- check_consent 按 scope->>'purpose' 索引匹配（purpose 是授权主键语义）。

迁移可逆性（make migrate-check）：upgrade→downgrade→upgrade 全绿。
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID

# revision identifiers, used by Alembic.
revision: str = "0015"
down_revision: Union[str, None] = "0014"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# ────────────────────────────────────────────────────────────────────
# 触发器函数：parental_consent append-only（独立函数，避免与 0005 的
# raise_append_only_error 互相覆盖——本表语义不同，独立错误消息更清晰）
# ────────────────────────────────────────────────────────────────────
_ENSURE_TRIGGER_FUNCTION_SQL = """
CREATE OR REPLACE FUNCTION raise_parental_consent_append_only_error()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'parental_consent is append-only (T-W4-032): UPDATE/DELETE forbidden';
END;
$$ LANGUAGE plpgsql;
"""

_TRIGGER_SQL = """
CREATE TRIGGER trg_parental_consent_append_only
    BEFORE UPDATE OR DELETE ON parental_consent
    FOR EACH STATEMENT
    EXECUTE FUNCTION raise_parental_consent_append_only_error();
"""


# ────────────────────────────────────────────────────────────────────
# 表创建
# ────────────────────────────────────────────────────────────────────

def _create_parental_consent() -> None:
    """家长授权事件表（append-only，grant/revoke 事件流）.

    - consent_id：事件 id（应用层 uuid）
    - student_alias_id：匿名学生 id（与主库 public.practice_session 同源）
    - event_type：grant（授权）/ revoke（撤回）
    - scope：范围 JSONB，必含 purpose（如 "practice" / "diagnosis"）；
      可含 subject / time_period 等扩展维度
    - valid_from / valid_until：grant 的有效窗口；revoke 事件两者为 NULL
    - version：同一 (student_alias_id, scope->>purpose) 下的单调递增版本号
    - created_at：事件时间戳（旧版本「过期时间戳」由后续事件的 created_at 隐式承载）
    """
    op.create_table(
        "parental_consent",
        sa.Column("consent_id", PG_UUID(as_uuid=True), primary_key=True),
        sa.Column("student_alias_id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("scope", JSONB(), nullable=False),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=True),
        sa.Column("valid_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "event_type IN ('grant', 'revoke')",
            name="ck_parental_consent_event_type_domain",
        ),
        sa.CheckConstraint(
            "version >= 1",
            name="ck_parental_consent_version_positive",
        ),
        sa.CheckConstraint(
            # grant 事件须有 valid_from；revoke 事件 valid_from/valid_until 为 NULL
            "(event_type = 'grant' AND valid_from IS NOT NULL) "
            "OR (event_type = 'revoke' AND valid_from IS NULL "
            "    AND valid_until IS NULL)",
            name="ck_parental_consent_event_type_time_consistency",
        ),
        sa.CheckConstraint(
            # scope 必须是对象且含 purpose 键
            "jsonb_typeof(scope) = 'object' "
            "AND scope ? 'purpose'",
            name="ck_parental_consent_scope_has_purpose",
        ),
    )


def _create_indexes() -> None:
    """查询热路径：按学生 + purpose + 版本倒序取最新事件."""
    # 按 student_alias_id 查全部授权事件
    op.create_index(
        "ix_parental_consent_student",
        "parental_consent",
        ["student_alias_id", "version"],
    )
    # 按 (student, purpose) 查最新版本——purpose 在 JSONB 内，用表达式索引
    op.create_index(
        "ix_parental_consent_student_purpose_version",
        "parental_consent",
        ["student_alias_id", sa.text("(scope ->> 'purpose')"), "version"],
    )


# ────────────────────────────────────────────────────────────────────
# upgrade / downgrade
# ────────────────────────────────────────────────────────────────────

def upgrade() -> None:
    """建 parental_consent 表 + 触发器 + 索引."""
    op.execute(_ENSURE_TRIGGER_FUNCTION_SQL)
    _create_parental_consent()
    _create_indexes()
    op.execute(_TRIGGER_SQL)


def downgrade() -> None:
    """删 parental_consent 表 + 触发器 + 函数."""
    op.execute(
        "DROP TRIGGER IF EXISTS trg_parental_consent_append_only ON parental_consent"
    )
    op.drop_table("parental_consent")
    op.execute(
        "DROP FUNCTION IF EXISTS raise_parental_consent_append_only_error()"
    )
