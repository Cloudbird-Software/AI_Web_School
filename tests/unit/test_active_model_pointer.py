"""T-W4-002 ActiveModelPointer 估计器版本切换测试.

覆盖任务卡验收 §1-§5：
  §1 set_active 登记活跃版本，旧版本记录退役时间戳。
  §2 get_params 按 timestamp 返回对应版本参数；无 timestamp 返回当前活跃。
  §3 同一样本经 v1/v2 估计后分别存储，查询互不影响；历史报告引用当时版本。
  §4 make accept TASK=T-W4-002 全绿（本文件即单元测试主体）。
  §5 不 import 学科包/学段包（A5/X6 静态扫描）。

宪法 D6 估计器可替换：v1(CTT+经验贝叶斯) / v2(Rasch/2PL) 指针登记与切换；
历史报告永远引用当时版本（按 timestamp 回溯）。
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.data.active_model_pointer import (
    ActiveModelPointer,
    ParamSnapshot,
    VALID_PURPOSE_SCOPES,
)
from src.core.models.estimator_run import EstimatorRun


# ────────────────────────────────────────────────────────────────────
# 辅助：FK 依赖 + item_param 行
# ────────────────────────────────────────────────────────────────────


async def _insert_item_version(db: AsyncSession, item_version_id: str) -> None:
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
    param_id: str,
    item_version_id: str,
    purpose_scope: str,
    method_version: str,
    params: dict,
    sample_size: int,
    as_of: datetime,
    source: str = "measured_ctt",
) -> None:
    import json

    await db.execute(
        text(
            "INSERT INTO item_param (param_id, item_version_id, purpose_scope,"
            " source, params, sample_size, method_version, as_of)"
            " VALUES (:pid, :vid, :scope, :src, CAST(:params AS jsonb), :n, :mv, :ao)"
        ),
        {
            "pid": param_id, "vid": item_version_id, "scope": purpose_scope,
            "src": source,
            "params": json.dumps(params, ensure_ascii=False),
            "n": sample_size, "mv": method_version, "ao": as_of,
        },
    )
    await db.commit()


# ────────────────────────────────────────────────────────────────────
# §1 set_active 登记活跃 + 旧版本退役时间戳
# ────────────────────────────────────────────────────────────────────


class TestSetActive:
    """set_active 登记当前活跃版本，旧版本记录 retired_at。"""

    async def test_first_set_active_has_null_retired_at(
        self, async_session: AsyncSession
    ):
        """首次登记：retired_at 为 NULL（当前活跃）."""
        ptr = ActiveModelPointer(async_session)
        run = await ptr.set_active(
            "practice", "ctt-v1",
            code_digest="sha256:ctt-code", input_snapshot_id="snap-1",
            graph_release_id="gr-1",
        )
        assert run.model_version == "ctt-v1"
        assert run.retired_at is None
        assert run.purpose_scope == "practice"
        assert run.code_digest == "sha256:ctt-code"

    async def test_second_set_active_retires_old(self, async_session: AsyncSession):
        """二次登记：旧版本 retired_at 被打戳，新版本 retired_at 为 NULL."""
        ptr = ActiveModelPointer(async_session)
        t1 = datetime(2026, 7, 1, 10, 0, tzinfo=timezone.utc)
        t2 = datetime(2026, 7, 10, 10, 0, tzinfo=timezone.utc)
        await ptr.set_active(
            "practice", "ctt-v1", code_digest="d1",
            input_snapshot_id="s1", graph_release_id="g1", activated_at=t1,
        )
        await ptr.set_active(
            "practice", "rasch-v1", code_digest="d2",
            input_snapshot_id="s2", graph_release_id="g2", activated_at=t2,
        )
        rows = (
            await async_session.execute(
                text(
                    "SELECT model_version, retired_at FROM estimator_run"
                    " WHERE purpose_scope='practice' ORDER BY activated_at"
                )
            )
        ).all()
        assert [r.model_version for r in rows] == ["ctt-v1", "rasch-v1"]
        # 旧版本退役时间戳 = 新版本激活时刻
        assert rows[0].retired_at is not None
        assert rows[1].retired_at is None

    async def test_only_one_active_per_scope(self, async_session: AsyncSession):
        """偏唯一索引：每场景至多一个 retired_at IS NULL."""
        ptr = ActiveModelPointer(async_session)
        await ptr.set_active(
            "diagnosis", "ctt-v1", code_digest="d1",
            input_snapshot_id="s1", graph_release_id="g1",
        )
        await ptr.set_active(
            "diagnosis", "rasch-v1", code_digest="d2",
            input_snapshot_id="s2", graph_release_id="g2",
        )
        active = (
            await async_session.execute(
                text(
                    "SELECT count(*) FROM estimator_run"
                    " WHERE purpose_scope='diagnosis' AND retired_at IS NULL"
                )
            )
        ).scalar()
        assert active == 1

    async def test_scopes_are_independent(self, async_session: AsyncSession):
        """不同场景的活跃指针互不影响（D5 分场景独立）."""
        ptr = ActiveModelPointer(async_session)
        await ptr.set_active(
            "practice", "ctt-v1", code_digest="d1",
            input_snapshot_id="s1", graph_release_id="g1",
        )
        await ptr.set_active(
            "diagnosis", "rasch-v1", code_digest="d2",
            input_snapshot_id="s2", graph_release_id="g2",
        )
        # practice 活跃仍是 ctt-v1，diagnosis 活跃是 rasch-v1
        p = await ptr.get_active("practice")
        d = await ptr.get_active("diagnosis")
        assert p.model_version == "ctt-v1"
        assert d.model_version == "rasch-v1"

    async def test_invalid_scope_rejected(self, async_session: AsyncSession):
        ptr = ActiveModelPointer(async_session)
        with pytest.raises(ValueError, match="purpose_scope"):
            await ptr.set_active(
                "mixed", "ctt-v1", code_digest="d",
                input_snapshot_id="s", graph_release_id="g",
            )


# ────────────────────────────────────────────────────────────────────
# §2/§3 get_params 当前 / 按 timestamp / v1-v2 分离
# ────────────────────────────────────────────────────────────────────


class TestGetParams:
    """get_params 按版本返回参数；v1/v2 分离；历史报告引用当时版本."""

    async def _setup_v1_v2(
        self, db: AsyncSession
    ) -> tuple[datetime, datetime, datetime]:
        """同题 practice 场景：v1(t1 激活→t2 退役) / v2(t2 激活，当前活跃)."""
        iv = "sha256:iv-amp-v1v2"
        await _insert_item_version(db, iv)
        t1 = datetime(2026, 7, 1, 10, 0, tzinfo=timezone.utc)
        t2 = datetime(2026, 7, 10, 10, 0, tzinfo=timezone.utc)
        # v1 参数行（method_version=ctt-v1，as_of 早于 t1）
        await _insert_param(
            db, param_id="p-v1", item_version_id=iv, purpose_scope="practice",
            method_version="ctt-v1",
            params={"difficulty": 0.5, "discrimination": 0.30},
            sample_size=40, as_of=t1 - timedelta(days=1),
        )
        # v2 参数行（method_version=rasch-v1，as_of 早于 t2）
        await _insert_param(
            db, param_id="p-v2", item_version_id=iv, purpose_scope="practice",
            method_version="rasch-v1",
            params={"difficulty": 0.45, "discrimination": 0.55},
            sample_size=40, as_of=t2 - timedelta(days=1),
        )
        ptr = ActiveModelPointer(db)
        await ptr.set_active(
            "practice", "ctt-v1", code_digest="d1",
            input_snapshot_id="s1", graph_release_id="g1", activated_at=t1,
        )
        await ptr.set_active(
            "practice", "rasch-v1", code_digest="d2",
            input_snapshot_id="s2", graph_release_id="g2", activated_at=t2,
        )
        t_mid = t1 + (t2 - t1) / 2  # v1 仍活跃的中间时刻
        return t1, t_mid, t2

    async def test_get_params_current_returns_v2(self, async_session: AsyncSession):
        """无 timestamp：返回当前活跃版本(v2)的参数."""
        _, _, _ = await self._setup_v1_v2(async_session)
        iv = "sha256:iv-amp-v1v2"
        ptr = ActiveModelPointer(async_session)
        snap = await ptr.get_params(iv, "practice")
        assert snap is not None
        assert snap.model_version == "rasch-v1"
        assert snap.params["difficulty"] == pytest.approx(0.45)
        assert snap.purpose_scope == "practice"

    async def test_get_params_by_timestamp_returns_v1(self, async_session: AsyncSession):
        """§2/§3：timestamp 在 v1 活跃期 → 返回 v1 参数（历史报告引用当时版本）."""
        _, t_mid, _ = await self._setup_v1_v2(async_session)
        iv = "sha256:iv-amp-v1v2"
        ptr = ActiveModelPointer(async_session)
        snap = await ptr.get_params(iv, "practice", timestamp=t_mid)
        assert snap is not None
        assert snap.model_version == "ctt-v1"
        assert snap.params["difficulty"] == pytest.approx(0.5)

    async def test_v1_v2_params_coexist_separately(self, async_session: AsyncSession):
        """§3：同题 v1/v2 参数行各自独立共存于 item_param（method_version 区分）."""
        await self._setup_v1_v2(async_session)
        iv = "sha256:iv-amp-v1v2"
        rows = (
            await async_session.execute(
                text(
                    "SELECT method_version, params->>'difficulty' AS d"
                    " FROM item_param WHERE item_version_id=:iv"
                    " ORDER BY method_version"
                ),
                {"iv": iv},
            )
        ).all()
        assert [r.method_version for r in rows] == ["ctt-v1", "rasch-v1"]
        assert {r.d for r in rows} == {"0.5", "0.45"}

    async def test_get_params_queries_dont_interfere(self, async_session: AsyncSession):
        """§3：切换活跃指针改变返回版本，但两版本参数行互不影响."""
        _, _, _ = await self._setup_v1_v2(async_session)
        iv = "sha256:iv-amp-v1v2"
        ptr = ActiveModelPointer(async_session)
        # 当前活跃 v2
        assert (await ptr.get_params(iv, "practice")).model_version == "rasch-v1"
        # 再切回 v1：登记新 v1 行（旧 v2 退役）
        await ptr.set_active(
            "practice", "ctt-v1", code_digest="d1b",
            input_snapshot_id="s1b", graph_release_id="g1b",
        )
        snap = await ptr.get_params(iv, "practice")
        assert snap.model_version == "ctt-v1"
        assert snap.params["difficulty"] == pytest.approx(0.5)

    async def test_get_params_no_active_returns_none(self, async_session: AsyncSession):
        """无活跃版本 → None（不伪造参数）."""
        iv = "sha256:iv-amp-none"
        await _insert_item_version(async_session, iv)
        await _insert_param(
            async_session, param_id="p-x", item_version_id=iv,
            purpose_scope="measurement", method_version="ctt-v1",
            params={"difficulty": 0.5}, sample_size=10,
            as_of=datetime(2026, 7, 1, tzinfo=timezone.utc),
        )
        ptr = ActiveModelPointer(async_session)
        # measurement 场景从未 set_active → 无活跃版本
        assert await ptr.get_params(iv, "measurement") is None

    async def test_get_params_no_param_row_returns_none(
        self, async_session: AsyncSession
    ):
        """活跃版本存在但该题无对应参数行 → None."""
        ptr = ActiveModelPointer(async_session)
        await ptr.set_active(
            "practice", "ctt-v1", code_digest="d",
            input_snapshot_id="s", graph_release_id="g",
        )
        # 该题从未被 ctt-v1 估计过
        assert await ptr.get_params("sha256:no-such-item", "practice") is None

    async def test_invalid_scope_in_get_params_rejected(
        self, async_session: AsyncSession
    ):
        ptr = ActiveModelPointer(async_session)
        with pytest.raises(ValueError, match="purpose_scope"):
            await ptr.get_params("iv", "all")

    async def test_param_snapshot_carries_model_version(
        self, async_session: AsyncSession
    ):
        """ParamSnapshot 携带 model_version（D6 历史报告引用当时版本实证）."""
        _, _, _ = await self._setup_v1_v2(async_session)
        iv = "sha256:iv-amp-v1v2"
        ptr = ActiveModelPointer(async_session)
        snap = await ptr.get_params(iv, "practice")
        assert isinstance(snap, ParamSnapshot)
        assert snap.item_version_id == iv
        assert snap.sample_size == 40
        assert snap.as_of is not None


# ────────────────────────────────────────────────────────────────────
# §5 不 import 学科包/学段包（A5/X6 静态扫描）
# ────────────────────────────────────────────────────────────────────


def test_no_subject_pack_imports_in_data() -> None:
    """src/core/data/ 不 import 任何学科包/学段包（宪法 A5/A7）."""
    data_dir = (
        Path(__file__).resolve().parent.parent.parent / "src" / "core" / "data"
    )
    assert data_dir.is_dir()
    pattern = re.compile(
        r"^\s*(?:from\s+(?:packs|subject_)|import\s+(?:packs|subject_))",
        re.MULTILINE,
    )
    violations = [
        str(p.relative_to(data_dir))
        for p in sorted(data_dir.rglob("*.py"))
        if pattern.findall(p.read_text(encoding="utf-8"))
    ]
    assert not violations, f"src/core/data/ 学科包 import 违反 A5/A7：{violations}"


def test_valid_purpose_scopes_three_values() -> None:
    assert VALID_PURPOSE_SCOPES == {"practice", "diagnosis", "measurement"}
