"""T-W3-assembly 曝光账本双轨表（paper_exposure / student_exposure）.

按架构 v2 §4.4「曝光互斥」落地曝光账本：
- 静态轨 paper_exposure：按 渠道×学科×版本×年级×周队列 记录题目曝光，
  服务周更静态批处理（同一周队列内同渠道不重复发题）。
- 在线轨 student_exposure：按 学生匿名 id 记录题目曝光，
  服务在线组卷的「跨期不重复」（同一学生不再见到同一题）。

为什么是两张表而非一张：双轨查询模式与基数完全不同——
静态轨按周队列批量查重（低基数、批量写），在线轨按学生查重（高基数、逐条写）；
分表让索引与分区演进互不影响。

宪法 D1 风格：两表均只增不改（本迁移触发器物理强制）——
曝光是历史事实，撤销曝光不产生 DELETE，而是新组卷不再引用。

宪法 D7：student_exposure 只存 student_alias_id（匿名 id），不存任何 PII；
PII 只在保险库 schema（W3 非目标，此处不落地）。

R-Z-02 曝光互斥的两条规则落点：
- 同母题不同卷：template_version_id 列支撑「同母题实例不出现在同卷/同期」判断；
- 跨期不重复：student_exposure 的 UNIQUE(student_alias_id, item_version_id)
  在 DB 层兜底学生级跨期不重复；paper_exposure 的
  UNIQUE(channel, subject_pack_id, week_label, item_version_id) 兜底周队列级。

事务性曝光预留（§4.4）：组卷服务在同一事务内写 paper/paper_item 与曝光行，
失败整体回滚，不产生「卷未发出但题已标记曝光」的幽灵占用。

迁移可逆性（make migrate-check）：upgrade→downgrade→upgrade 全绿。
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0010"
down_revision: Union[str, None] = "0009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# ────────────────────────────────────────────────────────────────────
# 触发器：两表只增（沿用 0005 统一的 raise_append_only_error 函数）
# ────────────────────────────────────────────────────────────────────
# 0005 已创建 raise_append_only_error()（CREATE OR REPLACE 幂等）；
# 本迁移不重复定义函数，仅挂触发器。
_TRIGGER_PAPER_EXPOSURE_SQL = """
CREATE TRIGGER trg_paper_exposure_append_only
    BEFORE UPDATE OR DELETE ON paper_exposure
    FOR EACH STATEMENT
    EXECUTE FUNCTION raise_append_only_error();
"""

_TRIGGER_STUDENT_EXPOSURE_SQL = """
CREATE TRIGGER trg_student_exposure_append_only
    BEFORE UPDATE OR DELETE ON student_exposure
    FOR EACH STATEMENT
    EXECUTE FUNCTION raise_append_only_error();
"""


def _create_paper_exposure() -> None:
    """静态轨：周队列曝光表.

    - exposure_id：内部 id（应用层 ULID）
    - channel：发放渠道（如 'weekly_pdf' / 'online' / 'targeted_mini'）
    - subject_pack_id：学科包 id
    - textbook_version：教材版本（nullable——语文统编无版本维度）
    - gradeband：学段（'L'/'M'/'H'）
    - week_label：周队列标签（如 '2026-W30'），静态互斥的时间桶
    - item_version_id：被曝光的题目版本（FK→item_version）
    - template_version_id：母题版本（nullable；C/D 级题无母题）
    - paper_id：产出卷（nullable——曝光可先于卷落库预留）
    """
    op.create_table(
        "paper_exposure",
        sa.Column("exposure_id", sa.Text(), primary_key=True),
        sa.Column("channel", sa.Text(), nullable=False),
        sa.Column("subject_pack_id", sa.Text(), nullable=False),
        sa.Column("textbook_version", sa.Text(), nullable=True),
        sa.Column("gradeband", sa.Text(), nullable=False),
        sa.Column("week_label", sa.Text(), nullable=False),
        sa.Column("item_version_id", sa.Text(), nullable=False),
        sa.Column("template_version_id", sa.Text(), nullable=True),
        sa.Column("paper_id", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["item_version_id"],
            ["item_version.item_version_id"],
            name="fk_paper_exposure_item_version",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["paper_id"],
            ["paper.paper_id"],
            name="fk_paper_exposure_paper",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "channel",
            "subject_pack_id",
            "week_label",
            "item_version_id",
            name="uq_paper_exposure_queue_item",
        ),
        sa.CheckConstraint(
            "gradeband IN ('L', 'M', 'H')",
            name="ck_paper_exposure_gradeband_domain",
        ),
    )


def _create_student_exposure() -> None:
    """在线轨：学生级曝光表.

    - exposure_id：内部 id（应用层 ULID）
    - student_alias_id：学生匿名 id（D7：只存 alias，PII 在保险库）
    - item_version_id：被曝光的题目版本（FK→item_version）
    - template_version_id：母题版本（nullable）
    - paper_id：来源卷（nullable——在线组卷可不入卷直接发题）
    - session_id：作答会话 id（nullable，W3 S3 会话服务接入后回填）
    - purpose：场景（practice/diagnosis/measurement；D5 分场景不混估）
    """
    op.create_table(
        "student_exposure",
        sa.Column("exposure_id", sa.Text(), primary_key=True),
        sa.Column("student_alias_id", sa.Text(), nullable=False),
        sa.Column("item_version_id", sa.Text(), nullable=False),
        sa.Column("template_version_id", sa.Text(), nullable=True),
        sa.Column("paper_id", sa.Text(), nullable=True),
        sa.Column("session_id", sa.Text(), nullable=True),
        sa.Column("purpose", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["item_version_id"],
            ["item_version.item_version_id"],
            name="fk_student_exposure_item_version",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["paper_id"],
            ["paper.paper_id"],
            name="fk_student_exposure_paper",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "student_alias_id",
            "item_version_id",
            name="uq_student_exposure_student_item",
        ),
        sa.CheckConstraint(
            "purpose IN ('practice', 'diagnosis', 'measurement')",
            name="ck_student_exposure_purpose_domain",
        ),
    )


def _create_indexes() -> None:
    """查重热路径索引：周队列批量查 / 学生查 / 同母题互斥查."""
    op.create_index(
        "ix_paper_exposure_queue",
        "paper_exposure",
        ["channel", "subject_pack_id", "week_label"],
    )
    op.create_index(
        "ix_paper_exposure_template",
        "paper_exposure",
        ["template_version_id"],
    )
    op.create_index(
        "ix_student_exposure_student",
        "student_exposure",
        ["student_alias_id"],
    )
    op.create_index(
        "ix_student_exposure_template",
        "student_exposure",
        ["template_version_id"],
    )


def upgrade() -> None:
    """创建曝光双轨表 + 触发器 + 索引."""
    _create_paper_exposure()
    _create_student_exposure()
    _create_indexes()
    op.execute(_TRIGGER_PAPER_EXPOSURE_SQL)
    op.execute(_TRIGGER_STUDENT_EXPOSURE_SQL)


def downgrade() -> None:
    """删除曝光双轨表 + 触发器（函数 raise_append_only_error 由 0005 管理，不动）."""
    op.execute(
        "DROP TRIGGER IF EXISTS trg_student_exposure_append_only ON student_exposure"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_paper_exposure_append_only ON paper_exposure"
    )
    op.drop_table("student_exposure")
    op.drop_table("paper_exposure")
