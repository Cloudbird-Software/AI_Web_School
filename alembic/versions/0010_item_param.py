"""W3 S8 数据域起步：item_param 参数标定表.

按架构 v2 §4.7 与宪法 D5/D6 落地题目参数账：
- item_param(item_version_id, purpose_scope, source, params jsonb,
  sample_size, method_version, as_of)
- source ∈ prior_rule / prior_expert / measured_*（先验与实测分开存储）
- purpose_scope ∈ practice / diagnosis / measurement（D5 分场景独立估计，
  禁止跨场景混估——同题不同场景各占一行，UNIQUE 约束含 purpose_scope）
- D6 估计器可替换：每次估计运行产出新行（method_version + as_of 区分），
  历史行永不覆盖——本表只增不改，由 0005 统一的 raise_append_only_error()
  触发器物理强制（与 response_event/gate 三表同一函数）。

为什么 PK 用 param_id（应用层 ULID）而非复合自然键：
估计行身份=一次运行产出，(item_version_id, purpose_scope, source,
method_version, as_of) 联合唯一约束承载幂等（同运行重复写入报冲突），
param_id 供报告/ mastery 外链引用单行。

迁移可逆性（make migrate-check）：upgrade→downgrade→upgrade 全绿。
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision: str = "0010"
down_revision: Union[str, None] = "0009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# ────────────────────────────────────────────────────────────────────
# 触发器：item_param 只增不改（复用 0005 统一函数 raise_append_only_error）
# ────────────────────────────────────────────────────────────────────
_TRIGGER_SQL = """
CREATE TRIGGER trg_item_param_append_only
    BEFORE UPDATE OR DELETE ON item_param
    FOR EACH STATEMENT
    EXECUTE FUNCTION raise_append_only_error();
"""


def _create_item_param() -> None:
    """题目参数标定表（架构 v2 §4.7）.

    - param_id：行 id（应用层 ULID）
    - item_version_id：FK→item_version（参数挂在不可变版本上，D3）
    - purpose_scope：场景域 practice/diagnosis/measurement（D5 禁混估）
    - source：参数来源 prior_rule/prior_expert/measured_*（先验/实测分离）
    - params：参数体 jsonb（CTT：{difficulty, discrimination}；IRT 预留
      {difficulty, discrimination, guessing…}；开放题按评分维度分别估计，
      维度作 params 内层键）
    - sample_size：估计样本量（先验行为 0）
    - method_version：估计方法版本（如 'ctt-v1'，D6 可替换）
    - as_of：估计截止时刻（输入数据快照右端；同方法多次运行靠它区分）
    """
    op.create_table(
        "item_param",
        sa.Column("param_id", sa.Text(), primary_key=True),
        sa.Column(
            "item_version_id",
            sa.Text(),
            sa.ForeignKey(
                "item_version.item_version_id",
                name="fk_item_param_item_version",
                ondelete="RESTRICT",
            ),
            nullable=False,
        ),
        sa.Column("purpose_scope", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("params", JSONB(), nullable=False),
        sa.Column("sample_size", sa.Integer(), nullable=False),
        sa.Column("method_version", sa.Text(), nullable=False),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint(
            "item_version_id",
            "purpose_scope",
            "source",
            "method_version",
            "as_of",
            name="uq_item_param_identity",
        ),
        sa.CheckConstraint(
            "purpose_scope IN ('practice', 'diagnosis', 'measurement')",
            name="ck_item_param_purpose_scope_domain",
        ),
        sa.CheckConstraint(
            "source ~ '^(prior_rule|prior_expert|measured_.+)$'",
            name="ck_item_param_source_domain",
        ),
        sa.CheckConstraint(
            "sample_size >= 0",
            name="ck_item_param_sample_size_nonneg",
        ),
    )


def _create_indexes() -> None:
    """常用查询索引：按题查参数、按场景筛."""
    op.create_index(
        "ix_item_param_item_version_id",
        "item_param",
        ["item_version_id"],
    )
    op.create_index(
        "ix_item_param_purpose_scope",
        "item_param",
        ["purpose_scope"],
    )


def upgrade() -> None:
    """创建 item_param + 索引 + append-only 触发器."""
    _create_item_param()
    _create_indexes()
    op.execute(_TRIGGER_SQL)


def downgrade() -> None:
    """删除 item_param + 触发器（触发器函数为 0005 统一函数，不删）."""
    op.execute(
        "DROP TRIGGER IF EXISTS trg_item_param_append_only ON item_param"
    )
    op.drop_table("item_param")
