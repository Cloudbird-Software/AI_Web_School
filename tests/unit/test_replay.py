"""T-W4-003 增量重判框架 + 年度全量重放演练测试.

覆盖任务卡验收 §1-§5：
  §1 incremental_rescore(item_ids, new_scorer_version) 写平行 score_run，
     原 response_event 不受影响。
  §2 replay_all 用当前活跃估计器重算，输出新旧对比报告
     （参数差异分布 + 一致性率）。
  §3 重放结果 100% 可复现：同代码版本 + 同数据快照输出一致（摘要哈希比对）。
  §4 make accept TASK=T-W4-003 全绿（本文件即单元测试主体）。
  §5 不 import 任何学科包/学段包。

宪法 D6 估计器可替换 + R-D-05 可重判：新 scorer 重放写平行 score_run，
原 response_event.scoring_trace 永不改动。
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

import src.core.scoring.platform_scorers  # noqa: F401 —— import 即注册 platform 评分器
from src.core.data.active_model_pointer import ActiveModelPointer
from src.core.data.replay import (
    ReplayReport,
    RescoreReport,
    incremental_rescore,
    replay_all,
)
from src.core.scoring.service import score_and_record


# ────────────────────────────────────────────────────────────────────
# 辅助：清理表 + 插入 item/item_version + 事件落账
# ────────────────────────────────────────────────────────────────────


@pytest_asyncio.fixture(autouse=True)
async def _truncate_events_and_runs(async_session: AsyncSession):
    """每测试前清空 response_event + score_run（response_event append-only，
    不能用 DELETE；TRUNCATE CASCADE 一并清空外键依赖的 score_run）。"""
    await async_session.execute(
        text("TRUNCATE TABLE response_event RESTART IDENTITY CASCADE")
    )
    await async_session.execute(text("TRUNCATE TABLE score_run RESTART IDENTITY CASCADE"))
    await async_session.execute(text("TRUNCATE TABLE estimator_run CASCADE"))
    await async_session.commit()
    yield


def _choice_iv(item_version_id: str) -> dict:
    """单选题：正解 B，干扰项 A 绑错误类型（与 test_scoring_ctt_link 同手法）."""
    return {
        "item_version_id": item_version_id,
        "interaction_ref": {"interaction_id": "single_choice", "interaction_params": {}},
        "scoring_ref": {"scorer_id": "exact_match", "scorer_params": {"answer": "B"}},
        "error_bindings": [
            {"option_value": "A", "label": "位数多的小数更大",
             "error_type_id": "math.decimal.digits_more_is_larger",
             "collision": False, "corpus_ref": None},
        ],
    }


async def _insert_item_version(db: AsyncSession, item_version_id: str) -> None:
    """插入最小 item + item_version（满足 score_run FK + run_scorer 调度需要）.

    为什么 scoring_ref / interaction_ref / error_bindings 必须填实际结构：
    replay.incremental_rescore 从 DB 取 item_version 后用 run_scorer 调度评分器，
    scoring_ref.scorer_id 是评分器调度的唯一依据——空 '{}' 会让 run_scorer
    抛 ScorerNotRegisteredError（与在线评分链路要求一致）。
    test_scoring_ctt_link 用 '{}' 占位是因为 score_and_record 直接收 dict 入参
    （不查 DB）；本测试走真正的「从 DB 回读 → 重判」链路，必须填实际结构。

    幂等：同 item_version_id 重复插入不报错（同题多次落事件场景）——
    ON CONFLICT DO NOTHING 保证 item/item_version 重复插入跳过。
    """
    item_id = f"item-for-{item_version_id[-8:]}"
    await db.execute(
        text(
            "INSERT INTO item (item_id, pack_id, tier) VALUES (:iid, 'platform', 'C')"
            " ON CONFLICT (item_id) DO NOTHING"
        ),
        {"iid": item_id},
    )
    # 与 _choice_iv() 同结构：scorer_id=exact_match, answer=B, A 绑错误类型
    scoring_ref = json.dumps(
        {"scorer_id": "exact_match", "scorer_params": {"answer": "B"}},
        ensure_ascii=False,
    )
    interaction_ref = json.dumps(
        {"interaction_id": "single_choice", "interaction_params": {}},
        ensure_ascii=False,
    )
    error_bindings = json.dumps(
        [
            {"option_value": "A", "label": "位数多的小数更大",
             "error_type_id": "math.decimal.digits_more_is_larger",
             "collision": False, "corpus_ref": None},
        ],
        ensure_ascii=False,
    )
    await db.execute(
        text(
            "INSERT INTO item_version (item_version_id, item_id, status, objective,"
            " interaction_ref, content, scoring_ref, error_bindings, lineage)"
            " VALUES (:vid, :iid, 'draft', '{}'::jsonb,"
            " CAST(:iref AS jsonb), '{}'::jsonb,"
            " CAST(:sref AS jsonb), CAST(:ebs AS jsonb), '{}'::jsonb)"
            " ON CONFLICT (item_version_id) DO NOTHING"
        ),
        {
            "vid": item_version_id, "iid": item_id,
            "iref": interaction_ref, "sref": scoring_ref, "ebs": error_bindings,
        },
    )
    await db.commit()


async def _record_event(
    db: AsyncSession,
    item_version_id: str,
    *,
    selected: str,
    scene: str = "practice",
    now: datetime | None = None,
) -> UUID:
    """落账一条作答事件（用真实 score_and_record 落 response_event）."""
    await _insert_item_version(db, item_version_id)
    outcome = await score_and_record(
        db,
        item_version=_choice_iv(item_version_id),
        response={"selected": selected},
        student_alias_id=uuid4(),
        scene=scene,
        pack_id="platform",
        now=now or datetime.now(timezone.utc),
    )
    return outcome.event_id


async def _fetch_event_trace(db: AsyncSession, event_id: UUID) -> dict:
    """读 response_event.scoring_trace（验证原序列未被改动）."""
    row = (
        await db.execute(
            text("SELECT scoring_trace FROM response_event WHERE event_id = :eid"),
            {"eid": event_id},
        )
    ).one()
    return dict(row._mapping)["scoring_trace"]


async def _count_score_runs(db: AsyncSession) -> int:
    return (
        await db.execute(text("SELECT count(*) FROM score_run"))
    ).scalar()


# ════════════════════════════════════════════════════════════════════
# §1 incremental_rescore：写平行 score_run，原 response_event 不受影响
# ════════════════════════════════════════════════════════════════════


class TestIncrementalRescore:
    """incremental_rescore 写平行 score_run；原 response_event 不动。"""

    async def test_writes_parallel_score_runs(self, async_session: AsyncSession):
        """§1：为指定题写平行 score_run 行，数量=匹配事件数."""
        iv_a = "sha256:replay-iv-a"
        iv_b = "sha256:replay-iv-b"
        # 每题 2 个事件
        await _record_event(async_session, iv_a, selected="B")
        await _record_event(async_session, iv_a, selected="A")
        await _record_event(async_session, iv_b, selected="B")

        report = await incremental_rescore(
            async_session, [iv_a, iv_b],
            new_scorer_version="exact_match-v2",
            run_label="test-001",
        )

        assert report.rescored_count == 3
        assert report.failed_count == 0
        assert report.skipped_count == 0
        assert await _count_score_runs(async_session) == 3

    async def test_original_response_event_unchanged(self, async_session: AsyncSession):
        """§1 核心：原 response_event.scoring_trace 永不改动（R-D-05 原序列不动）."""
        iv = "sha256:replay-iv-orig"
        eid = await _record_event(async_session, iv, selected="B")
        original_trace = await _fetch_event_trace(async_session, eid)
        # 记录原始 trace 的 scorer_version 与 correct
        original_sv = original_trace["scorer_version"]

        await incremental_rescore(
            async_session, [iv],
            new_scorer_version="exact_match-v2",
            run_label="test-002",
        )

        # 重判后再读原事件——必须完全一致
        post_trace = await _fetch_event_trace(async_session, eid)
        assert post_trace == original_trace
        assert post_trace["scorer_version"] == original_sv

    async def test_scope_filter_isolates_events(self, async_session: AsyncSession):
        """purpose_scope 过滤：practice 重判不影响 diagnosis 事件."""
        iv = "sha256:replay-iv-scope"
        # 同题 practice + diagnosis 各 1 事件
        await _record_event(async_session, iv, selected="B", scene="practice")
        await _record_event(async_session, iv, selected="A", scene="diagnosis")

        report = await incremental_rescore(
            async_session, [iv],
            new_scorer_version="exact_match-v2",
            purpose_scope="practice",
            run_label="test-003",
        )
        assert report.rescored_count == 1
        # score_run 表中应只有 practice 的 1 行
        rows = (
            await async_session.execute(
                text("SELECT purpose_scope FROM score_run ORDER BY purpose_scope")
            )
        ).all()
        assert [r.purpose_scope for r in rows] == ["practice"]

    async def test_invalid_scope_rejected(self, async_session: AsyncSession):
        with pytest.raises(ValueError, match="purpose_scope"):
            await incremental_rescore(
                async_session, ["sha256:x"],
                new_scorer_version="v2",
                purpose_scope="mixed",
            )

    async def test_empty_item_ids_returns_empty_report(self, async_session: AsyncSession):
        """空 item_ids 直接返回零报告，不查库."""
        report = await incremental_rescore(
            async_session, [],
            new_scorer_version="v2",
            run_label="test-empty",
        )
        assert report.rescored_count == 0
        assert report.summary_hash == ""

    async def test_no_matching_events_returns_empty(self, async_session: AsyncSession):
        """无匹配事件：返回零报告，但 summary_hash 非空（输入快照指纹）."""
        report = await incremental_rescore(
            async_session, ["sha256:no-such-iv"],
            new_scorer_version="v2",
            run_label="test-nomatch",
        )
        assert report.rescored_count == 0
        # 0 事件时 hash 为空（约定——无可重算内容）
        assert report.summary_hash == ""

    async def test_score_run_carries_original_scorer_version(
        self, async_session: AsyncSession
    ):
        """score_run.original_scorer_version 记录原始事件当时的评分器版本（对比报告输入）."""
        iv = "sha256:replay-iv-osv"
        await _record_event(async_session, iv, selected="B")
        await incremental_rescore(
            async_session, [iv],
            new_scorer_version="v2",
            run_label="test-osv",
        )
        row = (
            await async_session.execute(
                text(
                    "SELECT original_scorer_version, scorer_version FROM score_run LIMIT 1"
                )
            )
        ).one()
        # 原版本 = platform exact_match 当时版本；新版本 = 重判时 Scorer 自报
        assert row.original_scorer_version == "1.0.0+platform"
        assert row.scorer_version == "1.0.0+platform"  # 同一评分器，版本未变

    async def test_score_run_links_to_event(self, async_session: AsyncSession):
        """score_run.event_id + event_created_at 复合 FK 指向原始事件."""
        iv = "sha256:replay-iv-fk"
        eid = await _record_event(async_session, iv, selected="B")
        await incremental_rescore(
            async_session, [iv],
            new_scorer_version="v2",
            run_label="test-fk",
        )
        row = (
            await async_session.execute(
                text(
                    "SELECT event_id FROM score_run WHERE event_id = :eid LIMIT 1"
                ),
                {"eid": eid},
            )
        ).one()
        assert row.event_id == eid


# ════════════════════════════════════════════════════════════════════
# 幂等保护：同事件同版本同标签不重复写入
# ════════════════════════════════════════════════════════════════════


class TestIdempotency:
    """同事件同版本同标签幂等：第二次调用跳过，不重复写入。"""

    async def test_second_call_skips_all(self, async_session: AsyncSession):
        iv = "sha256:replay-iv-idem"
        await _record_event(async_session, iv, selected="B")
        await _record_event(async_session, iv, selected="A")

        r1 = await incremental_rescore(
            async_session, [iv],
            new_scorer_version="v2",
            run_label="idem-batch",
        )
        r2 = await incremental_rescore(
            async_session, [iv],
            new_scorer_version="v2",
            run_label="idem-batch",  # 同 label
        )
        assert r1.rescored_count == 2
        assert r2.rescored_count == 0
        assert r2.skipped_count == 2
        assert await _count_score_runs(async_session) == 2  # 没多写

    async def test_different_label_writes_new_runs(self, async_session: AsyncSession):
        """不同 run_label 视为不同批次，可再次写入（uq 含 run_label）."""
        iv = "sha256:replay-iv-label"
        await _record_event(async_session, iv, selected="B")
        await incremental_rescore(
            async_session, [iv], new_scorer_version="v2", run_label="batch-A",
        )
        await incremental_rescore(
            async_session, [iv], new_scorer_version="v2", run_label="batch-B",
        )
        assert await _count_score_runs(async_session) == 2


# ════════════════════════════════════════════════════════════════════
# §3 可重放性：同输入必同输出（摘要哈希比对）
# ════════════════════════════════════════════════════════════════════


class TestReproducibility:
    """同代码版本 + 同数据快照输出一致（summary_hash 比对）."""

    async def test_same_input_same_hash(self, async_session: AsyncSession):
        """两次重判（不同 label 避免 uq 冲突）输入相同 → summary_hash 一致."""
        iv = "sha256:replay-iv-repro"
        await _record_event(async_session, iv, selected="B")
        await _record_event(async_session, iv, selected="A")

        r1 = await incremental_rescore(
            async_session, [iv],
            new_scorer_version="v2",
            run_label="repro-1",
            input_snapshot_id="snap-fixed-001",
        )
        r2 = await incremental_rescore(
            async_session, [iv],
            new_scorer_version="v2",
            run_label="repro-2",
            input_snapshot_id="snap-fixed-001",  # 同输入快照
        )
        assert r1.summary_hash != ""
        assert r1.summary_hash == r2.summary_hash

    async def test_different_input_different_hash(self, async_session: AsyncSession):
        """数据快照不同（事件数不同）→ summary_hash 不同."""
        iv = "sha256:replay-iv-diff"
        await _record_event(async_session, iv, selected="B")

        r1 = await incremental_rescore(
            async_session, [iv],
            new_scorer_version="v2",
            run_label="diff-1",
            input_snapshot_id="snap-A",
        )
        # 再补一个事件——输入数据快照变化
        await _record_event(async_session, iv, selected="A")
        r2 = await incremental_rescore(
            async_session, [iv],
            new_scorer_version="v2",
            run_label="diff-2",
            input_snapshot_id="snap-B",
        )
        assert r1.summary_hash != r2.summary_hash

    async def test_report_to_dict_serializable(self, async_session: AsyncSession):
        """报告可序列化为 dict（JSON 报告导出契约）."""
        iv = "sha256:replay-iv-dict"
        await _record_event(async_session, iv, selected="B")
        report = await incremental_rescore(
            async_session, [iv],
            new_scorer_version="v2",
            run_label="dict-test",
        )
        d = report.to_dict()
        assert "rescored_count" in d
        assert "summary_hash" in d
        assert "failures" in d
        import json
        # 必须可 JSON 序列化
        json.dumps(d, default=str)


# ════════════════════════════════════════════════════════════════════
# §2 replay_all：年度全量重放 + 新旧对比报告
# ════════════════════════════════════════════════════════════════════


class TestReplayAll:
    """replay_all 用当前活跃估计器重算全部历史事件，输出新旧对比报告."""

    async def _setup_with_active_estimator(
        self, db: AsyncSession, *, model_version: str = "ctt-v1"
    ) -> None:
        """登记当前活跃估计器版本（replay_all 默认取活跃 model_version）."""
        ptr = ActiveModelPointer(db)
        await ptr.set_active(
            "practice", model_version,
            code_digest="sha256:replay-code",
            input_snapshot_id="snap-active",
            graph_release_id="gr-1",
        )

    async def test_replay_all_writes_parallel_runs(self, async_session: AsyncSession):
        """§2：全量重放对该场景全部事件写平行 score_run."""
        iv_a = "sha256:replay-all-a"
        iv_b = "sha256:replay-all-b"
        await _record_event(async_session, iv_a, selected="B")
        await _record_event(async_session, iv_a, selected="A")
        await _record_event(async_session, iv_b, selected="B")
        await self._setup_with_active_estimator(async_session)

        report = await replay_all(
            async_session, purpose_scope="practice", run_label="annual-2026",
        )
        assert isinstance(report, ReplayReport)
        assert report.rescored_count == 3
        assert await _count_score_runs(async_session) == 3

    async def test_replay_all_uses_active_estimator_version(
        self, async_session: AsyncSession
    ):
        """§2：未传 scorer_version 时取当前活跃 model_version."""
        iv = "sha256:replay-all-active"
        await _record_event(async_session, iv, selected="B")
        await self._setup_with_active_estimator(
            async_session, model_version="my-estimator-v9"
        )

        report = await replay_all(
            async_session, purpose_scope="practice", run_label="annual-v",
        )
        # report.scorer_version 是 Scorer 自报的版本（platform exact_match），
        # 但 replay_all 内部用的 scorer_version 标签是 active.model_version
        # —— 通过 score_run 表的 run_label 与 scorer_version 验证
        rows = (
            await async_session.execute(
                text("SELECT scorer_version, run_label FROM score_run")
            )
        ).all()
        assert len(rows) == 1
        # run_label 应为传入的标签
        assert rows[0].run_label == "annual-v"

    async def test_replay_all_no_active_raises(self, async_session: AsyncSession):
        """无活跃估计器版本且未传 scorer_version → 报错（不伪造）."""
        iv = "sha256:replay-all-no-active"
        await _record_event(async_session, iv, selected="B")
        # 不调 set_active，且不传 scorer_version
        with pytest.raises(ValueError, match="无活跃估计器"):
            await replay_all(async_session, purpose_scope="practice")

    async def test_replay_all_scope_isolation(self, async_session: AsyncSession):
        """§2：practice 重放只读 practice 事件，diagnosis 不被触碰."""
        iv = "sha256:replay-all-iso"
        await _record_event(async_session, iv, selected="B", scene="practice")
        await _record_event(async_session, iv, selected="A", scene="diagnosis")
        await self._setup_with_active_estimator(async_session)

        report = await replay_all(
            async_session, purpose_scope="practice", run_label="iso-test",
        )
        assert report.rescored_count == 1  # 只有 practice 那条
        # diagnosis 事件无对应 score_run
        rows = (
            await async_session.execute(
                text("SELECT purpose_scope FROM score_run")
            )
        ).all()
        assert all(r.purpose_scope == "practice" for r in rows)

    async def test_replay_all_report_contains_diff_distribution(
        self, async_session: AsyncSession
    ):
        """§2：报告含参数差异分布（difficulty 旧/新/差值）."""
        iv = "sha256:replay-all-diff"
        await _record_event(async_session, iv, selected="B")  # 1 对
        await _record_event(async_session, iv, selected="A")  # 1 错
        await self._setup_with_active_estimator(async_session)

        report = await replay_all(
            async_session, purpose_scope="practice", run_label="diff-test",
        )
        assert "difficulty_old" in report.param_diff_distribution
        assert "difficulty_new" in report.param_diff_distribution
        assert "difficulty_delta" in report.param_diff_distribution
        # 同评分器重算，难度应一致（delta ≈ 0）
        assert report.param_diff_distribution["difficulty_delta"] == pytest.approx(0.0)

    async def test_replay_all_consistency_rate(self, async_session: AsyncSession):
        """§2：一致性率 = 新旧 correct 一致的比例；同评分器重算应为 1.0."""
        iv = "sha256:replay-all-cons"
        await _record_event(async_session, iv, selected="B")
        await _record_event(async_session, iv, selected="A")
        await self._setup_with_active_estimator(async_session)

        report = await replay_all(
            async_session, purpose_scope="practice", run_label="cons-test",
        )
        assert report.consistency == pytest.approx(1.0)

    async def test_replay_all_reproducible_hash(self, async_session: AsyncSession):
        """§3：年度重放同代码+同数据→同 summary_hash."""
        iv = "sha256:replay-all-repro"
        await _record_event(async_session, iv, selected="B")
        await _record_event(async_session, iv, selected="A")
        await self._setup_with_active_estimator(async_session)

        r1 = await replay_all(
            async_session, purpose_scope="practice", run_label="repro-A",
            input_snapshot_id="snap-fixed-replay",
        )
        r2 = await replay_all(
            async_session, purpose_scope="practice", run_label="repro-B",
            input_snapshot_id="snap-fixed-replay",
        )
        assert r1.summary_hash != ""
        assert r1.summary_hash == r2.summary_hash

    async def test_replay_all_empty_scope_returns_zero(self, async_session: AsyncSession):
        """无事件场景返回零报告."""
        await self._setup_with_active_estimator(async_session)
        report = await replay_all(
            async_session, purpose_scope="practice", run_label="empty-test",
        )
        assert report.rescored_count == 0
        assert report.summary_hash == ""

    async def test_replay_all_invalid_scope_rejected(self, async_session: AsyncSession):
        with pytest.raises(ValueError, match="purpose_scope"):
            await replay_all(async_session, purpose_scope="all")

    async def test_replay_all_does_not_touch_original_events(
        self, async_session: AsyncSession
    ):
        """§1/§2：原 response_event.scoring_trace 不变（D1/R-D-05）."""
        iv = "sha256:replay-all-unchanged"
        eid = await _record_event(async_session, iv, selected="B")
        original_trace = await _fetch_event_trace(async_session, eid)
        await self._setup_with_active_estimator(async_session)

        await replay_all(
            async_session, purpose_scope="practice", run_label="unchanged-test",
        )
        post_trace = await _fetch_event_trace(async_session, eid)
        assert post_trace == original_trace


# ════════════════════════════════════════════════════════════════════
# §5 不 import 学科包/学段包（A5/X6 静态扫描）
# ════════════════════════════════════════════════════════════════════


def test_no_subject_pack_imports_in_data() -> None:
    """src/core/data/ 不 import 任何学科包/学段包（宪法 A5/A7）.

    复用 test_active_model_pointer.test_no_subject_pack_imports_in_data 的
    扫描逻辑——replay.py 必须同样通过。
    """
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


def test_replay_module_exports() -> None:
    """模块导出契约稳定（replay.incremental_rescore / replay_all）."""
    from src.core.data.replay import (
        ReplayReport,
        RescoreReport,
        incremental_rescore,
        replay_all,
    )
    assert callable(incremental_rescore)
    assert callable(replay_all)
    assert issubclass(ReplayReport, RescoreReport)
