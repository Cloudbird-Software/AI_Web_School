"""W3-S3 在线作答会话路由：开始练习→取下一题→提交作答→反馈→错题回测.

端点：
- POST   /sessions                      开始练习（静态卷 paper_id 或实例池序列）
- GET    /sessions/{session_id}         会话状态（进度/已用时长/保护余量）
- GET    /sessions/{session_id}/next    取下一题（主序列→错题回测；完成返回 done）
- POST   /sessions/{session_id}/responses  提交作答（即时评分+反馈+落 response_event）
- POST   /sessions/{session_id}/resume  休息确认（时长保护解除，计时锚点重置）
- POST   /sessions/{session_id}/abandon 放弃会话

时长保护（架构 v2 §4.8）：连续作答超学段阈值（L 15 分钟 / M/H 60 分钟）时，
取题与提交返回 409 + rest_required 休息提示，POST /resume 休息确认后继续。

评分器加载：import 本模块即注册 platform 通用评分器（exact_match/
keypoint_hit/stepwise_rubric）；学科评分器（如 math_equivalence）由部署
入口加载学科包 scorers 模块注册（核心域/本应用不 import 学科包，A5/X6）。
"""
from __future__ import annotations

from typing import Any, Literal, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import get_async_session, require_auth
# import 即注册 platform 通用评分器（同 gate validators/generic.py 模式）
import src.core.scoring.platform_scorers  # noqa: F401
from src.core.scoring.service import ScorerNotRegisteredError
from src.core.session import (
    Feedback,
    NextItem,
    SessionState,
    abandon_session,
    get_next_item,
    get_session_state,
    resume_session,
    start_session,
    submit_answer,
)
from src.core.session.service import (
    OutOfSequenceError,
    RestRequiredError,
    SessionCompletedError,
    SessionNotFoundError,
    SessionStateError,
    UnpublishedItemError,
)

router = APIRouter(prefix="/sessions", tags=["sessions"])


# ────────────────────────────────────────────────────────────────────
# 请求/响应模型
# ────────────────────────────────────────────────────────────────────

class StartSessionRequest(BaseModel):
    """POST /sessions 请求体（paper_id 与 item_version_ids 二选一）."""

    model_config = ConfigDict(extra="forbid")

    student_alias_id: UUID
    gradeband: Optional[Literal["L", "M", "H"]] = Field(
        default=None, description="学段；paper_id 会话缺省取 paper.gradeband"
    )
    scene: Literal["practice", "diagnosis"] = "practice"
    paper_id: Optional[str] = None
    item_version_ids: Optional[list[str]] = None
    retest_wrong: bool = Field(
        default=False, description="主序列走完后是否对错题回测一轮"
    )


class StartSessionResponse(BaseModel):
    """POST /sessions 响应."""

    model_config = ConfigDict(extra="forbid")

    session_id: UUID
    status: str
    scene: str
    gradeband: str
    total: int
    time_limit_sec: int


class SubmitResponseRequest(BaseModel):
    """POST /sessions/{id}/responses 请求体."""

    model_config = ConfigDict(extra="forbid")

    item_version_id: str
    response: dict[str, Any] = Field(
        ..., description="原始作答载荷（结构由交互类型 response_schema 保证）"
    )
    duration_ms: Optional[int] = Field(
        default=None, ge=0, description="作答耗时（毫秒）；NULL=未知，禁止填 0 冒充"
    )


# ────────────────────────────────────────────────────────────────────
# 异常映射
# ────────────────────────────────────────────────────────────────────

def _map_session_error(e: Exception) -> HTTPException:
    """会话域异常 → HTTP 状态码.

    - 404：会话不存在；
    - 409：状态冲突（时长保护休息提示/已完成/序列外作答）；
    - 422：参数与门纪律（未发布题目等）；
    - 500：评分器未注册（部署入口未加载学科包评分器）。
    """
    if isinstance(e, SessionNotFoundError):
        return HTTPException(status.HTTP_404_NOT_FOUND, detail=str(e))
    if isinstance(e, RestRequiredError):
        return HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "error": "rest_required",
                "message": e.message,
                "elapsed_sec": e.elapsed_sec,
                "time_limit_sec": e.time_limit_sec,
            },
        )
    if isinstance(e, (SessionCompletedError, OutOfSequenceError, SessionStateError)):
        return HTTPException(status.HTTP_409_CONFLICT, detail=str(e))
    if isinstance(e, (UnpublishedItemError, ValueError)):
        return HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(e))
    if isinstance(e, ScorerNotRegisteredError):
        return HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"评分器未注册（部署入口须加载学科包 scorers 模块）：{e}",
        )
    return HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


# ────────────────────────────────────────────────────────────────────
# 端点
# ────────────────────────────────────────────────────────────────────

@router.post(
    "",
    response_model=StartSessionResponse,
    status_code=status.HTTP_201_CREATED,
    summary="开始练习（静态卷或实例池序列快照）",
    responses={422: {"description": "参数互斥/题目未发布"}},
)
async def create_session(
    body: StartSessionRequest,
    db: AsyncSession = Depends(get_async_session),
    _auth: None = Depends(require_auth),
) -> StartSessionResponse:
    """开始练习：快照题目序列创建会话（序列一经开始不变）."""
    try:
        session = await start_session(
            db,
            student_alias_id=body.student_alias_id,
            gradeband=body.gradeband,
            scene=body.scene,
            paper_id=body.paper_id,
            item_version_ids=body.item_version_ids,
            retest_wrong=body.retest_wrong,
        )
    except Exception as e:
        raise _map_session_error(e) from e
    return StartSessionResponse(
        session_id=session.session_id,
        status=session.status,
        scene=session.scene,
        gradeband=session.gradeband,
        total=len(session.item_sequence),
        time_limit_sec=session.time_limit_sec,
    )


@router.get(
    "/{session_id}",
    response_model=SessionState,
    summary="会话状态（进度/已用时长/时长保护余量）",
    responses={404: {"description": "会话不存在"}},
)
async def read_session_state(
    session_id: UUID,
    db: AsyncSession = Depends(get_async_session),
    _auth: None = Depends(require_auth),
) -> SessionState:
    """取会话状态."""
    try:
        return await get_session_state(db, session_id)
    except Exception as e:
        raise _map_session_error(e) from e


@router.get(
    "/{session_id}/next",
    summary="取下一题（主序列→错题回测）",
    responses={
        404: {"description": "会话不存在"},
        409: {"description": "时长保护触发（rest_required）/会话已放弃"},
    },
)
async def read_next_item(
    session_id: UUID,
    db: AsyncSession = Depends(get_async_session),
    _auth: None = Depends(require_auth),
) -> dict[str, Any]:
    """取下一题；会话完成返回 {"done": true}（并把会话置 completed）."""
    try:
        item: Optional[NextItem] = await get_next_item(db, session_id)
    except Exception as e:
        raise _map_session_error(e) from e
    if item is None:
        return {"done": True, "session_id": str(session_id)}
    return item.model_dump(mode="json")


@router.post(
    "/{session_id}/responses",
    response_model=Feedback,
    summary="提交作答（即时评分+按错误类型的反馈+落 response_event）",
    responses={
        404: {"description": "会话不存在"},
        409: {"description": "时长保护触发/序列外作答/会话已结束"},
    },
)
async def create_response(
    session_id: UUID,
    body: SubmitResponseRequest,
    db: AsyncSession = Depends(get_async_session),
    _auth: None = Depends(require_auth),
) -> Feedback:
    """提交当前应答题的作答：评分→落账→反馈→错题回测标记."""
    try:
        return await submit_answer(
            db,
            session_id,
            item_version_id=body.item_version_id,
            response=body.response,
            duration_ms=body.duration_ms,
        )
    except Exception as e:
        raise _map_session_error(e) from e


@router.post(
    "/{session_id}/resume",
    response_model=SessionState,
    summary="休息确认（时长保护解除，计时锚点重置）",
    responses={404: {"description": "会话不存在"}, 409: {"description": "会话已结束"}},
)
async def resume(
    session_id: UUID,
    db: AsyncSession = Depends(get_async_session),
    _auth: None = Depends(require_auth),
) -> SessionState:
    """休息确认后继续作答."""
    try:
        return await resume_session(db, session_id)
    except Exception as e:
        raise _map_session_error(e) from e


@router.post(
    "/{session_id}/abandon",
    response_model=SessionState,
    summary="放弃会话",
    responses={404: {"description": "会话不存在"}, 409: {"description": "会话已完成"}},
)
async def abandon(
    session_id: UUID,
    db: AsyncSession = Depends(get_async_session),
    _auth: None = Depends(require_auth),
) -> SessionState:
    """放弃会话（已作答事件保留在 response_event 账）."""
    try:
        return await abandon_session(db, session_id)
    except Exception as e:
        raise _map_session_error(e) from e


__all__ = ["router"]
