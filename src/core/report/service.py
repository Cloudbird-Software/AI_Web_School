"""W3 S5 弱项报告服务：报告生成 + 针对性练习推荐.

- build_weakness_report：取学生作答事件（可选场景过滤，D5 分场景取数在
  SQL WHERE 定型）→ aggregator 纯函数聚合 → 阈值判定 → 对达阈值类型
  调 recommend_practice 组 5 题小卷。
- recommend_practice：查已发布实例池（item_version.status='published'）中
  error_bindings 绑定同 error_type_id 的题（架构 §4.6 S2 针对性练习的
  现组小卷形态；预渲染专项小卷库存为后续演进，本接口签名兼容）。

为什么推荐要剔除 contributing 题：刚在这些题上暴露该弱项，原题重练
测的是记忆不是理解；剔除后仍不足 5 题时如实返回更少（禁止凑数塞
不绑该错误类型的题——那会让练习失去针对性）。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.events.writer import Scene
from src.core.report.aggregator import (
    MIN_EVIDENCE_DEFAULT,
    InferenceEventView,
    aggregate_inferences,
)
from src.core.report.schemas import WeaknessItem, WeaknessReport

# 取数：报告只读 response_event（作答事件账永不被本模块写）
_EVENTS_SQL = """
SELECT item_version_id, error_inferences
FROM response_event
WHERE student_alias_id = :student_alias_id
"""

_SCENE_FILTER_SQL = " AND scene = :scene"

# 已发布实例池按错误类型查题（error_bindings 顶层是数组，逐元素匹配）
_RECOMMEND_SQL = """
SELECT iv.item_version_id
FROM item_version iv
WHERE iv.status = 'published'
  AND EXISTS (
      SELECT 1
      FROM jsonb_array_elements(iv.error_bindings) AS b
      WHERE b ->> 'error_type_id' = :error_type_id
  )
  AND iv.item_version_id <> ALL (CAST(:excluded AS text[]))
ORDER BY iv.item_version_id
LIMIT :limit
"""

RECOMMEND_LIMIT_DEFAULT = 5


async def recommend_practice(
    session: AsyncSession,
    *,
    error_type_id: str,
    exclude_item_version_ids: Optional[list[str]] = None,
    limit: int = RECOMMEND_LIMIT_DEFAULT,
) -> list[str]:
    """按错误类型查已发布实例池，组针对性强项小卷（默认 5 题）.

    Args:
        error_type_id: 目标错误类型（item_version.error_bindings[].error_type_id）。
        exclude_item_version_ids: 剔除的题目版本（通常是产生过该错误证据的题）。
        limit: 小卷题量上限；池中绑定题不足时如实返回更少，不凑数。

    Returns:
        item_version_id 列表（按 id 升序，确定性）。
    """
    rows = (
        await session.execute(
            text(_RECOMMEND_SQL),
            {
                "error_type_id": error_type_id,
                "excluded": exclude_item_version_ids or [],
                "limit": limit,
            },
        )
    ).all()
    return [row.item_version_id for row in rows]


async def build_weakness_report(
    session: AsyncSession,
    *,
    student_alias_id: UUID,
    scene: Optional[Scene] = None,
    min_evidence: int = MIN_EVIDENCE_DEFAULT,
    recommend_limit: int = RECOMMEND_LIMIT_DEFAULT,
) -> WeaknessReport:
    """生成学生弱项报告 v1.

    每个错误类型：证据计数 + 贝叶斯后验置信度 + 阈值判定；
    达阈值（concluded）的类型附带针对性练习推荐（剔除来源题）；
    未达阈值输出「证据不足」，不给定论、不给推荐。
    """
    sql = _EVENTS_SQL + (_SCENE_FILTER_SQL if scene is not None else "")
    params: dict = {"student_alias_id": student_alias_id}
    if scene is not None:
        params["scene"] = scene
    rows = (await session.execute(text(sql), params)).all()

    events = [
        InferenceEventView(
            item_version_id=row.item_version_id,
            error_inferences=tuple(row.error_inferences),
        )
        for row in rows
    ]
    evidences = aggregate_inferences(events)

    items: list[WeaknessItem] = []
    for error_type_id, ev in evidences.items():
        concluded = ev.evidence_count >= min_evidence
        recommended: list[str] = []
        if concluded:
            recommended = await recommend_practice(
                session,
                error_type_id=error_type_id,
                exclude_item_version_ids=sorted(ev.contributing_item_version_ids),
                limit=recommend_limit,
            )
        items.append(
            WeaknessItem(
                error_type_id=error_type_id,
                status="concluded" if concluded else "insufficient_evidence",
                evidence_count=ev.evidence_count,
                confidence=round(ev.posterior, 4),
                recommended_item_version_ids=recommended,
            )
        )

    # 确定性排序：证据多的在前，同计数按 error_type_id 字典序
    items.sort(key=lambda it: (-it.evidence_count, it.error_type_id))

    return WeaknessReport(
        student_alias_id=student_alias_id,
        scene=scene,
        min_evidence=min_evidence,
        generated_at=datetime.now(timezone.utc),
        items=items,
    )


__all__ = [
    "RECOMMEND_LIMIT_DEFAULT",
    "build_weakness_report",
    "recommend_practice",
]
