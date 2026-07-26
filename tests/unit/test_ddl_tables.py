"""T-W1-002 item 族 11 表存在性验证.

对照 T-W01-T02 验证卡 §3：9 核心 + item_kp + publication = 11 张表。
gate_certificate / response_event 占位表（0001）由各自任务卡覆盖，不在本卡范围。
"""
from __future__ import annotations

from sqlalchemy import text

# 9 核心表 + item_kp + publication（验收卡 §3）
EXPECTED_TABLES = {
    # 9 核心（§2.1-2.5）
    "item",
    "item_version",
    "item_template",
    "item_template_version",
    "material",
    "material_version",
    "item_group",
    "corpus_asset",
    "corpus_version",
    # 标注/签发辅助
    "item_kp",
    "publication",
    # material_license（§2.4 material_version.license_id FK 依赖，迁移一并建）
    "material_license",
}


async def test_all_item_family_tables_exist(async_session):
    """§3 11 张表（含 material_license 共 12 张）全部存在。"""
    result = await async_session.execute(
        text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
    )
    existing = {row[0] for row in result.fetchall()}
    missing = EXPECTED_TABLES - existing
    assert not missing, f"缺失表: {missing}"


async def test_material_license_decision_enum_values(async_session):
    """§2.4 material_license.decision 枚举含 approved/rejected/expired。"""
    result = await async_session.execute(
        text(
            """
            SELECT e.enumlabel
            FROM pg_type t
            JOIN pg_enum e ON e.enumtypid = t.oid
            WHERE t.typname = 'material_license_decision_enum'
            ORDER BY e.enumsortorder
            """
        )
    )
    values = [row[0] for row in result.fetchall()]
    assert set(values) == {"approved", "rejected", "expired"}, (
        f"material_license_decision_enum 值不符：{values}"
    )


async def test_item_template_version_status_enum_values(async_session):
    """§2.3 item_template_version status 枚举含 draft/published/retired（无 quarantined）。"""
    result = await async_session.execute(
        text(
            """
            SELECT e.enumlabel
            FROM pg_type t
            JOIN pg_enum e ON e.enumtypid = t.oid
            WHERE t.typname = 'item_template_version_status_enum'
            ORDER BY e.enumsortorder
            """
        )
    )
    values = [row[0] for row in result.fetchall()]
    assert set(values) == {"draft", "published", "retired"}, (
        f"item_template_version_status_enum 值不符：{values}"
    )
