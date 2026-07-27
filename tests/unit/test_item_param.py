"""W3 S8：item_param 参数标定表 DDL/ORM 测试（迁移 0010）.

覆盖架构 v2 §4.7 + 宪法 D5/D6：
  §1 表结构：列名/类型与迁移逐字对齐（information_schema 对照）。
  §2 域约束：purpose_scope 三值 / source 先验-实测形态 / sample_size 非负。
  §3 幂等：UNIQUE(item_version_id, purpose_scope, source, method_version, as_of)。
  §4 只增不改：UPDATE/DELETE 被触发器物理拒绝（D1 风格）。
  §5 FK RESTRICT：item_version_id 必须存在。
  §6 ORM：ItemParam 映射可插入并读回。
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.models.item_param import ItemParam

pytestmark = pytest.mark.asyncio


# ────────────────────────────────────────────────────────────────────
# 辅助：准备 FK 依赖（item + item_version）
# ────────────────────────────────────────────────────────────────────


async def _insert_item_version(db: AsyncSession, item_version_id: str) -> None:
    """插入最小 item + item_version（满足 item_param FK）."""
    item_id = f"item-for-{item_version_id[-8:]}"
    await db.execute(
        text("INSERT INTO item (item_id, pack_id, tier) VALUES (:iid, 'platform', 'C')"),
        {"iid": item_id},
    )
    await db.execute(
        text(
            "INSERT INTO item_version (item_version_id, item_id, status, objective,"
            " interaction_ref, content, scoring_ref, error_bindings, lineage)"
            " VALUES (:vid, :iid, 'draft', '{}'::jsonb, '{}'::jsonb, '{}'::jsonb,"
            " '{}'::jsonb, '{}'::jsonb, '{}'::jsonb)"
        ),
        {"vid": item_version_id, "iid": item_id},
    )
    await db.commit()


async def _insert_param(
    db: AsyncSession,
    *,
    param_id: str = "param-001",
    item_version_id: str = "sha256:iv-param-test",
    purpose_scope: str = "practice",
    source: str = "measured_ctt",
    params: str = '{"difficulty": 0.75, "discrimination": 0.4}',
    sample_size: int = 30,
    method_version: str = "ctt-v1",
    as_of: datetime | None = None,
) -> None:
    await db.execute(
        text(
            "INSERT INTO item_param (param_id, item_version_id, purpose_scope,"
            " source, params, sample_size, method_version, as_of)"
            " VALUES (:pid, :vid, :scope, :src, CAST(:params AS jsonb), :n, :mv, :ao)"
        ),
        {
            "pid": param_id, "vid": item_version_id, "scope": purpose_scope,
            "src": source, "params": params, "n": sample_size,
            "mv": method_version,
            "ao": as_of or datetime(2026, 7, 27, tzinfo=timezone.utc),
        },
    )
    await db.commit()


# ────────────────────────────────────────────────────────────────────
# §1 表结构
# ────────────────────────────────────────────────────────────────────


async def test_item_param_table_exists(async_session: AsyncSession):
    """迁移 0010 创建 item_param 表."""
    row = (
        await async_session.execute(
            text(
                "SELECT 1 FROM information_schema.tables"
                " WHERE table_schema='public' AND table_name='item_param'"
            )
        )
    ).first()
    assert row is not None


async def test_item_param_columns_align_with_migration(async_session: AsyncSession):
    """列名与迁移/契约对齐：param_id/item_version_id/purpose_scope/source/
    params/sample_size/method_version/as_of/created_at."""
    rows = (
        await async_session.execute(
            text(
                "SELECT column_name FROM information_schema.columns"
                " WHERE table_schema='public' AND table_name='item_param'"
            )
        )
    ).all()
    cols = {r[0] for r in rows}
    assert cols == {
        "param_id", "item_version_id", "purpose_scope", "source",
        "params", "sample_size", "method_version", "as_of", "created_at",
    }


async def test_item_param_params_column_is_jsonb(async_session: AsyncSession):
    """params 列为 jsonb."""
    row = (
        await async_session.execute(
            text(
                "SELECT data_type FROM information_schema.columns"
                " WHERE table_name='item_param' AND column_name='params'"
            )
        )
    ).one()
    assert row[0] == "jsonb"


# ────────────────────────────────────────────────────────────────────
# §2 域约束（D5）
# ────────────────────────────────────────────────────────────────────


async def test_purpose_scope_domain_enforced(async_session: AsyncSession):
    """purpose_scope 越域（如 'mixed'）被 CHECK 拒绝."""
    await _insert_item_version(async_session, "sha256:iv-scope-domain")
    with pytest.raises(Exception):
        await _insert_param(
            async_session,
            item_version_id="sha256:iv-scope-domain",
            purpose_scope="mixed",
        )
    await async_session.rollback()


async def test_purpose_scope_three_values_accepted(async_session: AsyncSession):
    """practice/diagnosis/measurement 三值均可插入（D5 分场景存储）."""
    await _insert_item_version(async_session, "sha256:iv-scope-ok")
    for i, scope in enumerate(("practice", "diagnosis", "measurement")):
        await _insert_param(
            async_session,
            param_id=f"param-scope-{i}",
            item_version_id="sha256:iv-scope-ok",
            purpose_scope=scope,
        )


async def test_source_domain_enforced(async_session: AsyncSession):
    """source 非法形态（非 prior_rule/prior_expert/measured_*）被拒绝."""
    await _insert_item_version(async_session, "sha256:iv-src-domain")
    with pytest.raises(Exception):
        await _insert_param(
            async_session,
            item_version_id="sha256:iv-src-domain",
            source="guessed",
        )
    await async_session.rollback()


async def test_source_prior_and_measured_accepted(async_session: AsyncSession):
    """prior_rule/prior_expert/measured_ctt 均可插入（先验/实测分离）."""
    await _insert_item_version(async_session, "sha256:iv-src-ok")
    for i, src in enumerate(("prior_rule", "prior_expert", "measured_ctt")):
        await _insert_param(
            async_session,
            param_id=f"param-src-{i}",
            item_version_id="sha256:iv-src-ok",
            source=src,
        )


async def test_sample_size_negative_rejected(async_session: AsyncSession):
    """sample_size < 0 被 CHECK 拒绝."""
    await _insert_item_version(async_session, "sha256:iv-neg-n")
    with pytest.raises(Exception):
        await _insert_param(
            async_session,
            item_version_id="sha256:iv-neg-n",
            sample_size=-1,
        )
    await async_session.rollback()


# ────────────────────────────────────────────────────────────────────
# §3 幂等 UNIQUE
# ────────────────────────────────────────────────────────────────────


async def test_identity_unique_conflict(async_session: AsyncSession):
    """同 (item_version_id, purpose_scope, source, method_version, as_of)
    重复写入冲突（同一次估计运行幂等）."""
    await _insert_item_version(async_session, "sha256:iv-uniq")
    await _insert_param(
        async_session, param_id="param-u1", item_version_id="sha256:iv-uniq"
    )
    with pytest.raises(Exception):
        await _insert_param(
            async_session, param_id="param-u2", item_version_id="sha256:iv-uniq"
        )
    await async_session.rollback()


async def test_same_item_different_scope_coexists(async_session: AsyncSession):
    """同题不同 purpose_scope 各占一行（D5 分场景独立估计的存储形态）."""
    await _insert_item_version(async_session, "sha256:iv-multi-scope")
    for scope in ("practice", "diagnosis"):
        await _insert_param(
            async_session,
            param_id=f"param-ms-{scope}",
            item_version_id="sha256:iv-multi-scope",
            purpose_scope=scope,
        )
    rows = (
        await async_session.execute(
            text(
                "SELECT purpose_scope FROM item_param"
                " WHERE item_version_id='sha256:iv-multi-scope' ORDER BY 1"
            )
        )
    ).all()
    assert [r[0] for r in rows] == ["diagnosis", "practice"]


# ────────────────────────────────────────────────────────────────────
# §4 只增不改（D1 风格触发器）
# ────────────────────────────────────────────────────────────────────


async def test_update_rejected(async_session: AsyncSession):
    """UPDATE item_param 被 append-only 触发器拒绝."""
    await _insert_item_version(async_session, "sha256:iv-append-u")
    await _insert_param(
        async_session, param_id="param-au", item_version_id="sha256:iv-append-u"
    )
    with pytest.raises(Exception):
        await async_session.execute(
            text("UPDATE item_param SET sample_size=99 WHERE param_id='param-au'")
        )
    await async_session.rollback()


async def test_delete_rejected(async_session: AsyncSession):
    """DELETE item_param 被 append-only 触发器拒绝."""
    await _insert_item_version(async_session, "sha256:iv-append-d")
    await _insert_param(
        async_session, param_id="param-ad", item_version_id="sha256:iv-append-d"
    )
    with pytest.raises(Exception):
        await async_session.execute(
            text("DELETE FROM item_param WHERE param_id='param-ad'")
        )
    await async_session.rollback()


# ────────────────────────────────────────────────────────────────────
# §5 FK RESTRICT
# ────────────────────────────────────────────────────────────────────


async def test_fk_item_version_required(async_session: AsyncSession):
    """item_version_id 不存在时 FK 拒绝."""
    with pytest.raises(Exception):
        await _insert_param(
            async_session,
            param_id="param-fk",
            item_version_id="sha256:iv-does-not-exist",
        )
    await async_session.rollback()


# ────────────────────────────────────────────────────────────────────
# §6 ORM
# ────────────────────────────────────────────────────────────────────


async def test_orm_insert_and_readback(async_session: AsyncSession):
    """ItemParam ORM 插入并读回（列映射与迁移对齐）."""
    await _insert_item_version(async_session, "sha256:iv-orm")
    row = ItemParam(
        param_id="param-orm-1",
        item_version_id="sha256:iv-orm",
        purpose_scope="practice",
        source="measured_ctt",
        params={"difficulty": 0.6, "discrimination": 0.35},
        sample_size=42,
        method_version="ctt-v1",
        as_of=datetime(2026, 7, 27, tzinfo=timezone.utc),
    )
    async_session.add(row)
    await async_session.commit()

    got = await async_session.get(ItemParam, "param-orm-1")
    assert got is not None
    assert got.purpose_scope == "practice"
    assert got.source == "measured_ctt"
    assert got.params["difficulty"] == 0.6
    assert got.sample_size == 42
    assert got.method_version == "ctt-v1"
