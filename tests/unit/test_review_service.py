"""W3 S6 复习排程服务单元测试（DB）.

覆盖：
- 迁移 0010 内置策略种子可加载（1/3/7/21 天）
- 错题（显式 correct=false / 仅含错误推断）自动入队
- 答对推进 stage 直至 done 出队
- get_due_reviews 到期判定（未到期不返回 / 到期返回 / done 不返回）
- 幂等性：重复 sync 结果不变
- 队列版本可重建：库中状态 == scheduler.rebuild_queue 纯函数重放结果
- review_policy 只增触发器物理拒绝 UPDATE
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.events.writer import record_event
from src.core.review.models import ReviewQueueEntry
from src.core.review.scheduler import (
    ReviewEventView,
    derive_correctness,
    rebuild_queue,
)
from src.core.review.service import (
    get_due_reviews,
    load_policy_intervals,
    sync_review_queue,
)

T0 = datetime.now(timezone.utc).replace(microsecond=0)


@pytest_asyncio.fixture(autouse=True)
async def _truncate_tables(async_session: AsyncSession):
    """每测试前清事件账与队列（TRUNCATE 不触发 append-only 触发器）."""
    await async_session.execute(
        text("TRUNCATE TABLE response_event RESTART IDENTITY CASCADE")
    )
    await async_session.execute(
        text("TRUNCATE TABLE review_queue_entry RESTART IDENTITY CASCADE")
    )
    await async_session.commit()
    yield


def _trace(correct: bool | None) -> dict:
    process = {} if correct is None else {"correct": correct}
    return {
        "scorer_id": "exact_match",
        "scorer_version": "1.0.0+sha256:test",
        "process": process,
        "confidence": {"scoring": 1.0},
    }


def _inferences(error_type_id: str = "math.et.a") -> list[dict]:
    return [
        {
            "error_type_id": error_type_id,
            "confidence": 0.85,
            "rule_version": "1.2.0",
            "evidence": {"selected_option": "B"},
        }
    ]


async def _record(
    session: AsyncSession,
    student: UUID,
    item: str,
    *,
    correct: bool | None,
    at: datetime,
    error_type: str | None = None,
) -> UUID:
    return await record_event(
        session,
        event_id=uuid4(),
        student_alias_id=student,
        item_version_id=item,
        scene="practice",
        raw_payload={"selected": "B"},
        scoring_trace=_trace(correct),
        error_inferences=_inferences(error_type) if error_type else [],
        created_at=at,
    )


# ────────────────────────────────────────────────────────────────────
# 策略种子
# ────────────────────────────────────────────────────────────────────


async def test_policy_seed_loadable(async_session: AsyncSession) -> None:
    """迁移 0010 内置 fixed-interval/1.0.0 = [1, 3, 7, 21] 天."""
    assert await load_policy_intervals(async_session) == [1, 3, 7, 21]


async def test_policy_append_only_trigger(async_session: AsyncSession) -> None:
    """review_policy 只增：UPDATE 被 DB 触发器物理拒绝."""
    with pytest.raises(Exception, match="append-only"):
        await async_session.execute(
            text(
                "UPDATE review_policy SET intervals_days = '[1]'::jsonb "
                "WHERE policy_id = 'fixed-interval'"
            )
        )
    await async_session.rollback()


# ────────────────────────────────────────────────────────────────────
# 入队 / 推进 / 出队
# ────────────────────────────────────────────────────────────────────


async def test_wrong_event_enqueues(async_session: AsyncSession) -> None:
    """显式答错事件自动入队：stage=0，due=事件时刻+1天，记录归因."""
    student = uuid4()
    await _record(
        async_session, student, "iv-1", correct=False, at=T0, error_type="math.et.a"
    )
    count = await sync_review_queue(async_session, student_alias_id=student)
    assert count == 1

    entry = (
        await async_session.execute(select(ReviewQueueEntry))
    ).scalars().one()
    assert entry.stage == 0
    assert entry.status == "pending"
    assert entry.due_at == T0 + timedelta(days=1)
    assert entry.source_error_type_id == "math.et.a"


async def test_inference_only_event_enqueues(async_session: AsyncSession) -> None:
    """无显式 correct 但含错误推断的事件同样自动入队（S6 要求）."""
    student = uuid4()
    await _record(
        async_session, student, "iv-1", correct=None, at=T0, error_type="math.et.a"
    )
    count = await sync_review_queue(async_session, student_alias_id=student)
    assert count == 1


async def test_correct_events_advance_until_done(async_session: AsyncSession) -> None:
    """答对依次推进 1/3/7/21，第 4 次答对出队."""
    student = uuid4()
    await _record(async_session, student, "iv-1", correct=False, at=T0)
    t = T0
    for interval in (1, 3, 7, 21):
        t = t + timedelta(days=interval)
        await _record(async_session, student, "iv-1", correct=True, at=t)

    count = await sync_review_queue(async_session, student_alias_id=student)
    assert count == 1
    entry = (
        await async_session.execute(select(ReviewQueueEntry))
    ).scalars().one()
    assert entry.status == "done"


# ────────────────────────────────────────────────────────────────────
# get_due_reviews 到期判定
# ────────────────────────────────────────────────────────────────────


async def test_get_due_reviews_respects_due_at(async_session: AsyncSession) -> None:
    """未到期不返回；到期返回；done 不返回."""
    student = uuid4()
    await _record(async_session, student, "iv-1", correct=False, at=T0)
    await sync_review_queue(async_session, student_alias_id=student)

    # 未到期（+12h < +1d）
    assert (
        await get_due_reviews(
            async_session, student_alias_id=student, now=T0 + timedelta(hours=12)
        )
        == []
    )
    # 到期（+1d1h）
    due = await get_due_reviews(
        async_session, student_alias_id=student, now=T0 + timedelta(days=1, hours=1)
    )
    assert [e.item_version_id for e in due] == ["iv-1"]

    # 走完出队后不再返回
    t = T0
    for interval in (1, 3, 7, 21):
        t = t + timedelta(days=interval)
        await _record(async_session, student, "iv-1", correct=True, at=t)
    await sync_review_queue(async_session, student_alias_id=student)
    assert (
        await get_due_reviews(
            async_session, student_alias_id=student, now=t + timedelta(days=365)
        )
        == []
    )


async def test_get_due_reviews_isolated_per_student(
    async_session: AsyncSession,
) -> None:
    """到期取题按学生隔离（禁止跨用户混查）."""
    s1, s2 = uuid4(), uuid4()
    await _record(async_session, s1, "iv-1", correct=False, at=T0)
    await _record(async_session, s2, "iv-2", correct=False, at=T0)
    await sync_review_queue(async_session, student_alias_id=s1)
    await sync_review_queue(async_session, student_alias_id=s2)

    due1 = await get_due_reviews(
        async_session, student_alias_id=s1, now=T0 + timedelta(days=2)
    )
    assert [e.item_version_id for e in due1] == ["iv-1"]


# ────────────────────────────────────────────────────────────────────
# 幂等 + 可重建
# ────────────────────────────────────────────────────────────────────


async def test_sync_idempotent(async_session: AsyncSession) -> None:
    """重复 sync：条目数与内容不变（同一事件流 × 同一策略版本 → 同态）."""
    student = uuid4()
    await _record(
        async_session, student, "iv-1", correct=False, at=T0, error_type="math.et.a"
    )
    await sync_review_queue(async_session, student_alias_id=student)
    first = (
        (await async_session.execute(select(ReviewQueueEntry))).scalars().all()
    )
    await sync_review_queue(async_session, student_alias_id=student)
    second = (
        (await async_session.execute(select(ReviewQueueEntry))).scalars().all()
    )
    assert len(first) == len(second) == 1
    assert first[0].entry_id == second[0].entry_id
    assert first[0].due_at == second[0].due_at
    assert first[0].stage == second[0].stage


async def test_db_state_equals_pure_rebuild(async_session: AsyncSession) -> None:
    """队列版本可重建：库中状态 == scheduler.rebuild_queue 纯函数重放."""
    student = uuid4()
    await _record(
        async_session, student, "iv-1", correct=False, at=T0, error_type="math.et.a"
    )
    await _record(
        async_session,
        student,
        "iv-1",
        correct=True,
        at=T0 + timedelta(days=1),
    )
    await _record(
        async_session,
        student,
        "iv-2",
        correct=False,
        at=T0 + timedelta(hours=2),
        error_type="math.et.b",
    )
    await sync_review_queue(async_session, student_alias_id=student)

    # 独立纯函数重放同一事件流
    rows = (
        await async_session.execute(
            text(
                "SELECT event_id, item_version_id, created_at, scoring_trace, "
                "error_inferences FROM response_event "
                "WHERE student_alias_id = :s ORDER BY created_at, event_id"
            ),
            {"s": student},
        )
    ).all()
    expected = rebuild_queue(
        [
            ReviewEventView(
                event_id=r.event_id,
                item_version_id=r.item_version_id,
                created_at=r.created_at,
                correct=derive_correctness(r.scoring_trace, r.error_inferences),
                error_type_ids=tuple(
                    i["error_type_id"] for i in r.error_inferences
                ),
            )
            for r in rows
        ],
        [1, 3, 7, 21],
    )

    entries = {
        e.item_version_id: e
        for e in (await async_session.execute(select(ReviewQueueEntry)))
        .scalars()
        .all()
    }
    assert set(entries) == set(expected)
    for item_id, state in expected.items():
        entry = entries[item_id]
        assert entry.stage == state.stage
        assert entry.status == state.status
        assert entry.due_at == state.due_at
        assert entry.source_error_type_id == state.source_error_type_id
        assert entry.last_event_id == state.last_event_id


# ────────────────────────────────────────────────────────────────────
# API：GET /review/due/{student_alias_id}
# ────────────────────────────────────────────────────────────────────


async def test_api_due_reviews_200(async_session: AsyncSession) -> None:
    """到期取题 API：未到期空列表；带 now 参数到期返回条目结构."""
    from typing import AsyncIterator

    from httpx import ASGITransport, AsyncClient

    from src.api.deps import get_async_session
    from src.api.main import create_app

    student = uuid4()
    await _record(
        async_session, student, "iv-1", correct=False, at=T0, error_type="math.et.a"
    )
    await sync_review_queue(async_session, student_alias_id=student)

    app = create_app()

    async def _override_session() -> AsyncIterator[AsyncSession]:
        yield async_session

    app.dependency_overrides[get_async_session] = _override_session
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            # 未到期：默认 now=当前时刻（T0 后数秒），+1 天才到期 → 空
            resp = await client.get(f"/review/due/{student}")
            assert resp.status_code == 200
            assert resp.json() == []

            # 显式 now = T0 + 2 天 → 到期返回
            due_now = (T0 + timedelta(days=2)).isoformat()
            resp = await client.get(
                f"/review/due/{student}", params={"now": due_now}
            )
            assert resp.status_code == 200
            body = resp.json()
            assert len(body) == 1
            entry = body[0]
            assert entry["item_version_id"] == "iv-1"
            assert entry["student_alias_id"] == str(student)
            assert entry["policy_id"] == "fixed-interval"
            assert entry["policy_version"] == "1.0.0"
            assert entry["stage"] == 0
            assert entry["status"] == "pending"
            assert entry["source_error_type_id"] == "math.et.a"
    finally:
        app.dependency_overrides.clear()
