"""W3 S8：CTT 粗标定批处理（正确率 / 点二列 + 样本量）.

架构 v2 §4.7「参数标定」首年形态：CTT（正确率/点二列）+ 经验贝叶斯收缩
（收缩为后续波次；本模块只产出实测参数行）。

产出（落 item_param，迁移 0010）：
- params.difficulty      = 正确率 p（CTT 难度指数，越大越易）
- params.discrimination  = 修正点二列相关系数（区分度）；不可计算时为 None
- sample_size            = 参与估计的作答事件数 n
- source                 = 'measured_ctt'（实测；先验 prior_* 分存储，D5）
- method_version         = 'ctt-v1'（D6 估计器可替换）
- as_of                  = 输入事件的最大 created_at（输入数据快照右端）

分场景禁混估（宪法 D5）：
- run_ctt_calibration 的 purpose_scope 为必填单值（三值域校验），
  取数 SQL 按 scene = :scope 精确过滤——不存在跨场景聚合的代码路径。

正确性信号取数位置：scoring_trace->'dimension_scores'->>'correct'
（scorer.yaml 统一评分契约 output_schema.dimension_scores.correct，0|1；
缺该键的事件不参与估计，不计入 sample_size）。

为什么纯函数与 DB IO 分离：compute_ctt 吃记录列表、无副作用，
数值正确性可离线单测；run_ctt_calibration 只负责取数/落库。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Sequence

import ulid
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.models.item_param import ItemParam

# 估计方法版本（D6：方法迭代时递增，历史行引用当时版本）
CTT_METHOD_VERSION = "ctt-v1"
# 实测来源标识（item_param.source 域：measured_*）
CTT_SOURCE = "measured_ctt"

# 场景三值域（与 response_event_scene_enum / D5 对齐）
VALID_PURPOSE_SCOPES: frozenset[str] = frozenset(
    {"practice", "diagnosis", "measurement"}
)


# ────────────────────────────────────────────────────────────────────
# 数据结构
# ────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ResponseRecord:
    """一条参与估计的作答记录（已按场景过滤）."""

    item_version_id: str
    student_alias_id: str
    correct: float  # 0.0 / 1.0（客观题）；部分分给分题可取 [0,1]


@dataclass(frozen=True)
class ItemCttStats:
    """单题 CTT 统计量.

    difficulty：正确率 p（越大越易）。
    discrimination：修正点二列（item 得分 × 学生总分减本题 的 Pearson 相关）；
        n<2 或任一变量零方差时为 None（信息不足，不伪造 0）。
    sample_size：参与估计的记录数。
    """

    item_version_id: str
    sample_size: int
    difficulty: float
    discrimination: Optional[float]


# ────────────────────────────────────────────────────────────────────
# 纯函数：Pearson 相关与 CTT 统计
# ────────────────────────────────────────────────────────────────────


def _pearson(xs: Sequence[float], ys: Sequence[float]) -> Optional[float]:
    """Pearson 相关系数；零方差或 n<2 时返回 None."""
    n = len(xs)
    if n < 2:
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx == 0 or syy == 0:
        return None
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    return sxy / (sxx * syy) ** 0.5


def compute_ctt(records: Sequence[ResponseRecord]) -> list[ItemCttStats]:
    """对一批作答记录计算逐题 CTT 统计量（纯函数，无副作用）.

    算法：
    1. 学生总分 = 该学生全部记录 correct 之和（场景内，D5）。
    2. 逐题：difficulty = 本题记录 correct 均值。
    3. 逐题区分度 = Pearson(本题 correct, 学生总分 - 本题 correct)
       （修正点二列：总分剔除本题，避免自相关高估）。

    Args:
        records: 单场景作答记录（调用方保证已按 purpose_scope 过滤）。

    Returns:
        按 item_version_id 排序的统计量列表。
    """
    # 学生总分（该学生在批内全部记录）
    student_total: dict[str, float] = {}
    for r in records:
        student_total[r.student_alias_id] = (
            student_total.get(r.student_alias_id, 0.0) + r.correct
        )

    # 按题分组
    by_item: dict[str, list[ResponseRecord]] = {}
    for r in records:
        by_item.setdefault(r.item_version_id, []).append(r)

    stats: list[ItemCttStats] = []
    for item_version_id in sorted(by_item):
        item_records = by_item[item_version_id]
        n = len(item_records)
        xs = [r.correct for r in item_records]
        difficulty = sum(xs) / n
        # 修正总分：学生总分减本题得分
        ys = [student_total[r.student_alias_id] - r.correct for r in item_records]
        discrimination = _pearson(xs, ys)
        stats.append(
            ItemCttStats(
                item_version_id=item_version_id,
                sample_size=n,
                difficulty=difficulty,
                discrimination=discrimination,
            )
        )
    return stats


# ────────────────────────────────────────────────────────────────────
# DB 取数（单场景精确过滤，D5 禁混估）
# ────────────────────────────────────────────────────────────────────

# 为什么 correctness 用 ->>'correct' 取文本再 ::float：dimension_scores.correct
# 在 JSONB 中是 number；->> 直出文本，CAST 一次即可；缺键 ->> 得 NULL，
# WHERE 过滤后不参与估计（不计入 sample_size）。
_FETCH_SQL = """
SELECT item_version_id,
       student_alias_id::text AS student_alias_id,
       (scoring_trace->'dimension_scores'->>'correct')::float AS correct,
       created_at
FROM response_event
WHERE scene = :scope
  AND scoring_trace->'dimension_scores'->>'correct' IS NOT NULL
"""


async def run_ctt_calibration(
    db: AsyncSession,
    *,
    purpose_scope: str,
    min_sample: int = 1,
    method_version: str = CTT_METHOD_VERSION,
) -> list[ItemParam]:
    """运行 CTT 粗标定：取单场景作答事件 → 统计 → 落 item_param.

    Args:
        db: 异步会话。
        purpose_scope: 场景（practice/diagnosis/measurement），必填单值——
            D5 分场景独立估计，禁止混估；越域值抛 ValueError。
        min_sample: 最小样本量；n < min_sample 的题不产出参数行
            （样本不足不伪造参数；默认 1，生产建议 ≥30）。
        method_version: 估计方法版本（默认 ctt-v1）。

    Returns:
        本次写入的 ItemParam ORM 行列表（按 item_version_id 排序）；
        无符合条件的事件时返回空列表（不写库）。

    Notes:
        - as_of = 输入事件最大 created_at（输入快照右端）；同快照重跑
          会因 uq_item_param_identity 冲突——这是预期的幂等保护，
          换 method_version 或新数据（更大 as_of）才产生新行（D6）。
    """
    if purpose_scope not in VALID_PURPOSE_SCOPES:
        raise ValueError(
            f"purpose_scope 越域：{purpose_scope!r}"
            f"（合法域 {sorted(VALID_PURPOSE_SCOPES)}；D5 禁止跨场景混估）"
        )

    rows = (
        await db.execute(text(_FETCH_SQL), {"scope": purpose_scope})
    ).all()
    if not rows:
        return []

    records = [
        ResponseRecord(
            item_version_id=r.item_version_id,
            student_alias_id=r.student_alias_id,
            correct=float(r.correct),
        )
        for r in rows
    ]
    as_of: datetime = max(r.created_at for r in rows)

    stats = compute_ctt(records)
    written: list[ItemParam] = []
    for s in stats:
        if s.sample_size < min_sample:
            continue
        row = ItemParam(
            param_id="param_" + str(ulid.new()),
            item_version_id=s.item_version_id,
            purpose_scope=purpose_scope,
            source=CTT_SOURCE,
            params={
                "difficulty": s.difficulty,
                "discrimination": s.discrimination,
            },
            sample_size=s.sample_size,
            method_version=method_version,
            as_of=as_of,
        )
        db.add(row)
        written.append(row)
    await db.commit()
    return written


__all__ = [
    "CTT_METHOD_VERSION",
    "CTT_SOURCE",
    "VALID_PURPOSE_SCOPES",
    "ResponseRecord",
    "ItemCttStats",
    "compute_ctt",
    "run_ctt_calibration",
]
