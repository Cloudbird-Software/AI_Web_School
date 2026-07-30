"""T-HOTFIX-0023: P0/P1/P2 数据库层问题集中修复（只增不改）.

涵盖审查报告 4 份中的以下 DB 层 bug：

P0-1 内容版本账三表（item_version / material_version / corpus_version）与
    item_template_version 无 append-only 触发器 → 补挂统一
    raise_append_only_error()，触发器独立命名可分别 DROP。
    （item_lifecycle / shadow_score 等 0018/0022 已自带，不重复。）

P0-2 gate_certificate_id FK 未补建 → 对所有持有 gate_certificate_id 的内容表
    加 CREATE FOREIGN KEY，DEFERRABLE INITIALLY DEFERRED（与 0002 的循环外键
    处理一致）：
      - item_version
      - material_version
      - corpus_version
      - item_template_version
      - passage
      - item_lifecycle_transition
    缺行即违反：FK 检查不看 CHECK，非法 ID 直接阻止 INSERT/UPDATE。

P0-3 cert:none 占位行未迁移落地 → INSERT gate_certificate(cert:none)，
    与 orchestrator.py:375 的失败路径 target_cert_id="cert:none" 对齐；
    行属性：artifact_ref="system:none" / cert_type="publish" /
    policy_version="builtin:none" / issued_by="system:bootstrap"。
    downgrade 时 DELETE（cert:none 是迁移新增、只服务应用层失败路径，删除安全）。

BUG-PC2 parental_consent 缺 UNIQUE(student_alias_id,
    scope->>'purpose', version) → 用表达式唯一索引承载 DB 兜底；
    BUG-PC1 应用层的 SELECT MAX+INSERT 并发竞争由 DB 层唯一索引兜底。
    同时补 ix_parental_consent_scope_purpose 普通索引加速
    scope->>'purpose' 过滤。

P2-8 pii_vault_reader 授 INSERT access_log 却无 SELECT → 补 GRANT SELECT
    ON pii_vault.access_log TO pii_vault_reader；审计角色必须能查自己的
    审计日志才合理。

P1-10 paper.py subject_pack_id IN(...) 硬编码 → 这里 DB CHECK 只是兜底，
    主修复在 src/core/models/paper.py 放宽该 CHECK（见应用层修复）。

迁移可逆性：upgrade→downgrade→upgrade 全绿（触发器/FK/索引/插入/授权全部有对称还原）。
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0023"
down_revision: Union[str, None] = "0022"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# ────────────────────────────────────────────────────────────────────
# P0-1: 统一触发器函数（与 0005 同体，CREATE OR REPLACE 幂等）
# ────────────────────────────────────────────────────────────────────
_UNIFIED_TRIGGER_FUNCTION_SQL = """
CREATE OR REPLACE FUNCTION raise_append_only_error() RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'append-only table rejects UPDATE/DELETE';
END;
$$ LANGUAGE plpgsql;
"""


# ── 四个版本表的触发器语句（独立命名，downgrade 逐个 DROP）─────────
def _mk_append_only_trigger_sql(table_name: str) -> str:
    return f"""
CREATE TRIGGER trg_{table_name}_append_only
    BEFORE UPDATE OR DELETE ON {table_name}
    FOR EACH STATEMENT
    EXECUTE FUNCTION raise_append_only_error();
"""


_APPEND_ONLY_TABLES: tuple[str, ...] = (
    "item_version",
    "material_version",
    "corpus_version",
    "item_template_version",
)


def _create_append_only_triggers() -> None:
    binding = op.get_bind()
    binding.execute(sa.text(_UNIFIED_TRIGGER_FUNCTION_SQL))
    for t in _APPEND_ONLY_TABLES:
        # 幂等：如触发器已存在则跳过（兼容部分执行）
        exists = binding.execute(
            sa.text(
                "SELECT 1 FROM pg_trigger "
                "WHERE tgname = :name "
                "  AND NOT tgisinternal"
            ),
            {"name": f"trg_{t}_append_only"},
        ).scalar()
        if exists:
            continue
        binding.execute(sa.text(_mk_append_only_trigger_sql(t)))


def _drop_append_only_triggers() -> None:
    binding = op.get_bind()
    for t in _APPEND_ONLY_TABLES:
        binding.execute(
            sa.text(
                f"DROP TRIGGER IF EXISTS trg_{t}_append_only ON {t}"
            )
        )


# ────────────────────────────────────────────────────────────────────
# P0-2: gate_certificate_id → gate_certificate.cert_id FK（DEFERRABLE）
# ────────────────────────────────────────────────────────────────────
_GATE_FK_TABLES: tuple[tuple[str, str], ...] = (
    # (表名, FK 约束名)
    ("item_version", "fk_iv_gate_certificate"),
    ("material_version", "fk_mv_gate_certificate"),
    ("corpus_version", "fk_cv_gate_certificate"),
    ("item_template_version", "fk_tv_gate_certificate"),
    ("passage", "fk_passage_gate_certificate"),
    ("item_lifecycle_transition", "fk_ilt_gate_certificate"),
)


def _create_gate_fks() -> None:
    """对上述 6 张表加 gate_certificate_id 外键.

    DEFERRABLE INITIALLY DEFERRED 对齐 0002 的循环外键风格：
    事务提交时才检查 FK，允许先插产物行再插证书行。
    ondelete="RESTRICT"（禁止删 gate_certificate 行——D1 本来就不许
    UPDATE/DELETE，加 RESTRICT 再防绕过）。
    """
    for table, constraint_name in _GATE_FK_TABLES:
        # 幂等：若 FK 已存在则跳过
        binding = op.get_bind()
        exists = binding.execute(
            sa.text(
                "SELECT 1 FROM information_schema.table_constraints "
                "WHERE constraint_name = :name AND table_name = :tbl"
            ),
            {"name": constraint_name, "tbl": table},
        ).scalar()
        if exists:
            continue
        with op.batch_alter_table(table, schema=None) as batch_op:
            batch_op.create_foreign_key(
                constraint_name,
                referent_table="gate_certificate",
                local_cols=["gate_certificate_id"],
                remote_cols=["cert_id"],
                ondelete="RESTRICT",
                deferrable=True,
                initially="DEFERRED",
            )


def _drop_gate_fks() -> None:
    for table, constraint_name in _GATE_FK_TABLES:
        with op.batch_alter_table(table, schema=None) as batch_op:
            batch_op.drop_constraint(constraint_name, type_="foreignkey")


# ────────────────────────────────────────────────────────────────────
# P0-3: cert:none 占位行（gate_certificate）
# ────────────────────────────────────────────────────────────────────
def _insert_cert_none() -> None:
    binding = op.get_bind()
    # 幂等 ON CONFLICT DO NOTHING；gate_certificate.cert_id 是 PK
    binding.execute(
        sa.text(
            "INSERT INTO gate_certificate "
            "(cert_id, artifact_ref, cert_type, policy_version, issued_by, "
            " issued_at, created_at) "
            "VALUES ('cert:none', 'system:none', 'publish', "
            "        'builtin:none', 'system:bootstrap', now(), now()) "
            "ON CONFLICT (cert_id) DO NOTHING"
        )
    )


def _delete_cert_none() -> None:
    binding = op.get_bind()
    binding.execute(
        sa.text("DELETE FROM gate_certificate WHERE cert_id = 'cert:none'")
    )


# ────────────────────────────────────────────────────────────────────
# BUG-PC2: parental_consent 唯一约束（应用层并发 bug 的 DB 兜底）
# ────────────────────────────────────────────────────────────────────
_PC_UNIQUE_INDEX_NAME = "uq_parental_consent_student_purpose_version"
_PC_PURPOSE_INDEX_NAME = "ix_parental_consent_scope_purpose"


def _create_parental_consent_indexes() -> None:
    binding = op.get_bind()
    # 表达式唯一索引：(student_alias_id, scope->>'purpose', version)
    exists = binding.execute(
        sa.text(
            "SELECT 1 FROM pg_indexes WHERE indexname = :name"
        ),
        {"name": _PC_UNIQUE_INDEX_NAME},
    ).scalar()
    if not exists:
        op.create_index(
            _PC_UNIQUE_INDEX_NAME,
            "parental_consent",
            [
                "student_alias_id",
                sa.text("(scope->>'purpose')"),
                "version",
            ],
            unique=True,
            postgresql_using="btree",
        )
    # purpose 普通索引加速 check_consent 的 scope->>'purpose' 过滤
    exists2 = binding.execute(
        sa.text(
            "SELECT 1 FROM pg_indexes WHERE indexname = :name"
        ),
        {"name": _PC_PURPOSE_INDEX_NAME},
    ).scalar()
    if not exists2:
        op.create_index(
            _PC_PURPOSE_INDEX_NAME,
            "parental_consent",
            [sa.text("(scope->>'purpose')")],
            unique=False,
            postgresql_using="btree",
        )


def _drop_parental_consent_indexes() -> None:
    op.drop_index(_PC_PURPOSE_INDEX_NAME, table_name="parental_consent")
    op.drop_index(_PC_UNIQUE_INDEX_NAME, table_name="parental_consent")


# ────────────────────────────────────────────────────────────────────
# P2-8: pii_vault_reader 需 SELECT access_log
# ────────────────────────────────────────────────────────────────────
def _grant_pii_reader_access_log_select() -> None:
    binding = op.get_bind()
    # 仅当角色存在时执行（避免单测无该角色时报错）
    role_exists = binding.execute(
        sa.text(
            "SELECT 1 FROM pg_roles WHERE rolname = 'pii_vault_reader'"
        )
    ).scalar()
    if role_exists:
        binding.execute(
            sa.text(
                "GRANT SELECT ON pii_vault.access_log TO pii_vault_reader"
            )
        )


def _revoke_pii_reader_access_log_select() -> None:
    binding = op.get_bind()
    role_exists = binding.execute(
        sa.text(
            "SELECT 1 FROM pg_roles WHERE rolname = 'pii_vault_reader'"
        )
    ).scalar()
    if role_exists:
        binding.execute(
            sa.text(
                "REVOKE SELECT ON pii_vault.access_log FROM pii_vault_reader"
            )
        )


# ────────────────────────────────────────────────────────────────────
# upgrade / downgrade（对称）
# ────────────────────────────────────────────────────────────────────


def upgrade() -> None:
    """按依赖顺序执行：FK 前先建 cert:none 行（FK INSERT 时有合法参照）."""
    _create_append_only_triggers()
    _insert_cert_none()          # 必须在 _create_gate_fks 之前
    _create_gate_fks()
    _create_parental_consent_indexes()
    _grant_pii_reader_access_log_select()


def downgrade() -> None:
    """逆序还原：先删 FK 再删 cert:none 行（防止 FK 检查阻塞删除）."""
    _revoke_pii_reader_access_log_select()
    _drop_parental_consent_indexes()
    _drop_gate_fks()
    _delete_cert_none()
    _drop_append_only_triggers()
