"""W3-report 弱项报告 + 复习到期取题路由.

- GET /reports/weakness/{student_alias_id}：弱项报告 v1（S5）
- GET /review/due/{student_alias_id}：复习到期取题（S6）

宪法 D1：两个端点均只读（报告/取题都是 SELECT；复习队列的写入入口是
src/core/review/service.py::sync_review_queue，由作答链路调用，API 不暴露）。
宪法 A5/X6：本模块不 import 学科包。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import get_async_session
from src.core.report.schemas import WeaknessReport
from src.core.report.service import build_weakness_report
from src.core.review.models import ReviewQueueEntryPydantic
from src.core.review.service import get_due_reviews

router = APIRouter(prefix="", tags=["report"])


@router.get(
    "/reports/weakness/{student_alias_id}",
    response_model=WeaknessReport,
    summary="弱项报告 v1：错误类型归因 + 证据计数 + 贝叶斯置信度 + 针对性练习",
)
async def get_weakness_report(
    student_alias_id: UUID,
    scene: Optional[Literal["practice", "diagnosis", "measurement"]] = Query(
        default=None,
        description="取数场景（D5 分场景口径；缺省=跨场景汇总）",
    ),
    min_evidence: int = Query(
        default=3, ge=1, description="证据阈值：低于此数输出「证据不足」"
    ),
    session: AsyncSession = Depends(get_async_session),
) -> WeaknessReport:
    """按 error_type 聚合该学生作答事件的错误推断，产出弱项报告.

    证据达阈值的错误类型给出归因结论 + 后验置信度 + 5 题针对性小卷；
    未达阈值输出「证据不足」（不给定论、不给推荐）。
    """
    return await build_weakness_report(
        session,
        student_alias_id=student_alias_id,
        scene=scene,
        min_evidence=min_evidence,
    )


@router.get(
    "/review/due/{student_alias_id}",
    response_model=list[ReviewQueueEntryPydantic],
    summary="复习到期取题（固定间隔策略 v1：1/3/7/21 天）",
)
async def get_due_review_items(
    student_alias_id: UUID,
    now: Optional[datetime] = Query(
        default=None, description="判定基准时刻（缺省=当前 UTC）"
    ),
    limit: int = Query(default=20, ge=1, le=100),
    session: AsyncSession = Depends(get_async_session),
) -> list[ReviewQueueEntryPydantic]:
    """返回该学生已到期的在队复习条目（最逾期优先）.

    队列由 sync_review_queue 在作答评分后同步；本端点只读。
    """
    effective_now = now if now is not None else datetime.now(timezone.utc)
    entries = await get_due_reviews(
        session,
        student_alias_id=student_alias_id,
        now=effective_now,
        limit=limit,
    )
    return [
        ReviewQueueEntryPydantic.model_validate(e, from_attributes=True)
        for e in entries
    ]


__all__ = ["router"]
