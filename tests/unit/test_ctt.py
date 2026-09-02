"""W3 S8：CTT 粗标定批处理测试（数值正确性 / 分场景隔离 / 落库）.

覆盖：
  §1 compute_ctt 纯函数数值正确性（手工可核的小数据集：
     难度=正确率；区分度=修正点二列，与手算 Pearson 一致）。
  §2 零方差 / n<2 时 discrimination=None（信息不足不伪造）。
  §3 run_ctt_calibration 落库：source=measured_ctt、purpose_scope、
     method_version、as_of=输入事件最大 created_at、sample_size 正确。
  §4 分场景禁混估（D5）：同题 practice/diagnosis 双场景数据，
     practice 标定只计 practice 事件；两场景参数行各自独立共存。
  §5 purpose_scope 越域抛 ValueError（无跨场景聚合路径）。
  §6 min_sample 过滤：样本不足的题不产出参数行。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.data.ctt import (
    CTT_METHOD_VERSION,
    CTT_SOURCE,
    ResponseRecord,
    compute_ctt,
    run_ctt_calibration,
)
from src.core.events.writer import record_event


def _month_safe_base() -> datetime:
    """边界安全基准：锚定当月月中，保证减若干小时仍落在当月分区内.

    response_event 按月分区，迁移只建「当月 + 未来 3 月」分区（无历史分区）。
    若用 ``datetime.now() - timedelta(hours=2)`` 作 created_at，在每月 1 日
    头两小时内会落进上月（无分区）→ ``no partition of relation``。锚定到
    当月 15 日 12:00，任何小时级偏移都不跨月，断言只依赖相对先后与
    as_of=max(created_at)，与绝对时间无关。
    """
    now = datetime.now(timezone.utc)
    return datetime(now.year, now.month, 15, 12, 0, 0, tzinfo=timezone.utc)


# ────────────────────────────────────────────────────────────────────
# §1/§2 compute_ctt 纯函数
# ────────────────────────────────────────────────────────────────────


def _hand_dataset() -> list[ResponseRecord]:
    """手工可核数据集：2 题 × 4 学生.

    A: s1=1 s2=1 s3=0 s4=0 → p=0.5
    B: s1=1 s2=0 s3=0 s4=0 → p=0.25
    学生总分：s1=2 s2=1 s3=0 s4=0
    """
    records: list[ResponseRecord] = []
    answers = {
        "A": {"s1": 1.0, "s2": 1.0, "s3": 0.0, "s4": 0.0},
        "B": {"s1": 1.0, "s2": 0.0, "s3": 0.0, "s4": 0.0},
    }
    for item, per_student in answers.items():
        for student, correct in per_student.items():
            records.append(
                ResponseRecord(
                    item_version_id=item,
                    student_alias_id=student,
                    correct=correct,
                )
            )
    return records


class TestComputeCtt:
    """compute_ctt 数值正确性."""

    def test_difficulty_is_correct_rate(self) -> None:
        """difficulty = 正确率（A=0.5，B=0.25）."""
        stats = {s.item_version_id: s for s in compute_ctt(_hand_dataset())}
        assert stats["A"].difficulty == pytest.approx(0.5)
        assert stats["B"].difficulty == pytest.approx(0.25)
        assert stats["A"].sample_size == 4
        assert stats["B"].sample_size == 4

    def test_discrimination_matches_hand_computed_pearson(self) -> None:
        """修正点二列与手算 Pearson 一致.

        A：xs=[1,1,0,0]，修正总分 ys=[1,0,0,0]
           sxy=0.5, sxx=1.0, syy=0.75 → r = 0.5/sqrt(0.75) ≈ 0.57735
        B：xs=[1,0,0,0]，修正总分 ys=[1,1,0,0]
           sxy=0.5, sxx=0.75, syy=1.0 → r = 0.5/sqrt(0.75) ≈ 0.57735
        """
        stats = {s.item_version_id: s for s in compute_ctt(_hand_dataset())}
        assert stats["A"].discrimination == pytest.approx(
            0.5 / (0.75 ** 0.5), rel=1e-9
        )
        assert stats["B"].discrimination == pytest.approx(
            0.5 / (0.75 ** 0.5), rel=1e-9
        )

    def test_zero_variance_gives_none_discrimination(self) -> None:
        """全对（xs 零方差）→ discrimination=None."""
        records = [
            ResponseRecord(item_version_id="C", student_alias_id=f"s{i}", correct=1.0)
            for i in range(4)
        ]
        (stats,) = compute_ctt(records)
        assert stats.difficulty == 1.0
        assert stats.discrimination is None

    def test_single_record_gives_none_discrimination(self) -> None:
        """n=1 → discrimination=None（Pearson 至少需 2 点）."""
        records = [
            ResponseRecord(item_version_id="D", student_alias_id="s1", correct=1.0)
        ]
        (stats,) = compute_ctt(records)
        assert stats.sample_size == 1
        assert stats.discrimination is None

    def test_results_sorted_by_item_version_id(self) -> None:
        """输出按 item_version_id 排序（确定性，D6 可重放）."""
        records = [
            ResponseRecord(item_version_id="z-item", student_alias_id="s1", correct=1.0),
            ResponseRecord(item_version_id="a-item", student_alias_id="s1", correct=0.0),
        ]
        stats = compute_ctt(records)
        assert [s.item_version_id for s in stats] == ["a-item", "z-item"]


# ────────────────────────────────────────────────────────────────────
# DB 集成：落库 / 分场景 / 越域 / min_sample
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


async def _answer(
    db: AsyncSession,
    *,
    item_version_id: str,
    student_alias_id,
    scene: str,
    correct: int,
    created_at: datetime | None = None,
) -> None:
    """写入一条作答事件（scoring_trace 含 dimension_scores.correct）."""
    await record_event(
        db,
        event_id=uuid4(),
        student_alias_id=student_alias_id,
        item_version_id=item_version_id,
        scene=scene,
        raw_payload={"selected": "A"},
        scoring_trace={
            "scorer_id": "exact_match",
            "scorer_version": "1.0.0+test",
            "dimension_scores": {"correct": correct},
            "process": {},
            "confidence": {"scoring": 1.0},
        },
        error_inferences=[],
        created_at=created_at or datetime.now(timezone.utc),
    )


class TestRunCttCalibration:
    """run_ctt_calibration 落库与 D5 分场景."""

    async def test_writes_item_param_rows(self, async_session: AsyncSession):
        """§3：CTT 行正确落库（source/scope/method/sample_size/params）."""
        iv = "sha256:iv-ctt-basic"
        await _insert_item_version(async_session, iv)
        # 4 学生：3 对 1 错 → p=0.75
        students = [uuid4() for _ in range(4)]
        for i, s in enumerate(students):
            await _answer(
                async_session, item_version_id=iv, student_alias_id=s,
                scene="practice", correct=0 if i == 0 else 1,
            )

        written = await run_ctt_calibration(
            async_session, purpose_scope="practice"
        )
        assert len(written) == 1
        row = written[0]
        assert row.item_version_id == iv
        assert row.purpose_scope == "practice"
        assert row.source == CTT_SOURCE
        assert row.method_version == CTT_METHOD_VERSION
        assert row.sample_size == 4
        assert row.params["difficulty"] == pytest.approx(0.75)

    async def test_as_of_is_max_event_created_at(self, async_session: AsyncSession):
        """as_of = 输入事件最大 created_at（输入快照右端）."""
        iv = "sha256:iv-ctt-asof"
        await _insert_item_version(async_session, iv)
        base = _month_safe_base()
        t1 = base - timedelta(hours=2)
        t2 = base - timedelta(hours=1)
        await _answer(
            async_session, item_version_id=iv, student_alias_id=uuid4(),
            scene="practice", correct=1, created_at=t1,
        )
        await _answer(
            async_session, item_version_id=iv, student_alias_id=uuid4(),
            scene="practice", correct=0, created_at=t2,
        )
        written = await run_ctt_calibration(
            async_session, purpose_scope="practice"
        )
        assert len(written) == 1
        got = written[0].as_of
        # 时区归一后比较（DB 回读 timestamptz）
        assert got.replace(tzinfo=timezone.utc) == t2

    async def test_scene_isolation_no_cross_estimation(
        self, async_session: AsyncSession
    ):
        """§4（D5）：practice 标定只计 practice 事件，diagnosis 不混入."""
        iv = "sha256:iv-ctt-isolate"
        await _insert_item_version(async_session, iv)
        # practice：2 对 2 错 → p=0.5
        for correct in (1, 1, 0, 0):
            await _answer(
                async_session, item_version_id=iv, student_alias_id=uuid4(),
                scene="practice", correct=correct,
            )
        # diagnosis：8 全对 → 若混入会把 p 抬高到 10/12≈0.833
        for _ in range(8):
            await _answer(
                async_session, item_version_id=iv, student_alias_id=uuid4(),
                scene="diagnosis", correct=1,
            )

        written = await run_ctt_calibration(
            async_session, purpose_scope="practice"
        )
        assert len(written) == 1
        assert written[0].sample_size == 4  # 只计 practice 的 4 条
        assert written[0].params["difficulty"] == pytest.approx(0.5)

    async def test_two_scenes_produce_independent_rows(
        self, async_session: AsyncSession
    ):
        """§4（D5）：同题两场景分别标定，参数行独立共存（各自 as_of 区分）."""
        iv = "sha256:iv-ctt-two-scope"
        await _insert_item_version(async_session, iv)
        now = _month_safe_base()
        await _answer(
            async_session, item_version_id=iv, student_alias_id=uuid4(),
            scene="practice", correct=1, created_at=now - timedelta(hours=2),
        )
        await _answer(
            async_session, item_version_id=iv, student_alias_id=uuid4(),
            scene="diagnosis", correct=0, created_at=now - timedelta(hours=1),
        )

        p_rows = await run_ctt_calibration(async_session, purpose_scope="practice")
        d_rows = await run_ctt_calibration(async_session, purpose_scope="diagnosis")
        assert len(p_rows) == 1 and len(d_rows) == 1
        assert p_rows[0].params["difficulty"] == 1.0
        assert d_rows[0].params["difficulty"] == 0.0
        assert p_rows[0].purpose_scope == "practice"
        assert d_rows[0].purpose_scope == "diagnosis"

    async def test_invalid_scope_rejected(self, async_session: AsyncSession):
        """§5：purpose_scope 越域抛 ValueError（混估入口不存在）."""
        with pytest.raises(ValueError, match="purpose_scope"):
            await run_ctt_calibration(async_session, purpose_scope="mixed")
        with pytest.raises(ValueError, match="purpose_scope"):
            await run_ctt_calibration(async_session, purpose_scope="all")

    async def test_min_sample_filters(self, async_session: AsyncSession):
        """§6：n < min_sample 的题不产出参数行."""
        iv = "sha256:iv-ctt-minsample"
        await _insert_item_version(async_session, iv)
        for _ in range(2):
            await _answer(
                async_session, item_version_id=iv, student_alias_id=uuid4(),
                scene="practice", correct=1,
            )
        written = await run_ctt_calibration(
            async_session, purpose_scope="practice", min_sample=3
        )
        assert written == []

    async def test_no_events_returns_empty(self, async_session: AsyncSession):
        """无符合条件事件 → 空列表，不写库."""
        written = await run_ctt_calibration(
            async_session, purpose_scope="measurement"
        )
        assert written == []

    async def test_events_without_dimension_scores_skipped(
        self, async_session: AsyncSession
    ):
        """缺 dimension_scores.correct 的事件不参与估计（不计入 sample_size）."""
        iv = "sha256:iv-ctt-nodim"
        await _insert_item_version(async_session, iv)
        # 一条无 dimension_scores 的事件（纸卷回录占位等）
        await record_event(
            async_session,
            event_id=uuid4(),
            student_alias_id=uuid4(),
            item_version_id=iv,
            scene="practice",
            raw_payload={"selected": "A"},
            scoring_trace={"scorer_id": "exact_match", "process": {}},
            error_inferences=[],
            created_at=datetime.now(timezone.utc),
        )
        written = await run_ctt_calibration(
            async_session, purpose_scope="practice"
        )
        assert written == []
