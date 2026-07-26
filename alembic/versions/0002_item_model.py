"""T-W1-002 统一内容模型 DDL 迁移.

按 specs/contracts/db/item-model.md v1.1 落地 item 族 9 表 + item_kp + publication
+ material_license（§2.4 material_version.license_id FK 依赖）。

代替 T-W0-005 中的 `item` 占位表——旧占位迁移（0001）不动，本迁移做结构替换。
`gate_certificate` 与 `response_event` 占位表保留（分别由 T-W1-006/T-W1-005 处理）。

关键纪律（契约 §6 实现注记）：
- 循环外键 item↔item_version / material↔material_version / corpus_asset↔corpus_version
  按 §6.1 用「先建两表、后加 DEFERRABLE INITIALLY DEFERRED FK」处理。
- §6.3 current_version_id 前移触发器：item_version INSERT 且 status='published'
  时自动更新 item.current_version_id（仅发布事务路径，D1）。
- §6.4 / §4 规则 1 门强制：published_at 非空必伴随 gate_certificate_id 非空，
  以 CHECK 约束承载（合法 gate_certificate_id 的 FK 在 T-W1-006 替换占位表后补）。
- item_version.gate_certificate_id 暂为 text 无 FK：W0 占位 gate_certificate.id 是
  BIGINT，类型不匹配；待 T-W1-006 替换占位表后由后续迁移补 FK。
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ENUM as PG_ENUM
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# ────────────────────────────────────────────────────────────────────
# 枚举类型（PG enum 必须先于表创建）
# ────────────────────────────────────────────────────────────────────
ENUM_DEFINITIONS = [
    # A7：四级生产线对等，tier 为谱系字段（不是分区键）
    ("item_tier_enum", ("A", "B", "C", "D")),
    # §4 状态机：draft → quarantined → published → retired（无回边）
    ("item_version_status_enum", ("draft", "quarantined", "published", "retired")),
    # §2.3 母题版本状态：draft/published/retired（无 quarantined，母题不直接过门）
    ("item_template_version_status_enum", ("draft", "published", "retired")),
    # §2.4 素材类型
    ("material_kind_enum", ("passage", "image", "table", "audio")),
    # §2.4 许可决策
    ("material_license_decision_enum", ("approved", "rejected", "expired")),
]


def _create_enums() -> None:
    """创建所有枚举类型（幂等：已存在则跳过）。

    为什么不用 sa.Enum 的自动创建：同一枚举被多表复用（item_version_status_enum
    用于 item_version/material_version/corpus_version），SQLAlchemy 在 create_table
    时会重复 CREATE TYPE 报 duplicate_object；显式 checkfirst 更可控。
    """
    binding = op.get_bind()
    for name, values in ENUM_DEFINITIONS:
        exists = binding.execute(
            sa.text("SELECT 1 FROM pg_type WHERE typname = :name"),
            {"name": name},
        ).scalar()
        if exists:
            continue
        values_sql = ", ".join(f"'{v}'" for v in values)
        binding.execute(sa.text(f"CREATE TYPE {name} AS ENUM ({values_sql})"))


def _drop_enums() -> None:
    """删除所有枚举类型（反序，幂等）。"""
    binding = op.get_bind()
    for name, _ in reversed(ENUM_DEFINITIONS):
        binding.execute(sa.text(f"DROP TYPE IF EXISTS {name}"))


# ────────────────────────────────────────────────────────────────────
# 表创建（按依赖顺序）
# ────────────────────────────────────────────────────────────────────

def _drop_placeholder_item() -> None:
    """0001 创建的 item 占位表（BIGINT id + created_at）让位给真实结构。"""
    op.drop_table("item")


def _create_material_license() -> None:
    """§2.4 许可表：素材许可的来源/权利人/范围/期限/决策。"""
    op.create_table(
        "material_license",
        sa.Column("license_id", sa.Text(), primary_key=True),
        sa.Column("source", sa.Text(), nullable=True),
        sa.Column("rights_holder", sa.Text(), nullable=True),
        sa.Column("scope", sa.Text(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "decision",
            PG_ENUM("approved", "rejected", "expired", name="material_license_decision_enum", create_type=False),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )


def _create_item_template() -> None:
    """§2.3 母题不变身份。template_version_id FK 在 item_template_version 之后加。"""
    op.create_table(
        "item_template",
        sa.Column("template_id", sa.Text(), primary_key=True),
        sa.Column("pack_id", sa.Text(), nullable=False),
        # current_version_id 暂不加 FK，待 item_template_version 建好后补
        sa.Column("current_version_id", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )


def _create_item_template_version() -> None:
    """§2.3 母题版本：dsl_version + spec（六大块母题定义）+ status。"""
    op.create_table(
        "item_template_version",
        sa.Column("template_version_id", sa.Text(), primary_key=True),  # sha256 of spec
        sa.Column("template_id", sa.Text(), nullable=False),
        sa.Column("dsl_version", sa.Text(), nullable=False),
        sa.Column("spec", JSONB(), nullable=False),
        sa.Column(
            "status",
            PG_ENUM("draft", "published", "retired", name="item_template_version_status_enum", create_type=False),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["template_id"], ["item_template.template_id"], name="fk_itv_template"),
    )
    # 母题的 current_version_id 现在可以加 FK（循环外键，DEFERRABLE）
    op.create_foreign_key(
        "fk_item_template_current_version",
        "item_template",
        "item_template_version",
        ["current_version_id"],
        ["template_version_id"],
        deferrable=True,
        initially="DEFERRED",
    )


def _create_item() -> None:
    """§2.1 item 不变身份。current_version_id / template_version_id FK 后补。"""
    op.create_table(
        "item",
        sa.Column("item_id", sa.Text(), primary_key=True),
        sa.Column("pack_id", sa.Text(), nullable=False),
        sa.Column(
            "tier",
            PG_ENUM("A", "B", "C", "D", name="item_tier_enum", create_type=False),
            nullable=False,
        ),
        # template_version_id：A/B 级实例的母题来源；C/D 级为 NULL
        sa.Column("template_version_id", sa.Text(), nullable=True),
        # current_version_id：最新 published 版本指针；FK 后补（循环外键）
        sa.Column("current_version_id", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        # template_version_id FK 可以现在加（item_template_version 已建）
        sa.ForeignKeyConstraint(
            ["template_version_id"],
            ["item_template_version.template_version_id"],
            name="fk_item_template_version",
        ),
    )


def _create_item_version() -> None:
    """§2.2 item_version 不可变内容快照（六大块 JSONB + 状态机 + 门证书）。"""
    op.create_table(
        "item_version",
        sa.Column("item_version_id", sa.Text(), primary_key=True),
        sa.Column("item_id", sa.Text(), nullable=False),
        sa.Column(
            "status",
            PG_ENUM("draft", "quarantined", "published", "retired", name="item_version_status_enum", create_type=False),
            nullable=False,
        ),
        # 六大块（契约 §2.2 / §1）：均为 JSONB
        sa.Column("objective", JSONB(), nullable=False),
        sa.Column("interaction_ref", JSONB(), nullable=False),
        sa.Column("content", JSONB(), nullable=False),
        sa.Column("scoring_ref", JSONB(), nullable=False),
        sa.Column("error_bindings", JSONB(), nullable=False),
        sa.Column("lineage", JSONB(), nullable=False),
        # §2.2 rendered_snapshot：quarantined 前必填（CHECK 兜底）
        sa.Column("rendered_snapshot", JSONB(), nullable=True),
        # §2.2 gate_certificate_id：唯一真源；FK 待 T-W1-006 替换占位表后补
        sa.Column("gate_certificate_id", sa.Text(), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["item_id"], ["item.item_id"], name="fk_iv_item"),
        # §4 规则 1 / §6.4 门强制：published_at 非空必伴随 gate_certificate_id 非空
        sa.CheckConstraint(
            "published_at IS NULL OR gate_certificate_id IS NOT NULL",
            name="ck_iv_published_requires_gate_cert",
        ),
        # §2.2 rendered_snapshot：进入 quarantined 前必填（draft 可空，其余必填）
        sa.CheckConstraint(
            "status = 'draft' OR rendered_snapshot IS NOT NULL",
            name="ck_iv_quarantine_requires_rendered",
        ),
    )
    # 循环外键：item.current_version_id → item_version.item_version_id（DEFERRABLE）
    op.create_foreign_key(
        "fk_item_current_version",
        "item",
        "item_version",
        ["current_version_id"],
        ["item_version_id"],
        deferrable=True,
        initially="DEFERRED",
    )


def _create_material() -> None:
    """§2.4 material 不变身份。current_version_id FK 后补。"""
    op.create_table(
        "material",
        sa.Column("material_id", sa.Text(), primary_key=True),
        sa.Column(
            "kind",
            PG_ENUM("passage", "image", "table", "audio", name="material_kind_enum", create_type=False),
            nullable=False,
        ),
        sa.Column("pack_id", sa.Text(), nullable=True),  # 跨学科通用素材为 'platform'
        sa.Column("current_version_id", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )


def _create_material_version() -> None:
    """§2.4 material_version 不可变内容快照（同 item_version 的两段式，D1 全版本化）。"""
    op.create_table(
        "material_version",
        sa.Column("material_version_id", sa.Text(), primary_key=True),  # H(content_digest)
        sa.Column("material_id", sa.Text(), nullable=False),
        sa.Column("content_ref", sa.Text(), nullable=False),  # 对象存储引用
        sa.Column("license_id", sa.Text(), nullable=False),
        sa.Column(
            "status",
            PG_ENUM("draft", "quarantined", "published", "retired", name="item_version_status_enum", create_type=False),
            nullable=False,
        ),
        sa.Column("lineage", JSONB(), nullable=False),
        sa.Column("gate_certificate_id", sa.Text(), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["material_id"], ["material.material_id"], name="fk_mv_material"),
        sa.ForeignKeyConstraint(["license_id"], ["material_license.license_id"], name="fk_mv_license"),
        sa.CheckConstraint(
            "published_at IS NULL OR gate_certificate_id IS NOT NULL",
            name="ck_mv_published_requires_gate_cert",
        ),
    )
    # 循环外键：material.current_version_id → material_version.material_version_id
    op.create_foreign_key(
        "fk_material_current_version",
        "material",
        "material_version",
        ["current_version_id"],
        ["material_version_id"],
        deferrable=True,
        initially="DEFERRED",
    )


def _create_corpus_asset() -> None:
    """§2.5 corpus_asset 语料库身份。current_version_id FK 后补。"""
    op.create_table(
        "corpus_asset",
        sa.Column("asset_id", sa.Text(), primary_key=True),
        sa.Column("kind", sa.Text(), nullable=False),  # 字/词/篇/句/词表/音标/函数/图库
        sa.Column("pack_id", sa.Text(), nullable=True),
        sa.Column("current_version_id", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )


def _create_corpus_version() -> None:
    """§2.5 corpus_version 语料库版本（版本化、带许可、带谱系；digest 进实例寻址链）。"""
    op.create_table(
        "corpus_version",
        sa.Column("version_id", sa.Text(), primary_key=True),  # 内容寻址 digest
        sa.Column("asset_id", sa.Text(), nullable=False),
        sa.Column("content_ref", sa.Text(), nullable=False),
        sa.Column("license_id", sa.Text(), nullable=False),
        sa.Column("lineage", JSONB(), nullable=False),
        sa.Column(
            "status",
            PG_ENUM("draft", "quarantined", "published", "retired", name="item_version_status_enum", create_type=False),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["asset_id"], ["corpus_asset.asset_id"], name="fk_cv_asset"),
        sa.ForeignKeyConstraint(["license_id"], ["material_license.license_id"], name="fk_cv_license"),
    )
    # 循环外键：corpus_asset.current_version_id → corpus_version.version_id
    op.create_foreign_key(
        "fk_corpus_asset_current_version",
        "corpus_asset",
        "corpus_version",
        ["current_version_id"],
        ["version_id"],
        deferrable=True,
        initially="DEFERRED",
    )


def _create_item_group() -> None:
    """§2.5 item_group 题组/testlet（一材多题 + 组内顺序 + ≤6，R-Z-06）。"""
    op.create_table(
        "item_group",
        sa.Column("item_group_id", sa.Text(), primary_key=True),
        # 引用素材版本（非素材身份），保证历史试卷可回溯（D1）
        sa.Column("material_version_id", sa.Text(), nullable=True),
        # item_version_ids：题组内题目版本列表（text[]，组内顺序由 ordered 决定）
        sa.Column("item_version_ids", sa.ARRAY(sa.Text()), nullable=False),
        sa.Column("ordered", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("testlet", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(
            ["material_version_id"],
            ["material_version.material_version_id"],
            name="fk_ig_material_version",
        ),
    )
    # R-Z-06：题组 ≤6 题（DB 层 CHECK）
    op.create_check_constraint(
        "ck_ig_max_six_items",
        "item_group",
        "array_length(item_version_ids, 1) <= 6",
    )


def _create_item_kp() -> None:
    """item_kp 知识点标注索引表（denormalized from item_version.objective.kp_set）。

    为什么需要：item_version.objective 是 JSONB，无法直接索引到 kp_code 层级；
    item_kp 提供 (kp_code, dimension) 的扁平索引，供组装域按知识点查询题目。
    """
    op.create_table(
        "item_kp",
        sa.Column("item_kp_id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column("item_id", sa.Text(), nullable=False),
        sa.Column("item_version_id", sa.Text(), nullable=False),
        sa.Column("dimension", sa.Text(), nullable=False),  # 'kp' 等
        sa.Column("kp_code", sa.Text(), nullable=False),  # e.g. 'math.nal.decimal.compare'
        sa.Column("gradeband", sa.Text(), nullable=True),  # 'L'/'M'/'H'
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["item_id"], ["item.item_id"], name="fk_ikp_item"),
        sa.ForeignKeyConstraint(["item_version_id"], ["item_version.item_version_id"], name="fk_ikp_item_version"),
    )


def _create_publication() -> None:
    """publication 签发表：记录每次发布动作（item_version 的 published 谱系留痕）。

    为什么单独建表：item_version 只增不改，但发布动作的元数据（谁签发、何时、
    用了哪张门证书）需要单独审计；publication 是签发账的入口（与 gate_certificate
    通过 gate_certificate_id 关联，T-W1-006 落地后补 FK）。
    """
    op.create_table(
        "publication",
        sa.Column("publication_id", sa.Text(), primary_key=True),  # ULID
        sa.Column("item_id", sa.Text(), nullable=False),
        sa.Column("item_version_id", sa.Text(), nullable=False),
        sa.Column("gate_certificate_id", sa.Text(), nullable=True),  # FK 待 T-W1-006
        sa.Column("published_by", sa.Text(), nullable=False),  # 签发人 id
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["item_id"], ["item.item_id"], name="fk_pub_item"),
        sa.ForeignKeyConstraint(["item_version_id"], ["item_version.item_version_id"], name="fk_pub_item_version"),
    )


# ────────────────────────────────────────────────────────────────────
# 触发器（契约 §6.3 / §6.4）
# ────────────────────────────────────────────────────────────────────

_TRIGGER_FUNCTIONS_SQL = """
CREATE OR REPLACE FUNCTION fn_item_version_on_publish() RETURNS TRIGGER AS $$
BEGIN
    -- §6.3：item_version INSERT 且 status='published' 时前移 item.current_version_id
    -- 为什么只在 INSERT：item_version 只增不改（D1），不会 UPDATE；触发器仅挂在 INSERT。
    IF NEW.status = 'published' THEN
        UPDATE item SET current_version_id = NEW.item_version_id
        WHERE item_id = NEW.item_id;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""

_TRIGGER_ITEM_VERSION_SQL = """
CREATE TRIGGER trg_item_version_on_publish
    AFTER INSERT ON item_version
    FOR EACH ROW
    EXECUTE FUNCTION fn_item_version_on_publish();
"""


def _create_triggers() -> None:
    """§6.3 current_version_id 前移触发器。"""
    binding = op.get_bind()
    binding.execute(sa.text(_TRIGGER_FUNCTIONS_SQL))
    binding.execute(sa.text(_TRIGGER_ITEM_VERSION_SQL))


def _drop_triggers() -> None:
    binding = op.get_bind()
    binding.execute(sa.text("DROP TRIGGER IF EXISTS trg_item_version_on_publish ON item_version"))
    binding.execute(sa.text("DROP FUNCTION IF EXISTS fn_item_version_on_publish()"))


# ────────────────────────────────────────────────────────────────────
# upgrade / downgrade
# ────────────────────────────────────────────────────────────────────

def upgrade() -> None:
    """建 item 族 9 表 + item_kp + publication + material_license + 触发器。"""
    _create_enums()
    # 先删 0001 的 item 占位表（gate_certificate / response_event 保留）
    _drop_placeholder_item()
    # 按依赖顺序建表
    _create_material_license()
    _create_item_template()
    _create_item_template_version()
    _create_item()
    _create_item_version()
    _create_material()
    _create_material_version()
    _create_corpus_asset()
    _create_corpus_version()
    _create_item_group()
    _create_item_kp()
    _create_publication()
    # 触发器（在所有表建完后）
    _create_triggers()


def downgrade() -> None:
    """回滚：删所有新表 + 触发器 + 枚举，重建 0001 的 item 占位表。

    为什么重建占位：migrate-check 跑 upgrade→downgrade→upgrade，downgrade 必须
    让库回到 0001 的状态，否则再次 upgrade 会在 _drop_placeholder_item 处失败。
    为什么先删循环 FK：item↔item_version 等循环外键会让 DROP TABLE 报
    DependentObjectsStillExist；先 drop_constraint 解环，再按依赖反序 drop_table。
    """
    _drop_triggers()
    # 1. 先删循环外键约束（解除 item↔item_version 等的环）
    op.drop_constraint("fk_item_current_version", "item", type_="foreignkey")
    op.drop_constraint("fk_material_current_version", "material", type_="foreignkey")
    op.drop_constraint("fk_corpus_asset_current_version", "corpus_asset", type_="foreignkey")
    op.drop_constraint("fk_item_template_current_version", "item_template", type_="foreignkey")
    # 2. 反序删表（此时无循环依赖，正常 DROP）
    op.drop_table("publication")
    op.drop_table("item_kp")
    op.drop_table("item_group")
    op.drop_table("corpus_version")
    op.drop_table("corpus_asset")
    op.drop_table("material_version")
    op.drop_table("material")
    op.drop_table("item_version")
    op.drop_table("item")
    op.drop_table("item_template_version")
    op.drop_table("item_template")
    op.drop_table("material_license")
    _drop_enums()
    # 3. 重建 0001 的 item 占位表（与 0001 migration 完全一致）
    op.create_table(
        "item",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
