"""W3 S6 复习排程服务：事件流同步入队 + 到期取题.

- sync_review_queue：读 response_event（只读 SELECT——作答事件账永不被本模块写），
  经 scheduler.rebuild_queue 纯函数重放，幂等 upsert 进 review_queue_entry。
  全量重放而非增量：事件量 v1 极小，且全量重放天然满足「队列版本可重建」
  （R-Z-07）——同一事件流 + 同一策略版本，重放结果与库中状态必然一致。
- get_due_reviews：到期取题接口（S6 验收点），只读。

为什么用裸 SQL 读 response_event：与 src/core/events/writer.py 同理由——
分区表 ORM 映射易踩坑，本处只取 4 个标量列 + 2 个 JSONB 列，裸 SQL 最直接。
"""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID, uuid4

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.review.models import ReviewPolicy, ReviewQueueEntry
from src.core.review.scheduler import (
    DEFAULT_POLICY_ID,
    DEFAULT_POLICY_VERSION,
    ReviewEventView,
    derive_correctness,
    rebuild_queue,
)

_EVENTS_SQL = """
SELECT event_id, item_version_id, created_at, scoring_trace, error_inferences
FROM response_event
WHERE student_alias_id = :student_alias_id
ORDER BY created_at, event_id
"""


async def load_policy_intervals(
    session: AsyncSession,
    policy_id: str = DEFAULT_POLICY_ID,
    policy_version: str = DEFAULT_POLICY_VERSION,
) -> list[int]:
    """加载策略版本的固定间隔表（天）.

    Raises:
        LookupError: 策略版本不存在（调用方应先确认迁移 0010 已执行）。
    """
    policy = await session.get(ReviewPolicy, (policy_id, policy_version))
    if policy is None:
        raise LookupError(
            f"review_policy 不存在: policy_id={policy_id!r} "
            f"policy_version={policy_version!r}"
        )
    return [int(d) for d in policy.intervals_days]


async def sync_review_queue(
    session: AsyncSession,
    *,
    student_alias_id: UUID,
    policy_id: str = DEFAULT_POLICY_ID,
    policy_version: str = DEFAULT_POLICY_VERSION,
) -> int:
    """重放学生作答事件流，幂等同步复习队列（全量重建语义）.

    错题（含错误推断的事件）自动入队；答对推进 stage；走完末间隔出队。
    重复调用结果不变（同一事件流 × 同一策略版本 → 同一状态）。

    Returns:
        本次在队（pending/done）的条目数。
    """
    intervals = await load_policy_intervals(session, policy_id, policy_version)

    rows = (
        await session.execute(
            text(_EVENTS_SQL), {"student_alias_id": student_alias_id}
        )
    ).all()

    events = [
        ReviewEventView(
            event_id=row.event_id,
            item_version_id=row.item_version_id,
            created_at=row.created_at,
            correct=derive_correctness(row.scoring_trace, row.error_inferences),
            error_type_ids=tuple(
                inf["error_type_id"]
                for inf in row.error_inferences
                if isinstance(inf, dict) and "error_type_id" in inf
            ),
        )
        for row in rows
    ]
    states = rebuild_queue(events, intervals)

    existing = {
        entry.item_version_id: entry
        for entry in (
            await session.execute(
                select(ReviewQueueEntry).where(
                    ReviewQueueEntry.student_alias_id == student_alias_id,
                    ReviewQueueEntry.policy_id == policy_id,
                    ReviewQueueEntry.policy_version == policy_version,
                )
            )
        )
        .scalars()
        .all()
    }

    now = datetime.now(timezone.utc)
    for item_version_id, state in states.items():
        entry = existing.get(item_version_id)
        if entry is None:
            session.add(
                ReviewQueueEntry(
                    entry_id=uuid4(),
                    student_alias_id=student_alias_id,
                    item_version_id=item_version_id,
                    policy_id=policy_id,
                    policy_version=policy_version,
                    stage=state.stage,
                    status=state.status,
                    source_error_type_id=state.source_error_type_id,
                    last_event_id=state.last_event_id,
                    enqueued_at=state.enqueued_at,
                    due_at=state.due_at,
                    updated_at=now,
                )
            )
        elif (
            entry.stage != state.stage
            or entry.status != state.status
            or entry.due_at != state.due_at
            or entry.last_event_id != state.last_event_id
            or entry.source_error_type_id != state.source_error_type_id
        ):
            # 派生表允许 UPDATE（非三本账）；仅状态变化时写，保证幂等
            entry.stage = state.stage
            entry.status = state.status
            entry.due_at = state.due_at
            entry.last_event_id = state.last_event_id
            entry.source_error_type_id = state.source_error_type_id
            entry.updated_at = now

    await session.commit()
    return len(states)


async def get_due_reviews(
    session: AsyncSession,
    *,
    student_alias_id: UUID,
    now: datetime,
    policy_id: str = DEFAULT_POLICY_ID,
    policy_version: str = DEFAULT_POLICY_VERSION,
    limit: int = 20,
) -> list[ReviewQueueEntry]:
    """到期取题接口：返回该学生已到期的在队复习条目.

    判定：status='pending' 且 due_at <= now；按 due_at 升序（最逾期优先），
    entry_id 作次序兜底保证确定性。
    """
    result = await session.execute(
        select(ReviewQueueEntry)
        .where(
            ReviewQueueEntry.student_alias_id == student_alias_id,
            ReviewQueueEntry.policy_id == policy_id,
            ReviewQueueEntry.policy_version == policy_version,
            ReviewQueueEntry.status == "pending",
            ReviewQueueEntry.due_at <= now,
        )
        .order_by(ReviewQueueEntry.due_at, ReviewQueueEntry.entry_id)
        .limit(limit)
    )
    return list(result.scalars().all())


__all__ = ["get_due_reviews", "load_policy_intervals", "sync_review_queue"]
