"""难度重估触发器（T-W2-006）.

在实例化后检测 params 是否变更了 difficulty_relevant 槽，
若是则发布 difficulty_reestimate 事件（JSON schema 见
specs/contracts/events/difficulty_reestimate_event.md）。

事件进入 Redis 任务队列，供 W3+ 参数标定消费；W2 只需落事件并验证 schema。

宪法 X6：本模块不 import 任何学科包/学段包。
宪法 D5：事件含 scene 字段，分场景独立估计，禁止混估。
"""
from src.core.instantiation.difficulty.trigger import (
    DifficultyReestimateEvent,
    detect_difficulty_change,
    emit_difficulty_reestimate,
)

__all__ = [
    "DifficultyReestimateEvent",
    "detect_difficulty_change",
    "emit_difficulty_reestimate",
]
