"""T-W5-011 授权版本唯一性防线 + 留痕主体字段（parental_consent）.

事实核验后本迁移把 0015 的非唯一索引 ix_parental_consent_student_purpose_version
升级为同一列集的唯一索引：MAX(version)+1 读后写分配下并发 grant/revoke 会产生
同版本号双行，check_consent 取到哪条不确定（合规账最不该有的不确定性）。
同一列集先 DROP 后 CREATE UNIQUE，不重复建第二份索引——重复索引只增加写放大
而不增强不变量。版本号自此在 (student_alias_id, purpose) 链内全局无重，
「永远取最新版本」由 DB 强制；应用层并发分配由 core/compliance 的
per-chain advisory xact lock 承担，本唯一索引是最后防线。

留痕维度：新增 recorded_by TEXT NOT NULL DEFAULT 'system'（授权事件登记主体），
配合既有 created_at 与单调 version 可完整还原「谁在何时把授权链从版本 A
推进到版本 B」。与 T-W5-019 对 estimator_run 补 activated_by 同构。

为什么用「带 DEFAULT 的单条 ADD COLUMN NOT NULL」而不 UPDATE 回填：
本表被 trg_parental_consent_append_only 物理禁止一切 UPDATE/DELETE，
UPDATE 回填方案第一步即被触发器拒绝；ADD COLUMN ... NOT NULL DEFAULT 由 PG
fast default 直接物化存量行默认值，不发任何 UPDATE 语句。

链序说明：down_revision 指 0025；0026 已被并行波次预留占用，合入顺序若发生
调整需按 alembic 实际线形重指（golang-migrate 主源按版本号排序，不受影响）。
可逆性（make migrate-check / migrate-go-check）：upgrade→downgrade→upgrade 全绿。
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0027"
down_revision = "0026"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """授权版本唯一性防线 + 登记主体留痕列."""
    op.add_column(
        "parental_consent",
        sa.Column(
            "recorded_by",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'system'"),
        ),
    )
    op.drop_index(
        "ix_parental_consent_student_purpose_version",
        table_name="parental_consent",
    )
    op.create_index(
        "uq_parental_consent_version_per_purpose",
        "parental_consent",
        ["student_alias_id", sa.text("(scope ->> 'purpose')"), "version"],
        unique=True,
    )


def downgrade() -> None:
    """退回 0015 形态：非唯一索引恢复原名原形，留痕列删除."""
    op.drop_index(
        "uq_parental_consent_version_per_purpose",
        table_name="parental_consent",
    )
    op.create_index(
        "ix_parental_consent_student_purpose_version",
        "parental_consent",
        ["student_alias_id", sa.text("(scope ->> 'purpose')"), "version"],
        unique=False,
    )
    op.drop_column("parental_consent", "recorded_by")
