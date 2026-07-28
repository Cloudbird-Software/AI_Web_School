"""T-W4-017 命题蓝图库 + 量规模板表（D 线命题工坊）.

落地架构 v2 §4.1 D 线「命题蓝图库」+ §4.5 量规评分的数据持久化：
- rubric_template：量规模板（量规即数据，payload JSONB 存完整 RubricTemplate）；
  被 AIRubricScorer（T-W4-019）经 to_scorer_params() 消费。
- blueprint：命题蓝图（payload JSONB 存完整 Blueprint，FK→rubric_template）；
  被 run_d_pipeline（T-W4-021）消费：选蓝图→实例化→量规嵌入→签发入库。

为什么两表 append-only：量规与蓝图是版本化参考数据，发布后不应被修改——
新版本应生成新 rubric_id/blueprint_id（内容寻址），而非 UPDATE 旧行。这与
平台「只增不改」哲学一致（虽非 D1 三本账，但作为可追溯的命题资产施 append-only
物理强制，防误改导致已签发题目元数据漂移）。

迁移可逆性（make migrate-check）：upgrade→downgrade→upgrade 全绿。

revision 编号说明：本 D 线分支 down_revision='0016'（跳过 '0017'）。
0017 由 T-W4-003（score_run）在另一并行会话占用且未提交；为避免 revision id
冲突，本 D 线迁移链 0016→0018→0019，合并时由 alembic merge 收口分叉。
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision: str = "0018"
down_revision: Union[str, None] = "0016"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# ────────────────────────────────────────────────────────────────────
# 触发器：两表只增不改（复用 0003 的 raise_append_only_error）
# ────────────────────────────────────────────────────────────────────
_RUBRIC_TRIGGER_SQL = """
CREATE TRIGGER trg_rubric_template_append_only
    BEFORE UPDATE OR DELETE ON rubric_template
    FOR EACH STATEMENT
    EXECUTE FUNCTION raise_append_only_error();
"""

_BLUEPRINT_TRIGGER_SQL = """
CREATE TRIGGER trg_blueprint_append_only
    BEFORE UPDATE OR DELETE ON blueprint
    FOR EACH STATEMENT
    EXECUTE FUNCTION raise_append_only_error();
"""


def _create_rubric_template() -> None:
    """量规模板表.

    - rubric_id：行 id（应用层内容寻址 sha256 of payload），text PK
    - name：量规模板名（教研展示 + 查询便利）
    - grade_band：学段覆盖标记 L/M/H（CK 约束域）
    - version：量规版本串（随题版本化）
    - payload：完整 RubricTemplate JSON（含 dimensions/levels/分值，评分器消费源）
    - total_max_score：分值合计（冗余列，便于查询/校验，= payload.total_max_score）
    - created_at：行写入时刻
    """
    op.create_table(
        "rubric_template",
        sa.Column("rubric_id", sa.Text(), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("grade_band", sa.Text(), nullable=False),
        sa.Column("version", sa.Text(), nullable=False),
        sa.Column("payload", JSONB(), nullable=False),
        sa.Column("total_max_score", sa.Numeric(6, 2), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "grade_band IN ('L', 'M', 'H')",
            name="ck_rubric_template_grade_band_domain",
        ),
    )


def _create_blueprint() -> None:
    """命题蓝图表.

    - blueprint_id：行 id，text PK
    - writing_type：写作类型 composition/picture_writing（CK 约束域）
    - pack_id：学科包 id（字符串引用，核心域不 import 学科包）
    - template_version_id：A 线母题模板版本引用
    - rubric_template_id：量规模板引用（FK→rubric_template，ondelete RESTRICT
      防止量规被删时蓝图悬空）
    - payload：完整 Blueprint JSON（含 grade_band_specs/topic_pool 等）
    - version：蓝图版本串
    - created_at：行写入时刻
    """
    op.create_table(
        "blueprint",
        sa.Column("blueprint_id", sa.Text(), primary_key=True),
        sa.Column("writing_type", sa.Text(), nullable=False),
        sa.Column("pack_id", sa.Text(), nullable=False),
        sa.Column("template_version_id", sa.Text(), nullable=False),
        sa.Column("rubric_template_id", sa.Text(), nullable=False),
        sa.Column("payload", JSONB(), nullable=False),
        sa.Column("version", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "writing_type IN ('composition', 'picture_writing')",
            name="ck_blueprint_writing_type_domain",
        ),
        sa.ForeignKeyConstraint(
            ["rubric_template_id"],
            ["rubric_template.rubric_id"],
            name="fk_blueprint_rubric_template",
            ondelete="RESTRICT",
        ),
    )


def _create_indexes() -> None:
    """常用查询索引：按学段/学科/写作类型查量规与蓝图."""
    op.create_index(
        "ix_rubric_template_grade_band",
        "rubric_template",
        ["grade_band"],
    )
    op.create_index(
        "ix_blueprint_writing_type",
        "blueprint",
        ["writing_type"],
    )
    op.create_index(
        "ix_blueprint_pack_id",
        "blueprint",
        ["pack_id"],
    )
    op.create_index(
        "ix_blueprint_rubric_template_id",
        "blueprint",
        ["rubric_template_id"],
    )


def _create_triggers() -> None:
    """两表 append-only 物理强制：BEFORE UPDATE OR DELETE FOR EACH STATEMENT."""
    binding = op.get_bind()
    # raise_append_only_error() 由 0003 创建，0017 等多表复用；
    # CREATE OR REPLACE 保证幂等（与既有迁移一致）。
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
    binding.execute(sa.text(_RUBRIC_TRIGGER_SQL))
    binding.execute(sa.text(_BLUEPRINT_TRIGGER_SQL))


def upgrade() -> None:
    """创建 rubric_template + blueprint + 索引 + append-only 触发器."""
    _create_rubric_template()
    _create_blueprint()
    _create_indexes()
    _create_triggers()


def downgrade() -> None:
    """删除两表 + 触发器（触发器函数为 0003 统一函数，不删）."""
    binding = op.get_bind()
    binding.execute(
        sa.text("DROP TRIGGER IF EXISTS trg_blueprint_append_only ON blueprint")
    )
    binding.execute(
        sa.text("DROP TRIGGER IF EXISTS trg_rubric_template_append_only ON rubric_template")
    )
    op.drop_index("ix_blueprint_rubric_template_id", table_name="blueprint")
    op.drop_index("ix_blueprint_pack_id", table_name="blueprint")
    op.drop_index("ix_blueprint_writing_type", table_name="blueprint")
    op.drop_index("ix_rubric_template_grade_band", table_name="rubric_template")
    op.drop_table("blueprint")
    op.drop_table("rubric_template")
