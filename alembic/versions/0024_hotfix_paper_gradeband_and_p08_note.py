"""T-HOTFIX-0024: paper.subject_pack_id CHECK 约束由枚举放宽为格式 + 长度约束（P1-10）.

与 src/core/models/paper.py ORM 层 __table_args__ 的修改对齐：
原 ck_paper_subject_pack_domain:
    IN ('subject-math','subject-chinese','subject-english')
    —— 违反宪法 A5（核心域学科中立，不应硬编码具体学科）。
新 ck_paper_subject_pack_domain:
    subject_pack_id LIKE 'subject-%' AND length(subject_pack_id) <= 64
    —— 只校验前缀格式与上限长度，真实学科包注册由 SubjectPackRegistry
       在应用层校验（A5 核心域不 import 学科包）。

迁移可逆性：upgrade→downgrade→upgrade 全绿（DROP + CREATE 对称操作）。
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0024"
down_revision: Union[str, None] = "0023"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# ────────────────────────────────────────────────────────────────────
# 新旧 CHECK 表达式
# ────────────────────────────────────────────────────────────────────
_OLD_CK_SQL = (
    "subject_pack_id IN "
    "('subject-math', 'subject-chinese', 'subject-english')"
)
_NEW_CK_SQL = (
    "subject_pack_id LIKE 'subject-%' "
    "AND length(subject_pack_id) <= 64"
)
_CK_NAME = "ck_paper_subject_pack_domain"
_TABLE = "paper"


def upgrade() -> None:
    """DROP 旧枚举 CHECK，CREATE 新格式+长度 CHECK."""
    op.drop_constraint(_CK_NAME, _TABLE, type_="check")
    op.create_check_constraint(
        _CK_NAME,
        _TABLE,
        _NEW_CK_SQL,
    )


def downgrade() -> None:
    """DROP 新格式+长度 CHECK，CREATE 回旧枚举 CHECK."""
    op.drop_constraint(_CK_NAME, _TABLE, type_="check")
    op.create_check_constraint(
        _CK_NAME,
        _TABLE,
        _OLD_CK_SQL,
    )
