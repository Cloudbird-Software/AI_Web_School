"""核心域·作答事件账（T-W1-005）.

按 specs/contracts/events/response_event.md v1.0.0 落地作答事件写入服务。
宪法 D1 三本账之一「作答事件账」——append-only，DB 触发器物理强制
禁 UPDATE/DELETE（迁移 0003）。

宪法 A5：核心域零学科特判，本包不 import 任何学科包/学段包。
"""
from src.core.events.writer import record_event

__all__ = ["record_event"]
