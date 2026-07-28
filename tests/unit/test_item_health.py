"""T-W4-004 题目健康度模型 + 生命周期状态机测试.

覆盖任务卡验收 §1-§5：
  §1 evaluate_health(item_id) 返回健康度评分与异常标签（≥4 类异常）。
  §2 状态机转换规则：ACTIVE↔WATCH 自动；WATCH→QUARANTINED / 任何→RETIRED 需门证书。
  §3 退役题目保留历史版本，活跃池排除 RETIRED。
  §4 make accept TASK=T-W4-004 全绿；迁移 alembic upgrade head 成功。
  §5 不 import 任何学科包/学段包。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.data.health import (
    ANOMALY_PENALTY,
    GATE_CERT_REQUIRED_STATES,
    HealthReport,
    LifecycleTransitionError,
    evaluate_health,
    get_current_state,
    query_active_pool_item_ids,
    transition_lifecycle,
)
from src.core.models.item_lifecycle import (
    ACTIVE_POOL_STATES,
    ItemLifecycleState,
    ItemLifecycleTransition,
)


# ────────────────────────────────────────────────────────────────────
# 辅助：清表 + 插入 item/item_version/response_event/item_param
# ────────────────────────────────────────────────────────────────────


@pytest_asyncio.fixture(autouse=True)
async def _truncate_tables(async_session: AsyncSession):
    """每测试前清空相关表（lifecycle 是 append-only，TRUNCATE CASCADE 清空）."""
    await async_session.execute(
        text("TRUNCATE TABLE item_lifecycle_transition RESTART IDENTITY CASCADE")
    )
    await async_session.execute(text("TRUNCATE TABLE response_event RESTART IDENTITY CASCADE"))
    await async_session.execute(text("TRUNCATE TABLE score_run RESTART IDENTITY CASCADE"))
    await async_session.execute(text("TRUNCATE TABLE item_param CASCADE"))
    await async_session.execute(text("TRUNCATE TABLE item_version CASCADE"))
    await async_session.execute(text("TRUNCATE TABLE item CASCADE"))
    await async_session.commit()
    yield


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


async def _insert_item(db: AsyncSession, item_id: str, pack_id: str = "platform") -> None:
    """插入 item 身份行."""
    await db.execute(
        text(
            "INSERT INTO item (item_id, pack_id, tier) VALUES (:iid, :pid, 'C')"
        ),
        {"iid": item_id, "pid": pack_id},
    )
    await db.commit()


async def _insert_item_version(
    db: AsyncSession,
    *,
    item_version_id: str,
    item_id: str,
    answer: str = "B",
    distractors: list[dict] | None = None,
    status: str = "published",
    rendered: bool = True,
) -> None:
    """插入 item_version（含 scoring_ref + error_bindings 供干扰项分析）."""
    if distractors is None:
        distractors = [
            {"option_value": "A", "label": "位数多的小数更大",
             "error_type_id": "math.decimal.digits_more_is_larger",
             "collision": False, "corpus_ref": None},
        ]
    scoring_ref = {"scorer_id": "exact_match", "scorer_params": {"answer": answer}}
    interaction_ref = {"interaction_id": "single_choice", "interaction_params": {}}
    await db.execute(
        text(
            "INSERT INTO item_version (item_version_id, item_id, status, objective,"
            " interaction_ref, content, scoring_ref, error_bindings, lineage,"
            " rendered_snapshot, gate_certificate_id, published_at)"
            " VALUES (:vid, :iid, CAST(:status AS item_version_status_enum), '{}'::jsonb,"
            " CAST(:iref AS jsonb), '{}'::jsonb, CAST(:sref AS jsonb),"
            " CAST(:ebs AS jsonb), '{}'::jsonb,"
            " CASE WHEN :rendered THEN '{}'::jsonb ELSE NULL END,"
            " CASE WHEN :status = 'published' THEN 'gate-test' ELSE NULL END,"
            " CASE WHEN :status = 'published' THEN now() ELSE NULL END)"
            " ON CONFLICT (item_version_id) DO NOTHING"
        ),
        {
            "vid": item_version_id, "iid": item_id, "status": status,
            "iref": json.dumps(interaction_ref, ensure_ascii=False),
            "sref": json.dumps(scoring_ref, ensure_ascii=False),
            "ebs": json.dumps(distractors, ensure_ascii=False),
            "rendered": rendered,
        },
    )
    await db.commit()


async def _insert_event(
    db: AsyncSession,
    *,
    item_version_id: str,
    selected: str = "B",
    correct: float = 1.0,
    duration_ms: int | None = 5000,
    scene: str = "practice",
    now: datetime | None = None,
) -> str:
    """直插 response_event（含 scoring_trace.dimension_scores.correct）."""
    eid = str(uuid4())
    await db.execute(
        text(
            "INSERT INTO response_event ("
            "  event_id, student_alias_id, item_version_id, scene, raw_payload,"
            "  duration_ms, scoring_trace, error_inferences, created_at"
            ") VALUES ("
            "  :eid, :sid, :ivid, CAST(:scene AS response_event_scene_enum),"
            "  CAST(:raw AS jsonb), :dur, CAST(:trace AS jsonb),"
            "  CAST(:infs AS jsonb), :cat)"
        ),
        {
            "eid": eid,
            "sid": uuid4(),
            "ivid": item_version_id,
            "scene": scene,
            "raw": json.dumps({"selected": selected}, ensure_ascii=False),
            "dur": duration_ms,
            "trace": json.dumps(
                {
                    "scorer_id": "exact_match",
                    "scorer_version": "1.0.0+platform",
                    "dimension_scores": {"correct": correct},
                    "process": {"correct": bool(correct >= 1.0)},
                    "confidence": {"scoring": 1.0, "recognition": 0.0},
                },
                ensure_ascii=False,
            ),
            "infs": json.dumps([], ensure_ascii=False),
            "cat": now or _now(),
        },
    )
    await db.commit()
    return eid


async def _insert_item_param(
    db: AsyncSession,
    *,
    item_version_id: str,
    discrimination: float | None,
    difficulty: float = 0.5,
    purpose_scope: str = "practice",
    source: str = "measured_ctt",
) -> None:
    """插入 item_param 行（区分度来源）."""
    params = {"difficulty": difficulty, "discrimination": discrimination}
    await db.execute(
        text(
            "INSERT INTO item_param (param_id, item_version_id, purpose_scope, source,"
            " params, sample_size, method_version, as_of)"
            " VALUES (:pid, :vid, :ps, :src, CAST(:p AS jsonb), 100, 'ctt-v1', now())"
        ),
        {
            "pid": f"param-{uuid4().hex[:8]}",
            "vid": item_version_id,
            "ps": purpose_scope,
            "src": source,
            "p": json.dumps(params, ensure_ascii=False),
        },
    )
    await db.commit()


# ────────────────────────────────────────────────────────────────────
# §1 evaluate_health：健康度评分 + 4 类异常
# ────────────────────────────────────────────────────────────────────


class TestEvaluateHealth:
    """§1 健康度评估返回评分与异常标签列表."""

    async def test_returns_score_and_metrics(self, async_session):
        """健康报告含 score / sample_size / anomalies / metrics."""
        item_id = "item-health-basic"
        iv_id = "sha256:health-iv-basic"
        await _insert_item(async_session, item_id)
        await _insert_item_version(async_session, item_version_id=iv_id, item_id=item_id)
        for _ in range(35):
            await _insert_event(async_session, item_version_id=iv_id, correct=1.0)

        report = await evaluate_health(async_session, item_id)

        assert report.item_id == item_id
        assert report.sample_size == 35
        assert 0.0 <= report.health_score <= 1.0
        assert "correct_rate" in report.metrics
        assert report.metrics["purpose_scope"] == "practice"

    async def test_insufficient_sample_flagged(self, async_session):
        """n < 30 → insufficient_sample=True，不判定异常."""
        item_id = "item-health-small"
        iv_id = "sha256:health-iv-small"
        await _insert_item(async_session, item_id)
        await _insert_item_version(async_session, item_version_id=iv_id, item_id=item_id)
        for _ in range(10):
            await _insert_event(async_session, item_version_id=iv_id, correct=1.0)

        report = await evaluate_health(async_session, item_id)

        assert report.sample_size == 10
        assert report.insufficient_sample is True
        assert report.anomalies == []
        # 样本不足不扣分（信息不足不伪造坏，也不伪造好）
        assert report.health_score == 1.0

    async def test_anomaly_correct_rate_too_high(self, async_session):
        """异常 1：正确率 > 0.95 → correct_rate_too_high."""
        item_id = "item-health-easy"
        iv_id = "sha256:health-iv-easy"
        await _insert_item(async_session, item_id)
        await _insert_item_version(async_session, item_version_id=iv_id, item_id=item_id)
        # 34 对 1 错 → 正确率 0.97；错的那条选干扰项 A（避免 no_distractor 误报）
        for _ in range(34):
            await _insert_event(async_session, item_version_id=iv_id, correct=1.0, selected="B")
        await _insert_event(async_session, item_version_id=iv_id, correct=0.0, selected="A")

        report = await evaluate_health(async_session, item_id)

        assert "correct_rate_too_high" in report.anomalies
        assert report.metrics["correct_rate"] > 0.95
        # 仅 1 个异常（正确率过高），干扰项 A 被选 1 次不触发 no_distractor
        assert "no_distractor_selected" not in report.anomalies
        assert report.health_score == 1.0 - ANOMALY_PENALTY * 1

    async def test_anomaly_correct_rate_too_low(self, async_session):
        """异常 1：正确率 < 0.05 → correct_rate_too_low."""
        item_id = "item-health-hard"
        iv_id = "sha256:health-iv-hard"
        await _insert_item(async_session, item_id)
        await _insert_item_version(async_session, item_version_id=iv_id, item_id=item_id)
        # 34 错 1 对 → 正确率 0.029
        for _ in range(34):
            await _insert_event(async_session, item_version_id=iv_id, correct=0.0, selected="A")
        await _insert_event(async_session, item_version_id=iv_id, correct=1.0, selected="B")

        report = await evaluate_health(async_session, item_id)

        assert "correct_rate_too_low" in report.anomalies
        assert report.metrics["correct_rate"] < 0.05

    async def test_anomaly_low_discrimination(self, async_session):
        """异常 2：区分度 < 0.2 → low_discrimination."""
        item_id = "item-health-disc"
        iv_id = "sha256:health-iv-disc"
        await _insert_item(async_session, item_id)
        await _insert_item_version(async_session, item_version_id=iv_id, item_id=item_id)
        # 中等正确率 0.5（不触发正确率异常），区分度低
        for i in range(35):
            correct = 1.0 if i % 2 == 0 else 0.0
            await _insert_event(async_session, item_version_id=iv_id, correct=correct)
        await _insert_item_param(async_session, item_version_id=iv_id, discrimination=0.1)

        report = await evaluate_health(async_session, item_id)

        assert "low_discrimination" in report.anomalies
        assert report.metrics["discrimination"] == 0.1

    async def test_anomaly_no_distractor_selected(self, async_session):
        """异常 3：干扰项无人选 → no_distractor_selected."""
        item_id = "item-health-nodist"
        iv_id = "sha256:health-iv-nodist"
        await _insert_item(async_session, item_id)
        # 干扰项 A/C，正解 B
        distractors = [
            {"option_value": "A", "label": "err-A",
             "error_type_id": "err.a", "collision": False, "corpus_ref": None},
            {"option_value": "C", "label": "err-C",
             "error_type_id": "err.c", "collision": False, "corpus_ref": None},
        ]
        await _insert_item_version(
            async_session, item_version_id=iv_id, item_id=item_id,
            answer="B", distractors=distractors,
        )
        # 35 条全选 B（正解），A/C 无人选
        for _ in range(35):
            await _insert_event(async_session, item_version_id=iv_id, correct=1.0, selected="B")

        report = await evaluate_health(async_session, item_id)

        assert "no_distractor_selected" in report.anomalies
        rates = report.metrics["distractor_rates"]
        assert rates["A"] == 0.0
        assert rates["C"] == 0.0

    async def test_anomaly_time_too_fast(self, async_session):
        """异常 4：中位耗时 < 2s → time_too_fast（猜题/秒杀）."""
        item_id = "item-health-fast"
        iv_id = "sha256:health-iv-fast"
        await _insert_item(async_session, item_id)
        await _insert_item_version(async_session, item_version_id=iv_id, item_id=item_id)
        # 正确率 0.5（不触发正确率异常），耗时全 1s
        for i in range(35):
            correct = 1.0 if i % 2 == 0 else 0.0
            await _insert_event(
                async_session, item_version_id=iv_id, correct=correct, duration_ms=1000
            )

        report = await evaluate_health(async_session, item_id)

        assert "time_too_fast" in report.anomalies
        assert report.metrics["duration_median_ms"] < 2000

    async def test_anomaly_time_too_slow(self, async_session):
        """异常 4：中位耗时 > 30s → time_too_slow（困惑/卡题）."""
        item_id = "item-health-slow"
        iv_id = "sha256:health-iv-slow"
        await _insert_item(async_session, item_id)
        await _insert_item_version(async_session, item_version_id=iv_id, item_id=item_id)
        for i in range(35):
            correct = 1.0 if i % 2 == 0 else 0.0
            await _insert_event(
                async_session, item_version_id=iv_id, correct=correct, duration_ms=35000
            )

        report = await evaluate_health(async_session, item_id)

        assert "time_too_slow" in report.anomalies

    async def test_healthy_item_no_anomalies(self, async_session):
        """健康题（中等正确率/区分度/耗时）→ 无异常，score=1.0."""
        item_id = "item-health-ok"
        iv_id = "sha256:health-iv-ok"
        await _insert_item(async_session, item_id)
        await _insert_item_version(async_session, item_version_id=iv_id, item_id=item_id)
        # 正确率 0.5：对的选 B（正解），错的选 A（干扰项，避免 no_distractor 误报）
        for i in range(35):
            if i % 2 == 0:
                await _insert_event(
                    async_session, item_version_id=iv_id, correct=1.0,
                    selected="B", duration_ms=5000,
                )
            else:
                await _insert_event(
                    async_session, item_version_id=iv_id, correct=0.0,
                    selected="A", duration_ms=5000,
                )
        await _insert_item_param(async_session, item_version_id=iv_id, discrimination=0.5)

        report = await evaluate_health(async_session, item_id)

        assert report.anomalies == []
        assert report.health_score == 1.0

    async def test_no_item_version_returns_empty(self, async_session):
        """无 item_version 的题目 → 空报告."""
        item_id = "item-health-empty"
        await _insert_item(async_session, item_id)

        report = await evaluate_health(async_session, item_id)

        assert report.sample_size == 0
        assert report.insufficient_sample is True


# ────────────────────────────────────────────────────────────────────
# §2 状态机转换规则
# ────────────────────────────────────────────────────────────────────


class TestStateMachine:
    """§2 状态机转换规则正确."""

    async def test_initial_to_active_no_gate_cert(self, async_session):
        """首次进入 ACTIVE 无需门证书."""
        item_id = "item-lc-init"
        await _insert_item(async_session, item_id)

        t = await transition_lifecycle(async_session, item_id, "ACTIVE")

        assert t.from_state is None
        assert t.to_state == "ACTIVE"
        assert t.gate_certificate_id is None

    async def test_active_to_watch_auto(self, async_session):
        """ACTIVE→WATCH 自动转换（无需门证书）."""
        item_id = "item-lc-aw"
        await _insert_item(async_session, item_id)
        await transition_lifecycle(async_session, item_id, "ACTIVE")

        t = await transition_lifecycle(async_session, item_id, "WATCH")

        assert t.from_state == "ACTIVE"
        assert t.to_state == "WATCH"
        assert t.gate_certificate_id is None

    async def test_watch_to_active_auto(self, async_session):
        """WATCH→ACTIVE 自动恢复（无需门证书）."""
        item_id = "item-lc-wa"
        await _insert_item(async_session, item_id)
        await transition_lifecycle(async_session, item_id, "ACTIVE")
        await transition_lifecycle(async_session, item_id, "WATCH")

        t = await transition_lifecycle(async_session, item_id, "ACTIVE")

        assert t.from_state == "WATCH"
        assert t.to_state == "ACTIVE"

    async def test_watch_to_quarantined_requires_gate_cert(self, async_session):
        """§2 WATCH→QUARANTINED 需门证书；缺证书抛错."""
        item_id = "item-lc-wq"
        await _insert_item(async_session, item_id)
        await transition_lifecycle(async_session, item_id, "ACTIVE")
        await transition_lifecycle(async_session, item_id, "WATCH")

        with pytest.raises(LifecycleTransitionError, match="门证书"):
            await transition_lifecycle(async_session, item_id, "QUARANTINED")

        # 带门证书成功
        t = await transition_lifecycle(
            async_session, item_id, "QUARANTINED",
            gate_certificate_id="gate-cert-quarantine",
        )
        assert t.to_state == "QUARANTINED"
        assert t.gate_certificate_id == "gate-cert-quarantine"

    async def test_any_to_retired_requires_gate_cert(self, async_session):
        """§2 任何→RETIRED 需门证书."""
        item_id = "item-lc-ret"
        await _insert_item(async_session, item_id)
        await transition_lifecycle(async_session, item_id, "ACTIVE")

        with pytest.raises(LifecycleTransitionError, match="门证书"):
            await transition_lifecycle(async_session, item_id, "RETIRED")

        t = await transition_lifecycle(
            async_session, item_id, "RETIRED",
            gate_certificate_id="gate-cert-retire",
        )
        assert t.to_state == "RETIRED"

    async def test_retired_is_terminal(self, async_session):
        """§2 RETIRED 为终态，无任何回边."""
        item_id = "item-lc-term"
        await _insert_item(async_session, item_id)
        await transition_lifecycle(async_session, item_id, "ACTIVE")
        await transition_lifecycle(
            async_session, item_id, "RETIRED", gate_certificate_id="g1"
        )

        with pytest.raises(LifecycleTransitionError, match="终态"):
            await transition_lifecycle(async_session, item_id, "ACTIVE")
        with pytest.raises(LifecycleTransitionError, match="终态"):
            await transition_lifecycle(async_session, item_id, "WATCH")

    async def test_invalid_transition_raises(self, async_session):
        """非法转换（如 ACTIVE→QUARANTINED 跳过 WATCH）抛错."""
        item_id = "item-lc-invalid"
        await _insert_item(async_session, item_id)
        await transition_lifecycle(async_session, item_id, "ACTIVE")

        with pytest.raises(LifecycleTransitionError, match="非法转换"):
            await transition_lifecycle(
                async_session, item_id, "QUARANTINED",
                gate_certificate_id="g1",  # 有门证书也不行——跳过 WATCH
            )

    async def test_invalid_to_state_value(self, async_session):
        """非法 to_state 值抛 ValueError."""
        item_id = "item-lc-badstate"
        await _insert_item(async_session, item_id)
        with pytest.raises(ValueError, match="非法 to_state"):
            await transition_lifecycle(async_session, item_id, "INVALID_STATE")

    async def test_get_current_state_none_if_uninitialized(self, async_session):
        """未初始化的 item 当前状态为 None."""
        item_id = "item-lc-none"
        await _insert_item(async_session, item_id)

        state = await get_current_state(async_session, item_id)

        assert state is None

    async def test_get_current_state_after_transitions(self, async_session):
        """当前状态 = 最新 transition 的 to_state."""
        item_id = "item-lc-current"
        await _insert_item(async_session, item_id)
        await transition_lifecycle(async_session, item_id, "ACTIVE")
        await transition_lifecycle(async_session, item_id, "WATCH")

        state = await get_current_state(async_session, item_id)

        assert state == ItemLifecycleState.WATCH

    async def test_health_report_snapshot_in_transition(self, async_session):
        """health_report 快照写入 transition（health_score + anomaly_tags）."""
        item_id = "item-lc-snap"
        await _insert_item(async_session, item_id)
        report = HealthReport(
            item_id=item_id,
            sample_size=50,
            health_score=0.6,
            anomalies=["correct_rate_too_high", "low_discrimination"],
            metrics={},
        )

        t = await transition_lifecycle(
            async_session, item_id, "ACTIVE", health_report=report
        )

        assert t.health_score is not None
        assert float(t.health_score) == 0.6
        assert t.anomaly_tags == ["correct_rate_too_high", "low_discrimination"]


# ────────────────────────────────────────────────────────────────────
# §3 退役保留历史版本 + 活跃池排除 RETIRED
# ────────────────────────────────────────────────────────────────────


class TestRetiredAndActivePool:
    """§3 退役题目保留历史版本；活跃池排除 RETIRED/QUARANTINED."""

    async def test_active_pool_excludes_retired(self, async_session):
        """§3 活跃池排除 RETIRED."""
        # item1 ACTIVE，item2 WATCH，item3 QUARANTINED，item4 RETIRED
        for iid, final in [
            ("item-pool-1", "ACTIVE"),
            ("item-pool-2", "WATCH"),
            ("item-pool-3", "QUARANTINED"),
            ("item-pool-4", "RETIRED"),
        ]:
            await _insert_item(async_session, iid)
            await transition_lifecycle(async_session, iid, "ACTIVE")
            if final == "WATCH":
                await transition_lifecycle(async_session, iid, "WATCH")
            elif final == "QUARANTINED":
                await transition_lifecycle(async_session, iid, "WATCH")
                await transition_lifecycle(
                    async_session, iid, "QUARANTINED", gate_certificate_id="g"
                )
            elif final == "RETIRED":
                await transition_lifecycle(
                    async_session, iid, "RETIRED", gate_certificate_id="g"
                )

        pool = await query_active_pool_item_ids(async_session)

        assert "item-pool-1" in pool
        assert "item-pool-2" in pool
        assert "item-pool-3" not in pool  # QUARANTINED 不在活跃池
        assert "item-pool-4" not in pool  # RETIRED 不在活跃池

    async def test_retired_item_versions_preserved(self, async_session):
        """§3 退役题目历史版本仍可查询（D1 版本账只增不改）."""
        item_id = "item-retired-preserve"
        iv_id = "sha256:retired-iv"
        await _insert_item(async_session, item_id)
        await _insert_item_version(async_session, item_version_id=iv_id, item_id=item_id)
        await transition_lifecycle(async_session, item_id, "ACTIVE")
        await transition_lifecycle(
            async_session, item_id, "RETIRED", gate_certificate_id="g"
        )

        # item_version 行仍在（退役不删除）
        row = (
            await async_session.execute(
                text("SELECT item_id, status FROM item_version WHERE item_version_id = :vid"),
                {"vid": iv_id},
            )
        ).first()
        assert row is not None
        assert row[0] == item_id

        # 活跃池不含该 item
        pool = await query_active_pool_item_ids(async_session)
        assert item_id not in pool

    async def test_active_pool_states_config(self):
        """活跃池状态集合 = {ACTIVE, WATCH}（排除 QUARANTINED/RETIRED）."""
        assert "ACTIVE" in ACTIVE_POOL_STATES
        assert "WATCH" in ACTIVE_POOL_STATES
        assert "QUARANTINED" not in ACTIVE_POOL_STATES
        assert "RETIRED" not in ACTIVE_POOL_STATES

    async def test_gate_cert_required_states(self):
        """GATE_CERT_REQUIRED_STATES = {QUARANTINED, RETIRED}."""
        assert "QUARANTINED" in GATE_CERT_REQUIRED_STATES
        assert "RETIRED" in GATE_CERT_REQUIRED_STATES
        assert "ACTIVE" not in GATE_CERT_REQUIRED_STATES
        assert "WATCH" not in GATE_CERT_REQUIRED_STATES


# ────────────────────────────────────────────────────────────────────
# §4 迁移成功 + §5 不 import 学科包
# ────────────────────────────────────────────────────────────────────


class TestMigrationAndNoSubjectPack:
    """§4 迁移可执行；§5 不 import 学科包/学段包."""

    async def test_migration_table_exists(self, async_session):
        """§4 item_lifecycle_transition 表存在（迁移已执行）."""
        row = (
            await async_session.execute(
                text(
                    "SELECT 1 FROM information_schema.tables "
                    "WHERE table_schema = 'public' "
                    "AND table_name = 'item_lifecycle_transition'"
                )
            )
        ).scalar()
        assert row == 1

    async def test_append_only_trigger_enforced(self, async_session):
        """§4 append-only 触发器物理强制 UPDATE/DELETE（D1）."""
        item_id = "item-lc-trigger"
        await _insert_item(async_session, item_id)
        await transition_lifecycle(async_session, item_id, "ACTIVE")

        # UPDATE 应被触发器拒绝
        with pytest.raises(Exception, match="append-only"):
            await async_session.execute(
                text("UPDATE item_lifecycle_transition SET to_state='RETIRED' "
                     "WHERE item_id = :iid"),
                {"iid": item_id},
            )

    def test_health_module_no_subject_pack_imports(self):
        """§5 health.py 不 import src.packs / src.gradeband."""
        import src.core.data.health as mod
        source = Path(mod.__file__).read_text(encoding="utf-8")
        assert "src.packs" not in source
        assert "src.gradeband" not in source
        assert "from src.packs" not in source

    def test_item_lifecycle_model_no_subject_pack_imports(self):
        """§5 item_lifecycle.py 不 import src.packs / src.gradeband."""
        import src.core.models.item_lifecycle as mod
        source = Path(mod.__file__).read_text(encoding="utf-8")
        assert "src.packs" not in source
        assert "src.gradeband" not in source
