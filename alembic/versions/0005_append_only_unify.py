"""W1 修复包：P9 触发器函数统一 + P8 corpus_version 补门字段.

P9（触发器函数互覆）：0003 与 0004 都用 CREATE OR REPLACE 定义同名函数
raise_append_only_error()，但消息文案不同——后跑的迁移覆盖先跑的，
同一函数在不同库实例上行为不一致。本迁移统一为单一定义，通用消息
'append-only table rejects UPDATE/DELETE'，同时服务 response_event 与
gate 三表（四表挂同一函数，触发器本身不变，只换函数体）。
不改 0003/0004 历史迁移（X7 迁移只增不改）。

P8（corpus_version 补门字段）：对齐 material_version（0002_item_model.py），
补 gate_certificate_id / published_at / retired_at；status 列 0002 已建
（item_version_status_enum NOT NULL），此处补 server_default 'draft' 与
§4 规则 1 门强制 CHECK（published_at 非空必伴随 gate_certificate_id 非空，
约束名 ck_cv_published_requires_gate_cert，与 ck_mv_* 同构）。

迁移可逆性（make migrate-check）：upgrade→downgrade→upgrade 全绿。
downgrade 将函数体还原为 0004 落地后的定义（gate 文案），删除新增列与 CHECK。
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# ────────────────────────────────────────────────────────────────────
# P9：统一 append-only 触发器函数为单一定义
# ────────────────────────────────────────────────────────────────────
# 为什么用通用消息：同一函数服务 response_event 与 gate 三表，表名特异消息
# 会互相覆盖（0003/0004 各写一份）；通用消息 + 触发器名（trg_*_append_only）
# 已足够定位违规表（PG 报错自带 table/trigger 上下文）。
_UNIFIED_TRIGGER_FUNCTION_SQL = """
CREATE OR REPLACE FUNCTION raise_append_only_error() RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'append-only table rejects UPDATE/DELETE';
END;
$$ LANGUAGE plpgsql;
"""

# downgrade 还原：0004 是 0005 之前最后一个定义该函数的迁移，
# 其 CREATE OR REPLACE 覆盖了 0003 的版本，故 0005 之前库里的生效定义是 0004 文案。
_PRE_0005_TRIGGER_FUNCTION_SQL = """
CREATE OR REPLACE FUNCTION raise_append_only_error() RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'gate table is append-only (D1): UPDATE/DELETE forbidden';
END;
$$ LANGUAGE plpgsql;
"""


# ────────────────────────────────────────────────────────────────────
# P8：corpus_version 补门字段（对齐 material_version）
# ────────────────────────────────────────────────────────────────────
def _add_corpus_version_gate_columns() -> None:
    """补 gate_certificate_id / published_at / retired_at + status 默认值 + 门 CHECK."""
    # status 列 0002 已建（item_version_status_enum NOT NULL，无默认值）；
    # 此处仅补 server_default 'draft'——与 §4 状态机入口一致，新行默认草稿态。
    op.alter_column(
        "corpus_version",
        "status",
        server_default=sa.text("'draft'"),
    )
    op.add_column(
        "corpus_version",
        sa.Column("gate_certificate_id", sa.Text(), nullable=True),
    )
    op.add_column(
        "corpus_version",
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "corpus_version",
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
    )
    # §4 规则 1 / §6.4 门强制：published_at 非空必伴随 gate_certificate_id 非空
    # （与 item_version/material_version 的 ck_iv_*/ck_mv_* 同构）
    op.create_check_constraint(
        "ck_cv_published_requires_gate_cert",
        "corpus_version",
        "published_at IS NULL OR gate_certificate_id IS NOT NULL",
    )


def _drop_corpus_version_gate_columns() -> None:
    op.drop_constraint(
        "ck_cv_published_requires_gate_cert", "corpus_version", type_="check"
    )
    op.drop_column("corpus_version", "retired_at")
    op.drop_column("corpus_version", "published_at")
    op.drop_column("corpus_version", "gate_certificate_id")
    # 还原 status 无默认值（回到 0002 的定义）
    op.alter_column("corpus_version", "status", server_default=None)


# ────────────────────────────────────────────────────────────────────
# upgrade / downgrade
# ────────────────────────────────────────────────────────────────────

def upgrade() -> None:
    """统一触发器函数定义 + corpus_version 补门字段."""
    binding = op.get_bind()
    binding.execute(sa.text(_UNIFIED_TRIGGER_FUNCTION_SQL))
    _add_corpus_version_gate_columns()


def downgrade() -> None:
    """回滚：还原 0004 版函数定义，删除 corpus_version 新增列与 CHECK."""
    _drop_corpus_version_gate_columns()
    binding = op.get_bind()
    binding.execute(sa.text(_PRE_0005_TRIGGER_FUNCTION_SQL))
