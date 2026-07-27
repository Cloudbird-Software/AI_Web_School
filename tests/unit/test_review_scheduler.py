"""W3 S6 复习排程纯函数核单元测试（无 DB）.

覆盖：
- derive_correctness：显式 correct 优先 / 错误推断非空 ⇒ 答错 / 无法判定 ⇒ None
- apply_event：入队 / 推进 / 出队 / 重置 / 忽略路径
- rebuild_queue：多题事件流重放 + 可重建确定性（同输入必同输出）
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from src.core.review.scheduler import (
    STATUS_DONE,
    STATUS_PENDING,
    ReviewEventView,
    apply_event,
    derive_correctness,
    rebuild_queue,
)

INTERVALS = [1, 3, 7, 21]
T0 = datetime(2026, 7, 27, 8, 0, tzinfo=timezone.utc)


def _ev(
    item: str,
    correct: bool | None,
    at: datetime = T0,
    error_types: tuple[str, ...] = (),
) -> ReviewEventView:
    return ReviewEventView(
        event_id=uuid4(),
        item_version_id=item,
        created_at=at,
        correct=correct,
        error_type_ids=error_types,
    )


def _trace(process: dict | None = None) -> dict:
    return {
        "scorer_id": "exact_match",
        "scorer_version": "1.0.0",
        "process": process or {},
        "confidence": {"scoring": 1.0},
    }


# ────────────────────────────────────────────────────────────────────
# derive_correctness
# ────────────────────────────────────────────────────────────────────


def test_derive_correctness_explicit_process_wins() -> None:
    """scoring_trace.process.correct 显式 bool 优先于错误推断."""
    assert derive_correctness(_trace({"correct": True}), []) is True
    assert derive_correctness(_trace({"correct": False}), []) is False
    # 显式 True 即使带错误推断也以显式为准（评分器权威）
    assert (
        derive_correctness(
            _trace({"correct": True}),
            [{"error_type_id": "et", "confidence": 0.5, "rule_version": "1"}],
        )
        is True
    )


def test_derive_correctness_inferences_imply_wrong() -> None:
    """无显式 correct 时，错误推断非空 ⇒ 答错（§4.5 选项→错误类型确定映射）."""
    inferences = [{"error_type_id": "et", "confidence": 0.85, "rule_version": "1.2.0"}]
    assert derive_correctness(_trace(), inferences) is False


def test_derive_correctness_unknown() -> None:
    """无显式 correct 且无错误推断 ⇒ None（不猜）."""
    assert derive_correctness(_trace(), []) is None
    # process.correct 非 bool（脏数据）不采用
    assert derive_correctness(_trace({"correct": "yes"}), []) is None


# ────────────────────────────────────────────────────────────────────
# apply_event
# ────────────────────────────────────────────────────────────────────


def test_apply_event_wrong_enqueues_at_stage_zero() -> None:
    """答错入队：stage=0，due = 事件时刻 + 1 天，记录主要归因."""
    ev = _ev("iv1", False, error_types=("math.et.a", "math.et.b"))
    state = apply_event(None, ev, INTERVALS)
    assert state is not None
    assert state.stage == 0
    assert state.status == STATUS_PENDING
    assert state.due_at == T0 + timedelta(days=1)
    assert state.enqueued_at == T0
    assert state.source_error_type_id == "math.et.a"
    assert state.last_event_id == ev.event_id


def test_apply_event_correct_advances_stage() -> None:
    """在队答对推进：0→1，due = 答对时刻 + 3 天."""
    wrong = _ev("iv1", False)
    state = apply_event(None, wrong, INTERVALS)
    t1 = T0 + timedelta(days=1)
    state = apply_event(state, _ev("iv1", True, at=t1), INTERVALS)
    assert state is not None
    assert state.stage == 1
    assert state.status == STATUS_PENDING
    assert state.due_at == t1 + timedelta(days=3)


def test_apply_event_completes_all_intervals_then_done() -> None:
    """走完 1/3/7/21 全部间隔 → done 出队."""
    state = apply_event(None, _ev("iv1", False), INTERVALS)
    t = T0
    for expected_stage in (1, 2, 3):
        t = t + timedelta(days=INTERVALS[expected_stage - 1])
        state = apply_event(state, _ev("iv1", True, at=t), INTERVALS)
        assert state is not None
        assert state.stage == expected_stage
        assert state.status == STATUS_PENDING
    # 第 4 次答对：越过末间隔 → done
    t = t + timedelta(days=INTERVALS[3])
    state = apply_event(state, _ev("iv1", True, at=t), INTERVALS)
    assert state is not None
    assert state.status == STATUS_DONE
    # done 后答对不再变化（last_event_id 除外也不动——直接忽略）
    final = apply_event(state, _ev("iv1", True, at=t + timedelta(days=30)), INTERVALS)
    assert final == state


def test_apply_event_wrong_resets_to_stage_zero() -> None:
    """在队再答错：重置回 stage=0（含错误推断的事件自动重排）."""
    state = apply_event(None, _ev("iv1", False), INTERVALS)
    t1 = T0 + timedelta(days=1)
    state = apply_event(state, _ev("iv1", True, at=t1), INTERVALS)
    assert state is not None and state.stage == 1
    t2 = t1 + timedelta(days=3)
    state = apply_event(
        state, _ev("iv1", False, at=t2, error_types=("math.et.c",)), INTERVALS
    )
    assert state is not None
    assert state.stage == 0
    assert state.status == STATUS_PENDING
    assert state.due_at == t2 + timedelta(days=1)
    assert state.source_error_type_id == "math.et.c"
    assert state.enqueued_at == T0  # 首次入队时刻保留


def test_apply_event_correct_ignored_when_not_in_queue() -> None:
    """不在队时答对：忽略（答对不是错题，不入队）."""
    assert apply_event(None, _ev("iv1", True), INTERVALS) is None


def test_apply_event_unknown_correctness_ignored() -> None:
    """无法判定对错的事件不迁移状态."""
    state = apply_event(None, _ev("iv1", False), INTERVALS)
    assert apply_event(state, _ev("iv1", None), INTERVALS) == state
    assert apply_event(None, _ev("iv1", None), INTERVALS) is None


def test_apply_event_empty_intervals_rejected() -> None:
    """空间隔表直接报错（策略数据损坏应显式失败）."""
    with pytest.raises(ValueError, match="intervals_days"):
        apply_event(None, _ev("iv1", False), [])


# ────────────────────────────────────────────────────────────────────
# rebuild_queue
# ────────────────────────────────────────────────────────────────────


def _sample_stream() -> list[ReviewEventView]:
    """两题混合事件流：iv1 错→对→对；iv2 错→错."""
    return [
        _ev("iv1", False, at=T0),
        _ev("iv2", False, at=T0 + timedelta(hours=1), error_types=("et.x",)),
        _ev("iv1", True, at=T0 + timedelta(days=1)),
        _ev("iv2", False, at=T0 + timedelta(days=2), error_types=("et.y",)),
        _ev("iv1", True, at=T0 + timedelta(days=4)),
    ]


def test_rebuild_queue_multi_item() -> None:
    """多题事件流重放：每题状态独立迁移."""
    states = rebuild_queue(_sample_stream(), INTERVALS)
    assert set(states) == {"iv1", "iv2"}
    # iv1：stage 推进到 2，due = 第 2 次答对时刻 + 7 天
    assert states["iv1"].stage == 2
    assert states["iv1"].due_at == T0 + timedelta(days=4) + timedelta(days=7)
    # iv2：重置回 stage 0，归因更新为 et.y
    assert states["iv2"].stage == 0
    assert states["iv2"].source_error_type_id == "et.y"
    assert states["iv2"].due_at == T0 + timedelta(days=2) + timedelta(days=1)


def test_rebuild_queue_deterministic() -> None:
    """队列版本可重建（R-Z-07）：同一事件流 × 同一策略版本重放必同态."""
    stream = _sample_stream()
    first = rebuild_queue(stream, INTERVALS)
    second = rebuild_queue(stream, INTERVALS)
    assert first == second
