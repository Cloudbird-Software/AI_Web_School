"""T-W2-037 paper / paper_item 追溯表.

按 ADR/母题与知识体系-工程架构设计方案v2.md#4.6 与 #附录A 落地卷追溯：
- paper：一份卷（卷码+卷规格 ID+知识点快照+确定性种子）
- paper_item：卷内题目（placement_token + 题短码 + item_version_id）

为什么不用 item_version 直接渲染：一份卷可能多次重渲染（不同时间/不同出口），
paper 行固化「这次组卷选了哪些 item_version、按什么顺序」，让 PDF 可复现追溯。

卷码 paper_code = ULID + Luhn 校验位（应用层 src/core/render/trace_codes.py 生成）：
- ULID 全局唯一（按时间排序）
- 校验位防手抄错（错一位能被检出）
- 卷码作为人类可读的卷标识，打印在卷面

QR payload 仅含 paper_spec_id + 校验位（不含实例明文）：
- paper_spec_id 是组卷规格的稳定 ID（同 spec 多次组卷可重用）
- 不含 item_version_id 等实例信息——QR 是公开打印的，不能泄露题目内容
- 扫码后端用 spec_id + 检索 paper 表的 paper_code 字段定位卷

迁移可逆性（make migrate-check）：upgrade→downgrade→upgrade 全绿。
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0009"
down_revision: Union[str, None] = "0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# ────────────────────────────────────────────────────────────────────
# 触发器函数：paper 表只增（D1 三本账之外的辅助账，沿用同模式）
# ────────────────────────────────────────────────────────────────────
# paper 表是组卷产物账本，行只增不改——重新组卷生成新行而非 UPDATE。
# paper_item 同样只增（卷一旦生成，题目集合冻结，需改题就生成新卷）。
_ENSURE_TRIGGER_FUNCTION_SQL = """
CREATE OR REPLACE FUNCTION raise_paper_append_only_error() RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'paper table is append-only: UPDATE/DELETE forbidden';
END;
$$ LANGUAGE plpgsql;
"""

_TRIGGER_PAPER_SQL = """
CREATE TRIGGER trg_paper_append_only
    BEFORE UPDATE OR DELETE ON paper
    FOR EACH STATEMENT
    EXECUTE FUNCTION raise_paper_append_only_error();
"""

_TRIGGER_PAPER_ITEM_SQL = """
CREATE TRIGGER trg_paper_item_append_only
    BEFORE UPDATE OR DELETE ON paper_item
    FOR EACH STATEMENT
    EXECUTE FUNCTION raise_paper_append_only_error();
"""


# ────────────────────────────────────────────────────────────────────
# 表创建
# ────────────────────────────────────────────────────────────────────

def _create_paper() -> None:
    """卷主表.

    - paper_id：内部 id（应用层 ULID 生成）；text 而非 uuid，与其他表风格一致
    - paper_code：人类可读卷码 = ULID + Luhn 校验位；UNIQUE
    - paper_spec_id：卷规格 id（QR 含此 id+校验位，不含实例明文）
    - paper_title / gradeband / subject_pack_id：卷面元信息
    - weekly_batch_id：周更批次 id（追溯用，nullable=非周更产出的卷）
    - kp_snapshot_ref：知识点范围快照引用（确定性组卷的输入快照）
    - seed：确定性种子（同快照+同种子→同题序）
    - rendered_snapshot_path：PDF 落盘路径（可复现验证）
    """
    op.create_table(
        "paper",
        sa.Column("paper_id", sa.Text(), primary_key=True),
        sa.Column("paper_code", sa.Text(), nullable=False),
        sa.Column("paper_spec_id", sa.Text(), nullable=False),
        sa.Column("paper_title", sa.Text(), nullable=False),
        sa.Column("gradeband", sa.Text(), nullable=False),
        sa.Column("subject_pack_id", sa.Text(), nullable=False),
        sa.Column("weekly_batch_id", sa.Text(), nullable=True),
        sa.Column("kp_snapshot_ref", sa.Text(), nullable=False),
        sa.Column("seed", sa.BigInteger(), nullable=False),
        sa.Column("rendered_snapshot_path", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.UniqueConstraint("paper_code", name="uq_paper_code"),
        sa.UniqueConstraint("paper_spec_id", name="uq_paper_spec_id"),
        sa.CheckConstraint(
            "gradeband IN ('L', 'M', 'H')",
            name="ck_paper_gradeband_domain",
        ),
        sa.CheckConstraint(
            "subject_pack_id IN ('subject-math', 'subject-chinese', 'subject-english')",
            name="ck_paper_subject_pack_domain",
        ),
    )


def _create_paper_item() -> None:
    """卷内题目表.

    - paper_item_id：内部 id（应用层 ULID）
    - paper_id：FK→paper
    - item_version_id：FK→item_version（卷选定题目版本）
    - placement_token：题在卷中的位置标识（如 'q1' / 'q2.sub1'）
    - item_number：题号（卷内顺序，1-based）
    - item_short_code：题短码 = base32 纠错短码（UNIQUE，打印在卷面供学生/家长扫码查源）
    """
    op.create_table(
        "paper_item",
        sa.Column("paper_item_id", sa.Text(), primary_key=True),
        sa.Column("paper_id", sa.Text(), nullable=False),
        sa.Column("item_version_id", sa.Text(), nullable=False),
        sa.Column("placement_token", sa.Text(), nullable=False),
        sa.Column("item_number", sa.Integer(), nullable=False),
        sa.Column("item_short_code", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["paper_id"],
            ["paper.paper_id"],
            name="fk_paper_item_paper",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["item_version_id"],
            ["item_version.item_version_id"],
            name="fk_paper_item_item_version",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("item_short_code", name="uq_paper_item_short_code"),
        sa.UniqueConstraint(
            "paper_id",
            "placement_token",
            name="uq_paper_item_paper_placement",
        ),
        sa.CheckConstraint(
            "item_number > 0",
            name="ck_paper_item_number_positive",
        ),
    )


def _create_indexes() -> None:
    """常用查询索引：按卷查题、按短码反查卷."""
    op.create_index(
        "ix_paper_item_paper_id",
        "paper_item",
        ["paper_id"],
    )
    op.create_index(
        "ix_paper_paper_spec_id",
        "paper",
        ["paper_spec_id"],
    )
    op.create_index(
        "ix_paper_weekly_batch_id",
        "paper",
        ["weekly_batch_id"],
    )


# ────────────────────────────────────────────────────────────────────
# Alembic 入口
# ────────────────────────────────────────────────────────────────────

def upgrade() -> None:
    """创建 paper/paper_item + 触发器 + 索引."""
    # 触发器函数（幂等：CREATE OR REPLACE）
    op.execute(_ENSURE_TRIGGER_FUNCTION_SQL)
    _create_paper()
    _create_paper_item()
    _create_indexes()
    op.execute(_TRIGGER_PAPER_SQL)
    op.execute(_TRIGGER_PAPER_ITEM_SQL)


def downgrade() -> None:
    """删除 paper/paper_item + 触发器 + 函数."""
    op.execute("DROP TRIGGER IF EXISTS trg_paper_item_append_only ON paper_item")
    op.execute("DROP TRIGGER IF EXISTS trg_paper_append_only ON paper")
    op.drop_table("paper_item")
    op.drop_table("paper")
    # 触发器函数可能在其他表还在用，保留（CREATE OR REPLACE 幂等，下次 upgrade 重建）
    # 但为干净起见，本迁移产生的函数也清理
    op.execute("DROP FUNCTION IF EXISTS raise_paper_append_only_error()")
