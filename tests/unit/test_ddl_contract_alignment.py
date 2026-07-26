"""T-W1-002 DDL 与契约逐项对照测试.

对照 specs/contracts/db/item-model.md §2.1 / §2.2 / §2.4 三个核心表的所有
非 JSONB 列名+类型，确保迁移脚本与契约字段一致。

为什么只对照非 JSONB 列：JSONB 列（objective/interaction_ref/content/
scoring_ref/error_bindings/lineage）在 PG 信息schema 中统一为 JSONB，
类型对照信息量低；列名存在性由 §3 表存在性测试覆盖。非 JSONB 列承载
状态机/外键/时间戳，类型错配会直接破坏契约语义。
"""
from __future__ import annotations

from sqlalchemy import text

# ────────────────────────────────────────────────────────────────────
# 期望列定义（契约 §2.1 / §2.2 / §2.4 非JSONB 列）
# (列名, 数据类型关键字串, not_null: bool)
# ────────────────────────────────────────────────────────────────────

# item 表（§2.1）：item_id / pack_id / tier / template_version_id /
#                 current_version_id / created_at
ITEM_COLUMNS = {
    "item_id": ("text", True),
    "pack_id": ("text", True),
    "tier": ("item_tier_enum", True),  # USER-DEFINED enum
    "template_version_id": ("text", False),
    "current_version_id": ("text", False),
    "created_at": ("timestamp with time zone", True),
}

# item_version 表（§2.2）：状态机字段 + 外键 + 时间戳
# JSONB 六大块由 test_item_version_six_blocks_exist 覆盖列名存在性
ITEM_VERSION_COLUMNS = {
    "item_version_id": ("text", True),
    "item_id": ("text", True),
    "status": ("item_version_status_enum", True),
    "rendered_snapshot": ("jsonb", False),
    "gate_certificate_id": ("text", False),
    "published_at": ("timestamp with time zone", False),
    "retired_at": ("timestamp with time zone", False),
    "created_at": ("timestamp with time zone", True),
}

# material_version 表（§2.4）：身份 + 许可 + 状态机 + 时间戳
MATERIAL_VERSION_COLUMNS = {
    "material_version_id": ("text", True),
    "material_id": ("text", True),
    "content_ref": ("text", True),
    "license_id": ("text", True),
    "status": ("item_version_status_enum", True),
    "lineage": ("jsonb", True),
    "gate_certificate_id": ("text", False),
    "published_at": ("timestamp with time zone", False),
    "retired_at": ("timestamp with time zone", False),
    "created_at": ("timestamp with time zone", True),
}


# 六大块字段名（§1/§2.2）
SIX_BLOCKS = {
    "objective",
    "interaction_ref",
    "content",
    "scoring_ref",
    "error_bindings",
    "lineage",
}


async def _fetch_columns(async_session, table_name: str) -> dict[str, tuple[str, bool]]:
    """从 information_schema 拉取列名/类型/可空性。

    为什么用 information_schema.columns 而非 pg_attribute：前者是 SQL 标准，
    字段语义直观（is_nullable='NO' 对应 NOT NULL）；data_type 对内置类型返回
    'integer'/'text'/'timestamp with time zone'，对 enum 等 USER-DEFINED 类型
    返回 'USER-DEFINED'，需配合 udts_schema/udt_name 拼出真实类型名。
    """
    result = await async_session.execute(
        text(
            """
            SELECT
                column_name,
                data_type,
                udt_name,
                is_nullable
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = :tbl
            """
        ),
        {"tbl": table_name},
    )
    columns: dict[str, tuple[str, bool]] = {}
    for col_name, data_type, udt_name, is_nullable in result.fetchall():
        # enum 类型：data_type='USER-DEFINED'，真实类型名在 udt_name
        if data_type == "USER-DEFINED" and udt_name:
            type_str = udt_name
        else:
            type_str = data_type
        not_null = is_nullable == "NO"
        columns[col_name] = (type_str, not_null)
    return columns


async def test_item_table_columns(async_session):
    """§2.1 item 表所有非 JSONB 列名+类型+可空性与契约一致。"""
    actual = await _fetch_columns(async_session, "item")
    for col_name, (expected_type, expected_not_null) in ITEM_COLUMNS.items():
        assert col_name in actual, f"item 表缺列 {col_name}"
        actual_type, actual_not_null = actual[col_name]
        assert actual_type == expected_type, (
            f"item.{col_name} 类型不符：期望 {expected_type}，实际 {actual_type}"
        )
        assert actual_not_null == expected_not_null, (
            f"item.{col_name} NOT NULL 不符：期望 {expected_not_null}，实际 {actual_not_null}"
        )


async def test_item_version_table_columns(async_session):
    """§2.2 item_version 表所有非 JSONB 列名+类型+可空性与契约一致。

    JSONB 六大块字段（objective/interaction_ref/content/scoring_ref/
    error_bindings/lineage）由 test_item_version_six_blocks_exist 验证列名存在性。
    """
    actual = await _fetch_columns(async_session, "item_version")
    for col_name, (expected_type, expected_not_null) in ITEM_VERSION_COLUMNS.items():
        assert col_name in actual, f"item_version 表缺列 {col_name}"
        actual_type, actual_not_null = actual[col_name]
        assert actual_type == expected_type, (
            f"item_version.{col_name} 类型不符：期望 {expected_type}，实际 {actual_type}"
        )
        assert actual_not_null == expected_not_null, (
            f"item_version.{col_name} NOT NULL 不符："
            f"期望 {expected_not_null}，实际 {actual_not_null}"
        )


async def test_material_version_table_columns(async_session):
    """§2.4 material_version 表所有非 JSONB 列名+类型+可空性与契约一致。"""
    actual = await _fetch_columns(async_session, "material_version")
    for col_name, (expected_type, expected_not_null) in MATERIAL_VERSION_COLUMNS.items():
        assert col_name in actual, f"material_version 表缺列 {col_name}"
        actual_type, actual_not_null = actual[col_name]
        assert actual_type == expected_type, (
            f"material_version.{col_name} 类型不符："
            f"期望 {expected_type}，实际 {actual_type}"
        )
        assert actual_not_null == expected_not_null, (
            f"material_version.{col_name} NOT NULL 不符："
            f"期望 {expected_not_null}，实际 {actual_not_null}"
        )


async def test_item_version_six_blocks_exist(async_session):
    """§1/§2.2 item_version 必含六大块 JSONB 字段。"""
    actual = await _fetch_columns(async_session, "item_version")
    missing = SIX_BLOCKS - set(actual.keys())
    assert not missing, f"item_version 缺六大块字段: {missing}"
    # 六大块均应为 jsonb 且 NOT NULL（契约 §2.2 表格）
    for block in SIX_BLOCKS:
        type_str, not_null = actual[block]
        assert type_str == "jsonb", f"item_version.{block} 应为 jsonb，实际 {type_str}"
        assert not_null, f"item_version.{block} 应为 NOT NULL"


async def test_item_tier_enum_values(async_session):
    """§2.1 tier 枚举含 4 个值：A/B/C/D（A7 生产线对等）。"""
    result = await async_session.execute(
        text(
            """
            SELECT e.enumlabel
            FROM pg_type t
            JOIN pg_enum e ON e.enumtypid = t.oid
            WHERE t.typname = 'item_tier_enum'
            ORDER BY e.enumsortorder
            """
        )
    )
    values = [row[0] for row in result.fetchall()]
    assert values == ["A", "B", "C", "D"], f"item_tier_enum 值不符：{values}"


async def test_item_version_status_enum_values(async_session):
    """§2.2/§4 status 枚举含 4 个值：draft/quarantined/published/retired。"""
    result = await async_session.execute(
        text(
            """
            SELECT e.enumlabel
            FROM pg_type t
            JOIN pg_enum e ON e.enumtypid = t.oid
            WHERE t.typname = 'item_version_status_enum'
            ORDER BY e.enumsortorder
            """
        )
    )
    values = [row[0] for row in result.fetchall()]
    assert values == ["draft", "quarantined", "published", "retired"], (
        f"item_version_status_enum 值不符：{values}"
    )


async def test_material_kind_enum_values(async_session):
    """§2.4 kind 枚举含 4 个值：passage/image/table/audio。"""
    result = await async_session.execute(
        text(
            """
            SELECT e.enumlabel
            FROM pg_type t
            JOIN pg_enum e ON e.enumtypid = t.oid
            WHERE t.typname = 'material_kind_enum'
            ORDER BY e.enumsortorder
            """
        )
    )
    values = [row[0] for row in result.fetchall()]
    assert values == ["passage", "image", "table", "audio"], (
        f"material_kind_enum 值不符：{values}"
    )


async def test_published_requires_gate_cert_check_constraint(async_session):
    """§4 规则 1 / §6.4：published_at 非空必伴随 gate_certificate_id 非空（CHECK）。"""
    result = await async_session.execute(
        text(
            """
            SELECT conname
            FROM pg_constraint
            WHERE contype = 'c'
              AND conname = 'ck_iv_published_requires_gate_cert'
            """
        )
    )
    assert result.fetchone() is not None, (
        "缺 ck_iv_published_requires_gate_cert CHECK 约束"
    )


async def test_circular_foreign_keys_deferrable(async_session):
    """§6.1 循环外键 item↔item_version 等必须 DEFERRABLE。

    为什么必须 DEFERRABLE：item.current_version_id → item_version.item_version_id，
    而 item_version.item_id → item.item_id 形成循环；非 DEFERRABLE 约束在
    单事务内插入互引行时会失败。DEFERRABLE INITIALLY DEFERRED 让约束在
    COMMIT 时才检查，允许事务内先插 item_version 再回填 item.current_version_id。
    """
    result = await async_session.execute(
        text(
            """
            SELECT conname, condeferrable, condeferred
            FROM pg_constraint
            WHERE conname IN (
                'fk_item_current_version',
                'fk_material_current_version',
                'fk_corpus_asset_current_version',
                'fk_item_template_current_version'
            )
            ORDER BY conname
            """
        )
    )
    rows = result.fetchall()
    assert len(rows) == 4, f"应有 4 个循环外键约束，实际 {len(rows)}"
    for conname, deferrable, deferred in rows:
        assert deferrable, f"{conname} 应为 DEFERRABLE"
        assert deferred, f"{conname} 应为 INITIALLY DEFERRED"
