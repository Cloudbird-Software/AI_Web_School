"""T-W0-005 首个迁移：三张占位表（item / gate_certificate / response_event）。

W0 仅验证迁移纪律可逆，不落真实业务表结构（随 W1 契约落地）。
每张表两列：id（BIGINT 标识列主键）+ created_at（带时区的默认时间戳）。
upgrade 与 downgrade 均真实实现——downgrade 必须把表删掉，禁止空 pass。

对应宪法 D1（三本账：内容版本/作答事件/校验签发）的占位入口。
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# W0 占位表统一定义：业务字段在 W1 契约落地后由后续迁移补全。
_PLACEHOLDER_COLUMNS = (
    sa.Column(
        "id",
        sa.BigInteger(),
        sa.Identity(always=True),
        primary_key=True,
    ),
    sa.Column(
        "created_at",
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.text("now()"),
    ),
)

_TABLES = ("item", "gate_certificate", "response_event")


def upgrade() -> None:
    """建三张占位表。"""
    for table in _TABLES:
        op.create_table(table, *_PLACEHOLDER_COLUMNS)


def downgrade() -> None:
    """删三张占位表——真实可逆，不是空 pass。"""
    # 反向删除以避免未来外键依赖（当前无外键，保持对称习惯）
    for table in reversed(_TABLES):
        op.drop_table(table)
