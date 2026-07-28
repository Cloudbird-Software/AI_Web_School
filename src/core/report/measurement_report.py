"""T-W4-030 测量卷报告（架构 v2 §4.7 / 宪法 D6 估计器可替换）.

本模块把 ctt_report.generate_ctt_report 的纯统计产物包装为「测量卷报告」：
- 按 paper_id 从 response_event 取 scene='measurement' 的作答事件
  （source_ref->>'paper_id' = :paper_id 精确过滤；D5 禁混估）
- 调用 generate_ctt_report 计算 α/SEM/区分度/难度分布
- 回填当时活跃的 ActiveModelPointer（model_version + code_digest +
  input_snapshot_id + graph_release_id + activated_at）作为 estimator_ref
- 报告版本化：每次生成都引用当时活跃的估计器版本，历史报告永不漂移

为什么 ActiveModelPointer 引用是报告头等公民（D6）：
- 估计器会迭代（v1 CTT → v2 Rasch/2PL）；同一份测量卷在不同时点用不同
  估计器估计，结论会变化
- 历史报告必须能回答「这份报告是用哪个版本的估计器算出来的」，否则飞轮
  闭环无法审计
- estimator_ref 在报告生成时刻定型，永不更新；后续 ActiveModelPointer
  切换不影响已生成的报告

为什么按 source_ref->>'paper_id' 取数：
- response_event 无 paper_id 顶层字段（A4 入水口规则：paper_id 在 source_ref
  JSONB 内，由 record_event 在写入时定型）
- 按 paper_id 取数 = 「这份测量卷的所有作答事件」，与按 student / scene 取数
  语义不同；本报告是「卷级」报告，不是学生级
- 同卷事件可能跨多个学生（n>1 才能算 α），取数后由 generate_ctt_report
  去重学生 id 统计 n

非目标（任务卡 non_goals）：Rasch/IRT 报告、测量等值、标准设定、常模建立。

宪法 A5/A7：本模块不 import 任何学科包/学段包（学科零特判）。
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.data.active_model_pointer import ActiveModelPointer
from src.core.data.ctt import ResponseRecord
from src.core.data.ctt_report import CttReport, generate_ctt_report

logger = logging.getLogger(__name__)

# 测量场景固定为 measurement（本报告只服务测量卷；D5 禁混估）
_MEASUREMENT_SCOPE = "measurement"


# ────────────────────────────────────────────────────────────────────
# 数据结构
# ────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class EstimatorRef:
    """报告引用的估计器版本（D6 历史报告引用当时版本）.

    字段映射 ActiveModelPointer.get_active 返回的 EstimatorRun 关键列；
    报告生成时刻定型，永不更新。
    """

    purpose_scope: str  # 固定 'measurement'
    model_version: str
    code_digest: str
    input_snapshot_id: str
    graph_release_id: str
    activated_at: datetime


@dataclass(frozen=True)
class MeasurementReport:
    """测量卷报告：CTT 报告 + 估计器引用 + 卷级元数据.

    Attributes:
        paper_id: 测量卷 id（与 MeasurementPaper.selection_digest 无强关联，
            本报告仅以 paper_id 作为取数键与报告标签）。
        spec_table_ref: 细目表引用（id/version），可选；若调用方提供则回显。
        ctt_report: CTT 信度/区分度报告（纯统计产物）。
        estimator_ref: 当时活跃估计器引用（D6）；无活跃版本时为 None
            （报告头部应警示「无活跃估计器，参数引用缺失」）。
        generated_at: 报告生成时刻（UTC）。
    """

    paper_id: str
    spec_table_ref: Optional[str]
    ctt_report: CttReport
    estimator_ref: Optional[EstimatorRef]
    generated_at: datetime


# ────────────────────────────────────────────────────────────────────
# DB 取数（按 paper_id + scene='measurement'，D5 禁混估）
# ────────────────────────────────────────────────────────────────────

# 为什么 source_ref->>'paper_id'：paper_id 在 source_ref JSONB 内（A4 入水口）；
# ->> 直出文本，与 :paper_id 字符串比较，缺键 ->> 得 NULL 不匹配（不参与统计）。
# 为什么 scene='measurement' 硬过滤：本报告只服务测量卷，D5 禁止跨场景混估；
# 即便 source_ref.paper_id 关联到测量卷，scene 也必须是 measurement。
_FETCH_EVENTS_SQL = """
SELECT item_version_id,
       student_alias_id::text AS student_alias_id,
       (scoring_trace->'dimension_scores'->>'correct')::float AS correct,
       created_at
FROM response_event
WHERE scene = :scope
  AND source_ref->>'paper_id' = :paper_id
  AND scoring_trace->'dimension_scores'->>'correct' IS NOT NULL
"""


async def _fetch_measurement_events(
    session: AsyncSession,
    *,
    paper_id: str,
) -> list[ResponseRecord]:
    """按 paper_id 取测量场景作答事件（D5 禁混估）.

    Args:
        session: 异步 SQLAlchemy 会话。
        paper_id: 测量卷 id（source_ref->>'paper_id' 匹配）。

    Returns:
        ResponseRecord 列表（已按 scene='measurement' + paper_id 过滤；
        缺 dimension_scores.correct 的事件不参与，不计入 sample_size——
        与 ctt.run_ctt_calibration 同口径）。
    """
    rows = (
        await session.execute(
            text(_FETCH_EVENTS_SQL),
            {"scope": _MEASUREMENT_SCOPE, "paper_id": paper_id},
        )
    ).all()
    return [
        ResponseRecord(
            item_version_id=r.item_version_id,
            student_alias_id=r.student_alias_id,
            correct=float(r.correct),
        )
        for r in rows
    ]


async def _resolve_estimator_ref(
    session: AsyncSession,
    *,
    now: datetime,
) -> Optional[EstimatorRef]:
    """取 measurement 场景当前活跃估计器引用（D6）.

    Args:
        session: 异步 SQLAlchemy 会话。
        now: 报告生成时刻（用于回溯当时活跃版本；通常与报告 generated_at 一致）。

    Returns:
        EstimatorRef（若 measurement 场景有活跃版本）；None 时调用方应警示
        「无活跃估计器，参数引用缺失」（D6 历史可追溯性的边界情形）。
    """
    ptr = ActiveModelPointer(session)
    run = await ptr.get_active(_MEASUREMENT_SCOPE, timestamp=now)
    if run is None:
        return None
    return EstimatorRef(
        purpose_scope=run.purpose_scope,
        model_version=run.model_version,
        code_digest=run.code_digest,
        input_snapshot_id=run.input_snapshot_id,
        graph_release_id=run.graph_release_id,
        activated_at=run.activated_at,
    )


# ────────────────────────────────────────────────────────────────────
# 主入口
# ────────────────────────────────────────────────────────────────────


async def build_measurement_report(
    session: AsyncSession,
    *,
    paper_id: str,
    spec_table_ref: Optional[str] = None,
    now: Optional[datetime] = None,
) -> MeasurementReport:
    """生成测量卷报告（CTT 报告 + 估计器引用 + 卷级元数据）.

    参数:
        session: 异步 SQLAlchemy 会话。
        paper_id: 测量卷 id（取数键，匹配 response_event.source_ref->>'paper_id'）。
        spec_table_ref: 细目表引用（如 'spec-xxx/1.0.0'）；可选，若提供则回显
            在报告头，便于审计卷规约。
        now: 报告生成时刻（默认 datetime.now(UTC)）；可传入固定值用于确定性测试
            与 ActiveModelPointer 历史回溯对齐。

    返回:
        MeasurementReport（含 ctt_report + estimator_ref + 卷级元数据）。

    Notes:
        - 取数只读 response_event（D1 三本账只增不改，本模块不写）。
        - estimator_ref 为 None 时（measurement 场景无活跃估计器），ctt_report
          仍照常生成（统计量与估计器版本无关），但报告头部应警示参数引用缺失——
          这是为了让「无活跃估计器」时仍能产出统计报告，D6 引用缺失显式呈现
          而非阻塞飞轮。
    """
    generated_at = now or datetime.now(timezone.utc)

    events = await _fetch_measurement_events(session, paper_id=paper_id)
    ctt_report = generate_ctt_report(
        events,
        paper_id=paper_id,
        now=generated_at,
    )

    estimator_ref = await _resolve_estimator_ref(session, now=generated_at)
    if estimator_ref is None:
        logger.warning(
            "measurement_report: paper_id=%s 无活跃 measurement 估计器"
            "（estimator_ref=None；D6 引用缺失，报告头部应警示）",
            paper_id,
        )

    return MeasurementReport(
        paper_id=paper_id,
        spec_table_ref=spec_table_ref,
        ctt_report=ctt_report,
        estimator_ref=estimator_ref,
        generated_at=generated_at,
    )


__all__ = [
    "EstimatorRef",
    "MeasurementReport",
    "build_measurement_report",
]
