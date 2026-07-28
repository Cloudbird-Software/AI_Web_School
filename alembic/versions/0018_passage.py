"""T-W4-012 C 线语篇 passage 表.

架构 v2 §4.1 C 线素材工坊核心产物表：体裁×知识点×难度×学段×学科 + 正文 +
许可 + 难度指标。独立于 item/material/corpus，承载语篇特有字段。

宪法 D2 门强制：published 行必须持合法 gate_certificate_id——本迁移用 CHECK
ck_passage_published_requires_gate 兜底（与 item_version 的门约束同构），
绕过写入服务直写 published 行必被 DB 拒绝（014 验收 #4 断言此行为）。

列与 src/core/models/passage.py::Passage 逐字对齐。
迁移可逆性（make migrate-check）：upgrade→downgrade→upgrade 全绿。

down_revision='0016'：本分支从 task/T-W4-ai-bus（head=0016）拉出；
0017 为另一会话 T-W4-048 的未提交产物（score_run），若 0017 先合并，
本迁移需 rebase down_revision='0017'。
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0018"
down_revision: Union[str, None] = "0016"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _create_passage() -> None:
    """创建 passage 表.

    字段对应 T-W4-012 验收 #1：
    - passage_id / content_hash / body / genre / kp_refs / difficulty_metrics /
      license_id / grade_band / subject / status / gate_certificate_id /
      published_at / created_at。
    """
    op.create_table(
        "passage",
        sa.Column("passage_id", sa.Text(), primary_key=True),
        sa.Column("content_hash", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("genre", sa.Text(), nullable=False),
        sa.Column("kp_refs", sa.dialects.postgresql.JSONB(), nullable=False),
        sa.Column(
            "difficulty_metrics",
            sa.dialects.postgresql.JSONB(),
            nullable=False,
        ),
        sa.Column(
            "license_id",
            sa.Text(),
            sa.ForeignKey("material_license.license_id", name="fk_passage_license"),
            nullable=True,
        ),
        sa.Column("grade_band", sa.Text(), nullable=False),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column(
            "status", sa.Text(), nullable=False, server_default="draft"
        ),
        sa.Column("gate_certificate_id", sa.Text(), nullable=True),
        sa.Column(
            "published_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        # D2 门强制：published 必须持 gate_certificate_id
        sa.CheckConstraint(
            "status <> 'published' OR gate_certificate_id IS NOT NULL",
            name="ck_passage_published_requires_gate",
        ),
        sa.CheckConstraint(
            "genre IN ('narrative','expository','argumentative','poetry',"
            "'fable','fairy_tale','dialogue','news_report','letter','diary')",
            name="ck_passage_genre_domain",
        ),
        sa.CheckConstraint(
            "grade_band IN ('L','M','H')",
            name="ck_passage_grade_band_domain",
        ),
        sa.CheckConstraint(
            "status IN ('draft','quarantined','published','retired')",
            name="ck_passage_status_domain",
        ),
    )


def _create_indexes() -> None:
    """常用查询索引：内容寻址查重 + 学科×学段筛选."""
    op.create_index(
        "ix_passage_content_hash", "passage", ["content_hash"]
    )
    op.create_index(
        "ix_passage_subject_grade_band",
        "passage",
        ["subject", "grade_band"],
    )


def upgrade() -> None:
    """创建 passage 表 + 索引."""
    _create_passage()
    _create_indexes()


def downgrade() -> None:
    """删除 passage 表 + 索引."""
    op.drop_index("ix_passage_subject_grade_band", table_name="passage")
    op.drop_index("ix_passage_content_hash", table_name="passage")
    op.drop_table("passage")
