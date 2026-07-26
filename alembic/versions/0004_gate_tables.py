"""T-W1-006 校验域三表：gate_certificate + gate_run + gate_verdict.

按 specs/constitution.md#D1-D2 与 specs/contracts/db/item-model.md#4.3 落地
「校验签发账」（D1 三本账之三）：

- gate_certificate：门证书（签发后只增不改）。一次成功校验完成后签发，
  作为发布事务的合法凭证（item_version.gate_certificate_id 的合法来源）。
- gate_run：一次校验运行的记录。包含策略版本/验证器版本/判定/证据/耗时/成本。
  一次 certificate 签发对应一次或多次 run（多验证器协同）。
- gate_verdict：每个验证器的判定结果明细。一次 run 可有多条 verdict
  （例如多步骤/多规则分别记录）。

替代 T-W0-005 中的 gate_certificate 占位表（0001 创建的 BIGINT id + created_at）。
response_event 占位表已由 0003 替换，本迁移只替换 gate_certificate。

D1 物理强制：三表均挂 BEFORE UPDATE OR DELETE FOR EACH STATEMENT 触发器，
RAISE EXCEPTION——不依赖角色体系（X7）。复用 0003 创建的 raise_append_only_error()
函数；如不存在则在本迁移中创建（兼容单独跑 0004 的场景）。

迁移可逆性（make migrate-check）：upgrade→downgrade→upgrade 全绿。
downgrade 重建 0001 占位表（与 0001 完全一致）。
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ENUM as PG_ENUM
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# ────────────────────────────────────────────────────────────────────
# 触发器函数：append-only 物理强制（D1）
# ────────────────────────────────────────────────────────────────────
# 复用 0003 的 raise_append_only_error()；若不存在（例如单独跑 0004）则创建。
# 函数体只 RAISE EXCEPTION，与 0003 完全一致——重复 CREATE OR REPLACE 无副作用。
_ENSURE_TRIGGER_FUNCTION_SQL = """
CREATE OR REPLACE FUNCTION raise_append_only_error() RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'gate table is append-only (D1): UPDATE/DELETE forbidden';
END;
$$ LANGUAGE plpgsql;
"""

# 三表挂同一函数的语句级触发器；分别命名以便单独 DROP。
_TRIGGER_GATE_CERT_SQL = """
CREATE TRIGGER trg_gate_certificate_append_only
    BEFORE UPDATE OR DELETE ON gate_certificate
    FOR EACH STATEMENT
    EXECUTE FUNCTION raise_append_only_error();
"""

_TRIGGER_GATE_RUN_SQL = """
CREATE TRIGGER trg_gate_run_append_only
    BEFORE UPDATE OR DELETE ON gate_run
    FOR EACH STATEMENT
    EXECUTE FUNCTION raise_append_only_error();
"""

_TRIGGER_GATE_VERDICT_SQL = """
CREATE TRIGGER trg_gate_verdict_append_only
    BEFORE UPDATE OR DELETE ON gate_verdict
    FOR EACH STATEMENT
    EXECUTE FUNCTION raise_append_only_error();
"""


# ────────────────────────────────────────────────────────────────────
# 枚举：gate_run.verdict（pass/fail/review）
# ────────────────────────────────────────────────────────────────────
# 为什么单独 enum 而非 text+CHECK：enum 在 information_schema 有明确类型名，
# 便于契约对照测试；CHECK 约束失效时 enum 仍兜底。
# review 表示人工复核状态（验证器无法自动判定时进入人工门）。


def _create_verdict_enum() -> None:
    binding = op.get_bind()
    exists = binding.execute(
        sa.text("SELECT 1 FROM pg_type WHERE typname = 'gate_run_verdict_enum'")
    ).scalar()
    if exists:
        return
    binding.execute(
        sa.text(
            "CREATE TYPE gate_run_verdict_enum AS ENUM ('pass', 'fail', 'review')"
        )
    )


def _drop_verdict_enum() -> None:
    binding = op.get_bind()
    binding.execute(sa.text("DROP TYPE IF EXISTS gate_run_verdict_enum"))


# ────────────────────────────────────────────────────────────────────
# 占位表处理
# ────────────────────────────────────────────────────────────────────
def _drop_placeholder_gate_certificate() -> None:
    """0001 创建的 gate_certificate 占位表（BIGINT id + created_at）让位给真实结构.

    幂等：若占位表已被先前的部分迁移删除，则跳过。
    """
    binding = op.get_bind()
    exists = binding.execute(
        sa.text(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_name = 'gate_certificate'"
        )
    ).scalar()
    if exists:
        op.drop_table("gate_certificate")


# ────────────────────────────────────────────────────────────────────
# 三表创建
# ────────────────────────────────────────────────────────────────────

def _create_gate_certificate() -> None:
    """§4.3 门证书：签发后只增不改；item_version.gate_certificate_id 的合法来源.

    - cert_id：证书 id（应用层 ULID 生成）；text 而非 uuid，与 item_version.gate_certificate_id 类型对齐。
    - artifact_ref：被签发的产物引用（如 item_version_id）；text 不加 FK——
      证书可能签发多种产物（item_version/material_version/corpus_version），
      统一以 text 承载，由应用层校验合法性。
    - cert_type：证书类型（'publish'/'retire' 等），text+CHECK 而非 enum——
      类型集合可能扩展，避免每次扩展跑迁移。
    """
    op.create_table(
        "gate_certificate",
        sa.Column("cert_id", sa.Text(), primary_key=True),
        sa.Column("artifact_ref", sa.Text(), nullable=False),
        sa.Column("cert_type", sa.Text(), nullable=False),
        sa.Column("policy_version", sa.Text(), nullable=False),
        sa.Column("issued_by", sa.Text(), nullable=False),
        sa.Column(
            "issued_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "cert_type IN ('publish', 'retire')",
            name="ck_gc_cert_type_domain",
        ),
    )


def _create_gate_run() -> None:
    """§4.3 一次校验运行记录：策略版本/验证器/判定/证据/成本.

    - certificate_id：关联证书；一次证书签发对应一次或多次 run（多验证器协同）。
    - verdict：pass/fail/review（review=人工复核）。
    - confidence：0.000~1.000，numeric(4,3) 容纳三位小数。
    - cost_ms/cost_tokens：运行成本（耗时+token），便于成本治理与预算告警。
    """
    op.create_table(
        "gate_run",
        sa.Column("run_id", sa.Text(), primary_key=True),
        sa.Column("certificate_id", sa.Text(), nullable=False),
        sa.Column("policy_version", sa.Text(), nullable=False),
        sa.Column("validator_id", sa.Text(), nullable=False),
        sa.Column("validator_version", sa.Text(), nullable=False),
        sa.Column(
            "verdict",
            PG_ENUM(
                "pass", "fail", "review",
                name="gate_run_verdict_enum",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("evidence", JSONB(), nullable=False),
        sa.Column("confidence", sa.Numeric(4, 3), nullable=False),
        sa.Column("cost_ms", sa.Integer(), nullable=False),
        sa.Column("cost_tokens", sa.Integer(), nullable=False),
        sa.Column(
            "run_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["certificate_id"],
            ["gate_certificate.cert_id"],
            name="fk_gr_certificate",
        ),
        sa.CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name="ck_gr_confidence_range",
        ),
        sa.CheckConstraint(
            "cost_ms >= 0 AND cost_tokens >= 0",
            name="ck_gr_cost_nonneg",
        ),
    )


def _create_gate_verdict() -> None:
    """§4.3 验证器判定结果明细：一次 run 可有多条 verdict（多步骤/多规则）.

    - verdict_id：bigint identity PK——明细行无业务 id 需求，自增足够。
    - run_id：关联 run。
    - detail：判定明细（jsonb），结构由验证器自定（命中规则/失败原因/建议等）。
    """
    op.create_table(
        "gate_verdict",
        sa.Column(
            "verdict_id",
            sa.BigInteger(),
            sa.Identity(always=True),
            primary_key=True,
        ),
        sa.Column("run_id", sa.Text(), nullable=False),
        sa.Column("detail", JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["gate_run.run_id"],
            name="fk_gv_run",
        ),
    )


# ────────────────────────────────────────────────────────────────────
# 触发器挂载/卸载
# ────────────────────────────────────────────────────────────────────
def _create_triggers() -> None:
    """D1 append-only 物理强制：三表均挂 BEFORE UPDATE OR DELETE FOR EACH STATEMENT."""
    binding = op.get_bind()
    binding.execute(sa.text(_ENSURE_TRIGGER_FUNCTION_SQL))
    binding.execute(sa.text(_TRIGGER_GATE_CERT_SQL))
    binding.execute(sa.text(_TRIGGER_GATE_RUN_SQL))
    binding.execute(sa.text(_TRIGGER_GATE_VERDICT_SQL))


def _drop_triggers() -> None:
    binding = op.get_bind()
    binding.execute(
        sa.text("DROP TRIGGER IF EXISTS trg_gate_certificate_append_only ON gate_certificate")
    )
    binding.execute(
        sa.text("DROP TRIGGER IF EXISTS trg_gate_run_append_only ON gate_run")
    )
    binding.execute(
        sa.text("DROP TRIGGER IF EXISTS trg_gate_verdict_append_only ON gate_verdict")
    )
    # 不删 raise_append_only_error() 函数：0003 也用它，删了会让 0003 的回滚再升级失败。
    # 函数本身是 CREATE OR REPLACE，留在库里无副作用。


# ────────────────────────────────────────────────────────────────────
# upgrade / downgrade
# ────────────────────────────────────────────────────────────────────

def upgrade() -> None:
    """建 gate 三表（替换占位）+ 触发器."""
    _create_verdict_enum()
    # 先删 0001 的 gate_certificate 占位表
    _drop_placeholder_gate_certificate()
    # 建三表（按依赖顺序：certificate → run → verdict）
    _create_gate_certificate()
    _create_gate_run()
    _create_gate_verdict()
    # 触发器（在所有表建完后）
    _create_triggers()


def downgrade() -> None:
    """回滚：删 gate 三表 + 触发器，重建 0001 的 gate_certificate 占位表.

    为什么重建占位：migrate-check 跑 upgrade→downgrade→upgrade，downgrade 必须
    让库回到 0003 的状态（gate_certificate 占位表来自 0001，0003 未触碰），
    否则再次 upgrade 会在 _drop_placeholder_gate_certificate 处失败。
    """
    _drop_triggers()
    # 反序删表（先 verdict → run → certificate，解除 FK 依赖）
    op.drop_table("gate_verdict")
    op.drop_table("gate_run")
    op.drop_table("gate_certificate")
    _drop_verdict_enum()
    # 重建 0001 的 gate_certificate 占位表（与 0001 migration 完全一致）
    op.create_table(
        "gate_certificate",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
