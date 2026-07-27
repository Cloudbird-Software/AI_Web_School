"""W3 S6 复习排程纯函数核（架构 §4.4：纯函数策略接口 + 版本化 → 队列可重建）.

设计要点：
- 全部函数无副作用、无 IO：输入 = 按时间序的作答事件视图 + 固定间隔表，
  输出 = 每题一条的队列状态机。同一事件流 + 同一策略版本重放必得同态，
  这是「队列版本可重建」（R-Z-07 / 架构 §4.4）的实现根基。
- v1 策略 = 固定间隔表 [1, 3, 7, 21] 天（迁移 0010 内置种子
  fixed-interval/1.0.0）；FSRS 等 v2 策略另起 policy_id，不影响本核。

状态机语义（每 学生×题目 一条）：
- 答错（含错误推断的事件）→ 入队或重置：stage=0，due = 事件时刻 + intervals[0]
- 答对（在队 pending）→ 推进：stage+1；越过最后一个间隔 → done（出队）
- 答对但不在队 / 已 done → 忽略（不重新入队——答对不是错题）
- 对错无法判定（correct=None）→ 忽略（评分轨迹缺 correctness 且无任何
  错误推断时，v1 不做猜测性归因——宁可不排程也不伪造证据）

对错判定 derive_correctness：
- scoring_trace.process.correct 为 bool 时优先采用（S4 评分执行联通后
  由评分器显式写入）；
- 否则 error_inferences 非空 ⇒ 答错（选择题选项→错误类型确定映射，§4.5）；
- 否则 None（未知）。
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any, Iterable, Optional
from uuid import UUID

# 迁移 0010 内置的 v1 策略种子（policy_id/policy_version）
DEFAULT_POLICY_ID = "fixed-interval"
DEFAULT_POLICY_VERSION = "1.0.0"

# 队列状态域（与迁移 0010 ck_review_queue_entry_status_domain 对齐）
STATUS_PENDING = "pending"
STATUS_DONE = "done"


@dataclass(frozen=True)
class ReviewEventView:
    """作答事件的排程视图（response_event 的最小投影）.

    correct=None 表示本事件无法判定对错，apply_event 将忽略之。
    error_type_ids 仅用于记录入队/重置时的主要归因（取首个），不参与判定。
    """

    event_id: UUID
    item_version_id: str
    created_at: datetime
    correct: Optional[bool]
    error_type_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class EntryState:
    """单题队列状态（frozen——状态迁移产出新实例，便于纯函数测试）."""

    stage: int
    status: str
    enqueued_at: datetime
    due_at: datetime
    source_error_type_id: Optional[str]
    last_event_id: UUID


def derive_correctness(
    scoring_trace: dict[str, Any],
    error_inferences: list[dict[str, Any]],
) -> Optional[bool]:
    """从契约 §3/§4 结构推导事件对错（None=无法判定）.

    优先级：scoring_trace.process.correct（显式 bool）> 错误推断非空 ⇒ 答错
    > None（未知，不猜）。
    """
    process = scoring_trace.get("process")
    if isinstance(process, dict):
        correct = process.get("correct")
        if isinstance(correct, bool):
            return correct
    if error_inferences:
        return False
    return None


def _due_at(base: datetime, stage: int, intervals_days: list[int]) -> datetime:
    """到期时刻 = 基准时刻 + intervals[stage] 天."""
    return base + timedelta(days=intervals_days[stage])


def apply_event(
    state: Optional[EntryState],
    event: ReviewEventView,
    intervals_days: list[int],
) -> Optional[EntryState]:
    """单事件状态迁移（纯函数）.

    Args:
        state: 当前队列状态；None=该题未在队。
        event: 排程视图事件（调用方保证按 created_at 升序喂入）。
        intervals_days: 策略固定间隔表（天），非空。

    Returns:
        迁移后状态；None 表示仍未在队（答对/未知且原本不在队）。
    """
    if not intervals_days:
        raise ValueError("intervals_days 不能为空（策略间隔表至少一个间隔）")
    if event.correct is None:
        # 无法判定对错：不迁移（不猜）
        return state

    if event.correct is False:
        # 答错：入队或重置回 stage 0（含错误推断的事件自动入队，S6 要求）
        return EntryState(
            stage=0,
            status=STATUS_PENDING,
            enqueued_at=event.created_at if state is None else state.enqueued_at,
            due_at=_due_at(event.created_at, 0, intervals_days),
            source_error_type_id=(
                event.error_type_ids[0] if event.error_type_ids else None
            ),
            last_event_id=event.event_id,
        )

    # 答对：仅在队 pending 时推进；不在队/已 done 忽略
    if state is None or state.status == STATUS_DONE:
        return state
    next_stage = state.stage + 1
    if next_stage >= len(intervals_days):
        # 走完最后一个间隔 → 出队
        return replace(
            state,
            stage=state.stage,
            status=STATUS_DONE,
            last_event_id=event.event_id,
        )
    return replace(
        state,
        stage=next_stage,
        due_at=_due_at(event.created_at, next_stage, intervals_days),
        last_event_id=event.event_id,
    )


def rebuild_queue(
    events: Iterable[ReviewEventView],
    intervals_days: list[int],
) -> dict[str, EntryState]:
    """事件流全量重放 → 每题队列状态（可重建性的权威实现）.

    Args:
        events: 单学生的排程视图事件流；调用方按 (created_at, event_id) 升序
            供给（乱序输入会破坏状态机语义，本函数不重复排序以防掩盖上游 bug）。
        intervals_days: 策略固定间隔表。

    Returns:
        {item_version_id: EntryState}——只含曾在队的题（答对/未知从不会产生条目）。
    """
    states: dict[str, EntryState] = {}
    for event in events:
        new_state = apply_event(
            states.get(event.item_version_id), event, intervals_days
        )
        # apply_event 仅在原不在队且事件为答对/未知时返回 None——不落键
        if new_state is not None:
            states[event.item_version_id] = new_state
    return states
