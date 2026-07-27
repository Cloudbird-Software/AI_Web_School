"""§4.4 曝光账本服务：双轨查询 + 事务性预留（T-W3-assembly S1）.

架构 v2 §4.4「曝光互斥」：同母题不同卷；跨期不重复（曝光账本双轨——
静态按渠道×学科×版本×年级×周队列，在线按学生）；事务性曝光预留。

- 查询：assemble() 的 excluded_* 入参由本模块的查询函数供给；
  池加载（load_candidates）与曝光查询分离，便于快照固化与确定性重放。
- 预留：record_*_exposures 与 paper/paper_item 写入在同一事务提交，
  失败整体回滚，不产生「卷未发出但题已标记曝光」的幽灵占用。

DB 层兜底（迁移 0010）：周队列级与学生级各有 UNIQUE 约束，
并发组卷的重复曝光在 INSERT 时失败（应用层查询只是热路径优化）。

宪法 D7：学生轨只存 student_alias_id，本模块接口不接收任何 PII 字段。
宪法 A5/A7：本模块不 import 任何学科包/学段包。
"""
from __future__ import annotations

from typing import Iterable, Optional

import ulid
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.assembly.candidates import CandidateItem
from src.core.assembly.profile import Gradeband, Purpose
from src.core.models.exposure import PaperExposure, StudentExposure


# ════════════════════════════════════════════════════════════════════
# 查询（曝光集 → assemble 的 excluded_*）
# ════════════════════════════════════════════════════════════════════

async def queue_exposed_item_version_ids(
    session: AsyncSession,
    *,
    channel: str,
    subject_pack_id: str,
    week_label: str,
) -> frozenset[str]:
    """静态轨：某 渠道×学科×周队列 已曝光的题目版本集（跨期不重复）."""
    result = await session.execute(
        select(PaperExposure.item_version_id).where(
            PaperExposure.channel == channel,
            PaperExposure.subject_pack_id == subject_pack_id,
            PaperExposure.week_label == week_label,
        )
    )
    return frozenset(result.scalars().all())


async def queue_exposed_template_version_ids(
    session: AsyncSession,
    *,
    channel: str,
    subject_pack_id: str,
    week_label: str,
) -> frozenset[str]:
    """静态轨：某 渠道×学科×周队列 已曝光的母题版本集（同母题不同卷）."""
    result = await session.execute(
        select(PaperExposure.template_version_id).where(
            PaperExposure.channel == channel,
            PaperExposure.subject_pack_id == subject_pack_id,
            PaperExposure.week_label == week_label,
            PaperExposure.template_version_id.is_not(None),
        )
    )
    return frozenset(result.scalars().all())


async def student_exposed_item_version_ids(
    session: AsyncSession,
    *,
    student_alias_id: str,
) -> frozenset[str]:
    """在线轨：某学生已见过的题目版本集（跨期不重复）."""
    result = await session.execute(
        select(StudentExposure.item_version_id).where(
            StudentExposure.student_alias_id == student_alias_id
        )
    )
    return frozenset(result.scalars().all())


async def student_exposed_template_version_ids(
    session: AsyncSession,
    *,
    student_alias_id: str,
) -> frozenset[str]:
    """在线轨：某学生已见过的母题版本集（同母题不同卷）."""
    result = await session.execute(
        select(StudentExposure.template_version_id).where(
            StudentExposure.student_alias_id == student_alias_id,
            StudentExposure.template_version_id.is_not(None),
        )
    )
    return frozenset(result.scalars().all())


# ════════════════════════════════════════════════════════════════════
# 预留（与组卷写入同事务）
# ════════════════════════════════════════════════════════════════════

async def record_paper_exposures(
    session: AsyncSession,
    *,
    channel: str,
    subject_pack_id: str,
    gradeband: Gradeband,
    week_label: str,
    items: Iterable[CandidateItem],
    textbook_version: Optional[str] = None,
    paper_id: Optional[str] = None,
) -> int:
    """静态轨曝光预留：把入选题登记到 渠道×学科×周队列.

    与 paper/paper_item 写入同事务调用（§4.4 事务性曝光预留）；
    本函数只 flush 不 commit，事务边界归调用方。
    返回登记行数。
    """
    rows = [
        PaperExposure(
            exposure_id=str(ulid.new()),
            channel=channel,
            subject_pack_id=subject_pack_id,
            textbook_version=textbook_version,
            gradeband=gradeband,
            week_label=week_label,
            item_version_id=item.item_version_id,
            template_version_id=item.template_version_id,
            paper_id=paper_id,
        )
        for item in items
    ]
    session.add_all(rows)
    await session.flush()
    return len(rows)


async def record_student_exposures(
    session: AsyncSession,
    *,
    student_alias_id: str,
    purpose: Purpose,
    items: Iterable[CandidateItem],
    paper_id: Optional[str] = None,
    session_id: Optional[str] = None,
) -> int:
    """在线轨曝光预留：把发给学生（匿名 id）的题登记到学生轨.

    与发题/组卷写入同事务调用；本函数只 flush 不 commit。
    返回登记行数。
    """
    rows = [
        StudentExposure(
            exposure_id=str(ulid.new()),
            student_alias_id=student_alias_id,
            item_version_id=item.item_version_id,
            template_version_id=item.template_version_id,
            paper_id=paper_id,
            session_id=session_id,
            purpose=purpose,
        )
        for item in items
    ]
    session.add_all(rows)
    await session.flush()
    return len(rows)


__all__ = [
    "queue_exposed_item_version_ids",
    "queue_exposed_template_version_ids",
    "student_exposed_item_version_ids",
    "student_exposed_template_version_ids",
    "record_paper_exposures",
    "record_student_exposures",
]
