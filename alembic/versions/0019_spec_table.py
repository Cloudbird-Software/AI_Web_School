"""T-W4-027 双向细目表 spec_table 迁移.

按架构 v2 §4.4「测量(预留)」行落地双向细目表 schema：
- 内容（知识点）× 认知层级 × 题量 × 难度单元格计数约束
- 单元格 = {content_code, cognitive_level, target_count, difficulty_min, difficulty_max}
- schema 校验在 Pydantic 层（SpecTable 模型），DB 层只做容器与基本域校验

D1 只增不改：与 item_version / response_event / gate_certificate /
item_lifecycle_transition 同手法——细目表是版本账，改表 = 新行（新版本），
append-only 触发器物理强制禁 UPDATE/DELETE。

迁移可逆性（make migrate-check）：upgrade→downgrade→upgrade 全绿。
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
# append-only 触发器（D1 物理强制，与 item_lifecycle_transition 同手法）
# ────────────────────────────────────────────────────────────────────

_TRIGGER_FUNCTION_SQL = """
CREATE OR REPLACE FUNCTION raise_spec_table_append_only_error() RETURNS TRIGGER AS $$
BEGIN
    -- D1 细目表版本账只增不改：改表 = INSERT 新版本行，禁止 UPDATE/DELETE。
    RAISE EXCEPTION 'spec_table is append-only (D1): UPDATE/DELETE forbidden';
END;
$$ LANGUAGE plpgsql;
"""

_TRIGGER_SQL = """
CREATE TRIGGER trg_spec_table_append_only
    BEFORE UPDATE OR DELETE ON spec_table
    FOR EACH STATEMENT
    EXECUTE FUNCTION raise_spec_table_append_only_error();
"""


def _create_trigger() -> None:
    binding = op.get_bind()
    binding.execute(sa.text(_TRIGGER_FUNCTION_SQL))
    binding.execute(sa.text(_TRIGGER_SQL))


def _drop_trigger() -> None:
    binding = op.get_bind()
    binding.execute(
        sa.text("DROP TRIGGER IF EXISTS trg_spec_table_append_only ON spec_table")
    )
    binding.execute(sa.text("DROP FUNCTION IF EXISTS raise_spec_table_append_only_error()"))


# ────────────────────────────────────────────────────────────────────
# 表：spec_table（D1 版本账）
# ────────────────────────────────────────────────────────────────────


def _create_table() -> None:
    """建 spec_table 表.

    - 复合主键 (spec_table_id, spec_table_version)：D1 版本账自然键——
      同 id 改版递增 spec_table_version，老版本行保留供历史报告引用。
      与 item_param 的 (item_version_id, purpose_scope, source, method_version,
      as_of) UNIQUE 同手法：身份 + 版本共同标识一行。
    - gradeband：学段（L/M/H）
    - graph_release：引用的知识图谱 release id（kp_node 存在性校验依据）
    - cells：list[SpecCell dict]，JSONB；schema 校验在 Pydantic 层
    - created_at / created_by：版本账留档
    """
    op.create_table(
        "spec_table",
        sa.Column("spec_table_id", sa.Text(), nullable=False),
        sa.Column("spec_table_version", sa.Text(), nullable=False),
        sa.Column("gradeband", sa.Text(), nullable=False),
        sa.Column("graph_release", sa.Text(), nullable=False),
        sa.Column("cells", JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint(
            "spec_table_id",
            "spec_table_version",
            name="pk_spec_table",
        ),
        sa.CheckConstraint(
            "gradeband IN ('L', 'M', 'H')",
            name="ck_spec_table_gradeband_domain",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(cells) = 'array'",
            name="ck_spec_table_cells_is_array",
        ),
    )
    # 按学段查询（测量卷按学段取细目表）
    op.create_index(
        "ix_spec_table_gradeband",
        "spec_table",
        ["gradeband"],
    )
    # 按图谱 release 查询（图谱升级时盘点受影响的细目表）
    op.create_index(
        "ix_spec_table_graph_release",
        "spec_table",
        ["graph_release"],
    )


def _drop_table() -> None:
    op.drop_table("spec_table")


# ────────────────────────────────────────────────────────────────────
# upgrade / downgrade
# ────────────────────────────────────────────────────────────────────


def upgrade() -> None:
    """建 spec_table 表 + append-only 触发器."""
    _create_table()
    _create_trigger()


def downgrade() -> None:
    """回滚：删表 + 触发器."""
    _drop_trigger()
    _drop_table()
