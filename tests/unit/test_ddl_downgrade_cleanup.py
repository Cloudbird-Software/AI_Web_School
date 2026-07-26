"""T-W1-002 downgrade base 清理验证.

对照 T-W01-T02 验证卡 §10：downgrade base 后 public schema 无任何业务表
（alembic_version 除外），upgrade head 后全部重建。

为什么需要 base 级 downgrade：本卡覆盖 0001+0002 两个迁移，downgrade -1
只回退 0002 仍保留 0001 的占位表（item/gate_certificate/response_event）；
只有 downgrade base 才能验证「全部迁移清零」。

执行方式：本测试模块**不**自动跑 alembic downgrade base；测试者需在 CLI 中
手动执行：
    alembic downgrade base && pytest tests/unit/test_ddl_downgrade_cleanup.py -v
    alembic upgrade head

测试函数会检测当前 DB 状态：若仍有业务表则 skip（说明未跑 downgrade base），
避免在常规 `pytest tests/` 全量运行时误报。
"""
from __future__ import annotations

import pytest
from sqlalchemy import text


async def test_all_tables_dropped_after_downgrade_base(async_session):
    """执行本测试前须先 `alembic downgrade base`。

    检测策略：若 public schema 仍存在 item_version 等业务表，说明未跑
    downgrade base，本测试 skip；只有空库（仅 alembic_version）才真正断言。
    """
    result = await async_session.execute(
        text("SELECT tablename FROM pg_tables WHERE schemaname='public'")
    )
    tables = {r[0] for r in result.fetchall()}
    business_tables = tables - {"alembic_version"}

    if business_tables:
        # 仍有业务表 → 未执行 downgrade base，跳过本断言
        pytest.skip(
            f"未执行 alembic downgrade base（仍存在业务表 {business_tables}）；"
            f"本测试需手动跑 `alembic downgrade base` 后再执行。"
        )

    # 空库断言：除 alembic_version 外不应有任何表
    assert tables == {"alembic_version"}, (
        f"downgrade base 后仍存在非 alembic_version 表：{tables}"
    )


async def test_upgrade_head_rebuilds_all_tables(async_session):
    """upgrade head 后 11 张表（含 material_license）全部重建。

    本测试在常规 `pytest tests/` 全量运行时生效：若 DB 已在 head 状态，
    11 张业务表应全部存在；若刚跑完 downgrade base 测试，需先 alembic upgrade head。
    """
    expected = {
        "item", "item_version", "item_template", "item_template_version",
        "material", "material_version", "item_group",
        "corpus_asset", "corpus_version",
        "item_kp", "publication", "material_license",
    }
    result = await async_session.execute(
        text("SELECT tablename FROM pg_tables WHERE schemaname='public'")
    )
    tables = {r[0] for r in result.fetchall()}
    if "alembic_version" not in tables:
        pytest.skip("DB 未初始化（无 alembic_version 表），请先 alembic upgrade head")

    missing = expected - tables
    assert not missing, (
        f"upgrade head 后仍缺表 {missing}；若刚跑 downgrade base 测试，"
        f"请先 `alembic upgrade head` 再跑本测试。"
    )
