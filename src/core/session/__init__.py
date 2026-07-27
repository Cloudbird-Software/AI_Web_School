"""W3-S3 在线作答会话域：会话状态服务（开始→取题→作答→反馈→错题回测）.

- models：practice_session ORM（运行态可变，非三本账）+ DTO。
- service：start_session / get_next_item / submit_answer / get_session_state /
  resume_session / abandon_session；时长保护（L≤15min、M/H≤60min，§4.8）。

宪法 A5/X6：本包不 import 任何学科包/学段包。
"""
from __future__ import annotations

from src.core.session.models import (
    Feedback,
    NextItem,
    PracticeSession,
    SessionState,
)
from src.core.session.service import (
    GRADEBAND_TIME_LIMIT_SEC,
    VALID_SCENES,
    OutOfSequenceError,
    RestRequiredError,
    SessionCompletedError,
    SessionNotFoundError,
    SessionStateError,
    UnpublishedItemError,
    abandon_session,
    get_next_item,
    get_session_state,
    resume_session,
    start_session,
    submit_answer,
)

__all__ = [
    "Feedback",
    "GRADEBAND_TIME_LIMIT_SEC",
    "NextItem",
    "OutOfSequenceError",
    "PracticeSession",
    "RestRequiredError",
    "SessionCompletedError",
    "SessionNotFoundError",
    "SessionState",
    "SessionStateError",
    "UnpublishedItemError",
    "VALID_SCENES",
    "abandon_session",
    "get_next_item",
    "get_session_state",
    "resume_session",
    "start_session",
    "submit_answer",
]
