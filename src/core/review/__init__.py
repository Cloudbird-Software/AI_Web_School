"""W3 S6 复习排程 v1（架构 §4.4：纯函数策略 + 版本化 ReviewPolicy → 可重建 ReviewQueue）.

子模块：
- scheduler：纯函数核——事件流 × 策略版本 → 队列状态（无任何 IO，可重建性的根基）
- models：review_policy / review_queue_entry ORM（迁移 0010）
- service：sync_review_queue（事件流重放 → 幂等落库）+ get_due_reviews（到期取题）

宪法 A5：本包禁止 import 任何学科包/学段包。
"""
from __future__ import annotations

from src.core.review.scheduler import (
    DEFAULT_POLICY_ID,
    DEFAULT_POLICY_VERSION,
    EntryState,
    ReviewEventView,
    apply_event,
    derive_correctness,
    rebuild_queue,
)
from src.core.review.service import (
    get_due_reviews,
    load_policy_intervals,
    sync_review_queue,
)

__all__ = [
    "DEFAULT_POLICY_ID",
    "DEFAULT_POLICY_VERSION",
    "EntryState",
    "ReviewEventView",
    "apply_event",
    "derive_correctness",
    "rebuild_queue",
    "get_due_reviews",
    "load_policy_intervals",
    "sync_review_queue",
]
