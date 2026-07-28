"""T-W4-020 影子模式评分表（D 线 AI 量规基准验证）.

落地架构 v2 §4.5「AI 维度量规评分器」上线四步之第二步「影子运行」：
- AI 量规评分器对模拟作答打分，结果写入 ``shadow_score`` 表（不影响真实分数）；
- ``response_event`` 主表的 score 字段不被触碰（D1 三本账只增不改）；
- 与基准数据集（``tests/golden/shadow_dataset.json``）的人工量规结论对比，
  计算一致率（验收③：逐维偏差≤1 分视为一致，整体一致率≥70%）；
- 影子模式是上线四步「公开基准验证→影子运行→抽检伴随→灰度」中的第二步，
  T-W4-020 只到影子模式（不抽检、不灰度）.

为什么独立表而非复用 score_run（T-W4-003）：
- ``score_run`` 是「重判平行账」——对已落 response_event 的真实作答重判，
  必须绑 event_id（FK→response_event）；
- ``shadow_score`` 是「AI 量规评分器自验」——对模拟/基准作答打分，可能从未进
  response_event（基准数据集是合成的），无 event_id 可绑；
- 两者语义不同，强行复用会让 score_run.event_id 必须可空，破坏其重判语义.

为什么 append-only：影子评分是验证资产，发布后不应被修改（与 rubric_template /
blueprint 同理）；新版本应生成新 shadow_id，而非 UPDATE 旧行.

迁移可逆性（make migrate-check）：upgrade→downgrade→upgrade 全绿.

revision 编号：down_revision='0018'（D 线迁移链 0016→0018→0019）.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision: str = "0019"
down_revision: Union[str, None] = "0018"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# ────────────────────────────────────────────────────────────────────
# 触发器：append-only（复用 0003 的 raise_append_only_error）
# ────────────────────────────────────────────────────────────────────
_SHADOW_TRIGGER_SQL = """
CREATE TRIGGER trg_shadow_score_append_only
    BEFORE UPDATE OR DELETE ON shadow_score
    FOR EACH STATEMENT
    EXECUTE FUNCTION raise_append_only_error();
"""


def _create_shadow_score() -> None:
    """影子评分表.

    - shadow_id：行 id（应用层 ULID），text PK
    - dataset_id：基准数据集 id（如 'shadow-baseline-v1'；非基准场景为 'ad-hoc'）
    - case_id：数据集内 case id（如 'shadow-001'；非基准场景为 ULID）
    - rubric_id：量规模板 id（字符串引用，不强制 FK——影子运行时量规可能在内存中）
    - grade_band：学段 L/M/H（CK 约束域）
    - writing_type：写作类型 composition/picture_writing（CK 约束域）
    - response_text：被评分的作答文本（基准数据集原文；可空——仅记 digest 时）
    - response_text_digest：作答文本 sha256（dedup/replay，非空）
    - ai_score_payload：完整 AIRubricScore JSON（dimensions/total_score/
      total_max/overall_confidence/needs_human_review）
    - overall_confidence：整体置信度（冗余列便于查询，= ai_score_payload.overall_confidence）
    - needs_human_review：是否待人工复核（冗余列）
    - human_score_payload：人工量规结论 JSON（基准数据集附带；非基准场景 NULL）
    - consistency_status：一致状态（pending/consistent/inconsistent；CK 约束域）
      pending=未对比 / consistent=逐维偏差≤阈值 / inconsistent=超阈值
    - created_at：行写入时刻
    """
    op.create_table(
        "shadow_score",
        sa.Column("shadow_id", sa.Text(), primary_key=True),
        sa.Column("dataset_id", sa.Text(), nullable=False),
        sa.Column("case_id", sa.Text(), nullable=False),
        sa.Column("rubric_id", sa.Text(), nullable=False),
        sa.Column("grade_band", sa.Text(), nullable=False),
        sa.Column("writing_type", sa.Text(), nullable=False),
        sa.Column("response_text", sa.Text(), nullable=True),
        sa.Column("response_text_digest", sa.Text(), nullable=False),
        sa.Column("ai_score_payload", JSONB(), nullable=False),
        sa.Column("overall_confidence", sa.Numeric(4, 3), nullable=False),
        sa.Column("needs_human_review", sa.Boolean(), nullable=False),
        sa.Column("human_score_payload", JSONB(), nullable=True),
        sa.Column(
            "consistency_status",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "grade_band IN ('L', 'M', 'H')",
            name="ck_shadow_score_grade_band_domain",
        ),
        sa.CheckConstraint(
            "writing_type IN ('composition', 'picture_writing')",
            name="ck_shadow_score_writing_type_domain",
        ),
        sa.CheckConstraint(
            "consistency_status IN ('pending', 'consistent', 'inconsistent')",
            name="ck_shadow_score_consistency_status_domain",
        ),
        sa.CheckConstraint(
            "overall_confidence >= 0 AND overall_confidence <= 1",
            name="ck_shadow_score_overall_confidence_range",
        ),
    )


def _create_indexes() -> None:
    """常用查询索引：按数据集/学段/一致状态查影子评分."""
    op.create_index(
        "ix_shadow_score_dataset_id",
        "shadow_score",
        ["dataset_id"],
    )
    op.create_index(
        "ix_shadow_score_grade_band",
        "shadow_score",
        ["grade_band"],
    )
    op.create_index(
        "ix_shadow_score_consistency_status",
        "shadow_score",
        ["consistency_status"],
    )
    op.create_index(
        "ix_shadow_score_rubric_id",
        "shadow_score",
        ["rubric_id"],
    )


def _create_triggers() -> None:
    """append-only 物理强制：BEFORE UPDATE OR DELETE FOR EACH STATEMENT."""
    binding = op.get_bind()
    # raise_append_only_error() 由 0003 创建并经 0018 等复用；CREATE OR REPLACE 保证幂等
    binding.execute(
        sa.text(
            "CREATE OR REPLACE FUNCTION raise_append_only_error() "
            "RETURNS TRIGGER AS $$ "
            "BEGIN "
            "  RAISE EXCEPTION 'append-only table (D1): UPDATE/DELETE forbidden'; "
            "END; "
            "$$ LANGUAGE plpgsql;"
        )
    )
    binding.execute(sa.text(_SHADOW_TRIGGER_SQL))


def upgrade() -> None:
    """创建 shadow_score + 索引 + append-only 触发器."""
    _create_shadow_score()
    _create_indexes()
    _create_triggers()


def downgrade() -> None:
    """删除 shadow_score + 触发器（触发器函数为 0003 统一函数，不删）."""
    binding = op.get_bind()
    binding.execute(
        sa.text("DROP TRIGGER IF EXISTS trg_shadow_score_append_only ON shadow_score")
    )
    op.drop_index("ix_shadow_score_rubric_id", table_name="shadow_score")
    op.drop_index("ix_shadow_score_consistency_status", table_name="shadow_score")
    op.drop_index("ix_shadow_score_grade_band", table_name="shadow_score")
    op.drop_index("ix_shadow_score_dataset_id", table_name="shadow_score")
    op.drop_table("shadow_score")
