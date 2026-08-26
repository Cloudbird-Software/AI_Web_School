"""T-W5-019 active 指针唯一性防线声明 + 切换留痕主体字段（estimator_run）.

事实核验后本迁移不新建偏唯一索引：uq_estimator_run_one_active_per_scope
（purpose_scope WHERE retired_at IS NULL）已由 0016 建立且双源一致；
同一谓词重复建索引只增加写放大而不增强不变量，验收 #1 以既有索引为准。

本迁移补齐并发切换留痕缺失的「谁」（任务卡验收 #3）：
- estimator_run 新增 activated_by TEXT NOT NULL DEFAULT 'system'
- 存量行回填 'system'（登记主体不可考的历史行）
配合既有 activated_at 与 retired_at 退役链，可完整还原
「谁在何时把 scope 从版本 A 切到版本 B」的 append-only 时间线。

链序说明：down_revision 暂指 0023；T-W5-001（0024）为并行分支，
合入时需把本文件 down_revision 改指 0024 以保 alembic 线性链。
golang-migrate 主源（db/migrations）按版本号排序，不受影响。
可逆性（make migrate-go-check）：upgrade→downgrade→upgrade 全绿。
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0025"
down_revision: Union[str, None] = "0024"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """新增 activated_by 并回填，补齐切换留痕的操作者维度."""
    op.add_column(
        "estimator_run", sa.Column("activated_by", sa.Text(), nullable=True)
    )
    op.execute(
        "UPDATE estimator_run SET activated_by = 'system' WHERE activated_by IS NULL"
    )
    op.alter_column(
        "estimator_run",
        "activated_by",
        existing_type=sa.Text(),
        server_default=sa.text("'system'"),
        nullable=False,
    )


def downgrade() -> None:
    """删除 activated_by（对称回滚，仅回收本迁移引入的列）."""
    op.alter_column(
        "estimator_run",
        "activated_by",
        existing_type=sa.Text(),
        server_default=None,
    )
    op.drop_column("estimator_run", "activated_by")
