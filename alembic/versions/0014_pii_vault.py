"""T-W4-031 PII 保险库独立 schema + 列级加密表.

按架构 v2 §4.8 与宪法 D7 落地 PII 保险库：
- 独立 schema `pii_vault`，与主库 public 物理隔离。
- 列级加密：直标识字段以 bytea 密文 + 独立 nonce 落库。
  加解密在应用层 src/core/compliance/pii_encryption.py 完成（AES-256-GCM，
  密钥环境变量 PII_VAULT_KEY 注入，永不入 SQL/日志，X3）。
- 白名单访问控制：REVOKE ALL FROM PUBLIC；仅 pii_vault_reader 可 SELECT。
- 主库零直标识：public schema 仅 student_alias_id（UUID 不可逆别名）。

为什么不用 pgcrypto 在 DB 侧加解密：密钥会进 SQL bind param / query log，
违反 X3。应用层加解密让密钥只在 Python 进程内存中短暂存在。

迁移可逆性（make migrate-check）：upgrade→downgrade→upgrade 全绿。
downgrade 删除 schema（CASCADE 删表）+ 角色，回到 0013 后状态。
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

# revision identifiers, used by Alembic.
revision: str = "0014"
down_revision: Union[str, None] = "0013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# ────────────────────────────────────────────────────────────────────
# 角色：白名单访问控制（pii_vault_reader NOLOGIN，仅授权读）
# ────────────────────────────────────────────────────────────────────
_CREATE_ROLE_SQL = """
DO $$ BEGIN
    CREATE ROLE pii_vault_reader NOLOGIN;
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;
"""

# #43 Major 修复：down 不再 DROP 集群级角色——角色可能被同集群其他库的 ACL
# 依赖引用，DROP 会使全量 down 在共享集群上失败。角色生命周期归部署/DBA；
# 迁移只管本库 schema 与权限（up 的幂等 CREATE NOLOGIN 保留，供全新实例）。
_DROP_ROLE_SQL = None  # 保留常量位防误用：down 不 DROP ROLE


# ────────────────────────────────────────────────────────────────────
# Schema + 表
# ────────────────────────────────────────────────────────────────────
def _create_schema_and_tables() -> None:
    """建独立 schema + student_identity / access_log 两表.

    - student_identity：学生直标识密文（PK=student_alias_id，与主库匿名别名同源）
    - access_log：访问审计日志（W4 预留结构；写入由应用层触发）
    """
    # 独立 schema（与 public 物理隔离）
    op.execute("CREATE SCHEMA IF NOT EXISTS pii_vault")

    # 默认拒绝：所有角色对 pii_vault 无任何权限
    op.execute("REVOKE ALL ON SCHEMA pii_vault FROM PUBLIC")
    op.execute("REVOKE ALL ON ALL TABLES IN SCHEMA pii_vault FROM PUBLIC")

    # student_identity：直标识密文表
    op.create_table(
        "student_identity",
        sa.Column("student_alias_id", PG_UUID(as_uuid=True), primary_key=True),
        # 直标识密文 + nonce（AES-256-GCM；nonce 96-bit 每次写入随机）
        sa.Column("name_ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("name_nonce", sa.LargeBinary(), nullable=False),
        sa.Column("phone_ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("phone_nonce", sa.LargeBinary(), nullable=False),
        sa.Column("address_ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("address_nonce", sa.LargeBinary(), nullable=False),
        sa.Column("parent_contact_ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("parent_contact_nonce", sa.LargeBinary(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        schema="pii_vault",
    )

    # access_log：访问审计（W4 结构预留）
    op.create_table(
        "access_log",
        sa.Column("access_id", PG_UUID(as_uuid=True), primary_key=True),
        sa.Column("student_alias_id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("accessor", sa.Text(), nullable=False),
        sa.Column(
            "accessed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("purpose", sa.Text(), nullable=False),
        schema="pii_vault",
    )


def _grant_privileges() -> None:
    """白名单授权：pii_vault_reader 仅 SELECT student_identity."""
    op.execute("GRANT USAGE ON SCHEMA pii_vault TO pii_vault_reader")
    op.execute("GRANT SELECT ON pii_vault.student_identity TO pii_vault_reader")
    op.execute("GRANT INSERT ON pii_vault.access_log TO pii_vault_reader")
    # 默认 REVOKE 兜底（防止未来新建表意外可读）
    op.execute("REVOKE ALL ON pii_vault.student_identity FROM PUBLIC")
    op.execute("REVOKE ALL ON pii_vault.access_log FROM PUBLIC")


# ────────────────────────────────────────────────────────────────────
# upgrade / downgrade
# ────────────────────────────────────────────────────────────────────

def upgrade() -> None:
    """建 PII 保险库 schema + 表 + 角色 + 白名单授权."""
    op.execute(_CREATE_ROLE_SQL)
    _create_schema_and_tables()
    _grant_privileges()


def downgrade() -> None:
    """删 PII 保险库 schema（CASCADE 删表）；集群级角色不 DROP."""
    # CASCADE：删 schema 自动删内部所有表
    op.execute("DROP SCHEMA IF EXISTS pii_vault CASCADE")
    # #43 Major：不 DROP 集群级角色——角色可能被同集群其他库的 ACL 依赖引用，
    # DROP 会使全量 down 在共享集群上失败。角色生命周期归部署/DBA。
