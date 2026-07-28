"""T-W4-004 题目健康度模型 + 生命周期状态机.

架构 v2 §4.7「飞轮闭环」：题目健康度评估（正确率异常/区分度低/干扰项无人选/
耗时异常）→ 生命周期 ACTIVE→WATCH→QUARANTINED→RETIRED（签发制，退役不删除）。

核心接口：
- evaluate_health(db, item_id, *, purpose_scope='practice') → HealthReport
  计算单题健康度评分与异常标签列表（至少 4 类异常）。
- transition_lifecycle(db, item_id, to_state, *, gate_certificate_id, reason,
  health_report) → ItemLifecycleTransition
  状态机转换：校验规则后 INSERT 一行 transition（append-only）。
- get_current_state(db, item_id) → ItemLifecycleState | None
  取该 item 最新 transition 的 to_state（当前生命周期状态）。
- query_active_pool_filter() → SQL 片段
  返回活跃池 item_id 子查询（排除 QUARANTINED/RETIRED）。

设计要点：
- 健康度评估是只读分析（不改 DB）；状态变更是 append-only INSERT（D1 物理强制）
- 区分度从 item_param 表读（CTT 标定产出，避免重算 + 尊重数据飞轮分工）
- ACTIVE↔WATCH 自动转换（无门证书）；WATCH→QUARANTINED、任何→RETIRED 需门证书
- RETIRED 为终态，无回边；历史版本保留（D1 版本账只增不改）

宪法 A5/X6：本模块是核心域数据子模块，禁止 import 任何学科包/学段包。
"""
from __future__ import annotations

import logging
import statistics
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from typing import Any, Optional

import ulid
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.models.item_lifecycle import (
    ACTIVE_POOL_STATES,
    ItemLifecycleState,
    ItemLifecycleTransition,
    LIFECYCLE_STATES,
    TERMINAL_STATES,
)

logger = logging.getLogger(__name__)

# ────────────────────────────────────────────────────────────────────
# 常量
# ────────────────────────────────────────────────────────────────────

# 健康度评估最小样本门槛（n < 此值不判定异常，记 insufficient_sample 标签）
HEALTH_MIN_SAMPLE = 30

# 异常阈值（架构 §4.7 默认档，可在调用方覆盖后重算）
CORRECT_RATE_TOO_HIGH = 0.95   # 正确率 > 0.95 → 题太易（无区分度）
CORRECT_RATE_TOO_LOW = 0.05    # 正确率 < 0.05 → 题太难
LOW_DISCRIMINATION = 0.2      # 区分度 < 0.2 → 区分度低
TIME_TOO_FAST_MS = 2000        # 中位耗时 < 2s → 猜题/秒杀
TIME_TOO_SLOW_MS = 30000       # 中位耗时 > 30s → 困惑/卡题

# 每个异常的扣分（health_score = 1.0 - sum(penalties)，下限 0.0）
ANOMALY_PENALTY = 0.2

# 状态机转换规则：from_state → {允许的 to_state 集合}
# None = 初始（无既有状态，仅允许 → ACTIVE）
# RETIRED = 终态，无任何回边
_ALLOWED_TRANSITIONS: dict[Optional[str], frozenset[str]] = {
    None: frozenset({ItemLifecycleState.ACTIVE.value}),
    ItemLifecycleState.ACTIVE.value: frozenset(
        {ItemLifecycleState.WATCH.value, ItemLifecycleState.RETIRED.value}
    ),
    ItemLifecycleState.WATCH.value: frozenset(
        {
            ItemLifecycleState.ACTIVE.value,
            ItemLifecycleState.QUARANTINED.value,
            ItemLifecycleState.RETIRED.value,
        }
    ),
    ItemLifecycleState.QUARANTINED.value: frozenset(
        {ItemLifecycleState.WATCH.value, ItemLifecycleState.RETIRED.value}
    ),
    ItemLifecycleState.RETIRED.value: frozenset(),  # 终态
}

# 需要门证书的目标状态（转入这些状态必须带 gate_certificate_id）
GATE_CERT_REQUIRED_STATES: frozenset[str] = frozenset(
    {ItemLifecycleState.QUARANTINED.value, ItemLifecycleState.RETIRED.value}
)


# ────────────────────────────────────────────────────────────────────
# 健康度报告数据结构
# ────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class HealthReport:
    """单题健康度评估报告.

    - item_id：被评估的题目身份
    - sample_size：参与评估的作答事件数（n）
    - health_score：0.0~1.0（1.0=完全健康，0.0=最差）
    - anomalies：异常标签列表（如 ['correct_rate_too_high', 'low_discrimination']）
    - metrics：明细指标字典（correct_rate/discrimination/duration_stats/...）
    - insufficient_sample：n < HEALTH_MIN_SAMPLE 时为 True（不判定异常）
    """

    item_id: str
    sample_size: int
    health_score: float
    anomalies: list[str] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    insufficient_sample: bool = False

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["health_score"] = float(self.health_score)
        return d


# ────────────────────────────────────────────────────────────────────
# 取数 SQL
# ────────────────────────────────────────────────────────────────────

# 取该 item 的所有 item_version_id
_FETCH_ITEM_VERSIONS_SQL = """
SELECT item_version_id FROM item_version WHERE item_id = :item_id
"""

# 取这些 item_version_id 的作答事件（按场景过滤，D5）
# 为什么 correctness 取 scoring_trace->'dimension_scores'->>'correct'：
# 与 ctt.py 同口径（scorer.yaml 统一契约）；缺键事件过滤掉不计入样本。
_FETCH_EVENTS_SQL = """
SELECT event_id,
       item_version_id,
       raw_payload,
       scoring_trace,
       duration_ms,
       created_at
FROM response_event
WHERE item_version_id = ANY(:iv_ids)
  AND scene = :scope
  AND scoring_trace->'dimension_scores'->>'correct' IS NOT NULL
ORDER BY created_at ASC
"""

# 取该 item 最新实测 item_param（区分度来源）
_FETCH_LATEST_PARAM_SQL = """
SELECT params
FROM item_param
WHERE item_version_id = ANY(:iv_ids)
  AND purpose_scope = :scope
  AND source LIKE 'measured_%'
ORDER BY as_of DESC
LIMIT 1
"""

# 取 item_version 的选项结构（干扰项分析用）
# 单选题：scoring_ref.scorer_params.answer = 正解；error_bindings[].option_value = 干扰项
_FETCH_ITEM_VERSION_OPTIONS_SQL = """
SELECT item_version_id,
       interaction_ref,
       scoring_ref,
       error_bindings
FROM item_version
WHERE item_version_id = ANY(:iv_ids)
  AND interaction_ref->>'interaction_id' = 'single_choice'
"""


# ────────────────────────────────────────────────────────────────────
# 健康度评估
# ────────────────────────────────────────────────────────────────────


def _median(values: list[float]) -> Optional[float]:
    """中位数（空列表返回 None）."""
    if not values:
        return None
    return statistics.median(values)


def _detect_anomalies(
    *,
    sample_size: int,
    correct_rate: float,
    discrimination: Optional[float],
    duration_median: Optional[float],
    distractor_rates: Optional[dict[str, float]],
    correct_option: Optional[str],
) -> list[str]:
    """根据指标判定异常标签（至少 4 类异常）.

    四类异常（架构 §4.7）：
    1. correct_rate_anomaly：正确率过高（>0.95）或过低（<0.05）
    2. low_discrimination：区分度 < 0.2（仅 discrimination 可计算时判定）
    3. no_distractor_selected：某干扰项被选 0 次（单选题，n>=min_sample）
    4. time_anomaly：中位耗时过快（<2s）或过慢（>30s）
    """
    anomalies: list[str] = []
    if sample_size < HEALTH_MIN_SAMPLE:
        return anomalies  # 样本不足不判定（insufficient_sample 在外层标记）

    # 1. 正确率异常
    if correct_rate > CORRECT_RATE_TOO_HIGH:
        anomalies.append("correct_rate_too_high")
    elif correct_rate < CORRECT_RATE_TOO_LOW:
        anomalies.append("correct_rate_too_low")

    # 2. 区分度低（仅 discrimination 可计算时判定）
    if discrimination is not None and discrimination < LOW_DISCRIMINATION:
        anomalies.append("low_discrimination")

    # 3. 干扰项无人选（单选题）
    if distractor_rates is not None and correct_option is not None:
        for option, rate in distractor_rates.items():
            if option != correct_option and rate == 0.0:
                anomalies.append("no_distractor_selected")
                break  # 一个干扰项无人选即标记

    # 4. 耗时异常
    if duration_median is not None:
        if duration_median < TIME_TOO_FAST_MS:
            anomalies.append("time_too_fast")
        elif duration_median > TIME_TOO_SLOW_MS:
            anomalies.append("time_too_slow")

    return anomalies


async def evaluate_health(
    db: AsyncSession,
    item_id: str,
    *,
    purpose_scope: str = "practice",
) -> HealthReport:
    """评估单题健康度（只读，不改 DB）.

    Args:
        db：异步会话
        item_id：题目身份 id
        purpose_scope：场景（默认 practice；D5 分场景独立评估）

    Returns:
        HealthReport：含 sample_size / health_score / anomalies / metrics
    """
    # 1. 取该 item 的所有 item_version_id
    iv_rows = (
        await db.execute(text(_FETCH_ITEM_VERSIONS_SQL), {"item_id": item_id})
    ).fetchall()
    iv_ids = [r[0] for r in iv_rows]
    if not iv_ids:
        # 题目无任何版本 → 空报告
        return HealthReport(
            item_id=item_id,
            sample_size=0,
            health_score=0.0,
            anomalies=[],
            metrics={"note": "no item_version found"},
            insufficient_sample=True,
        )

    # 2. 取作答事件
    event_rows = (
        await db.execute(
            text(_FETCH_EVENTS_SQL),
            {"iv_ids": iv_ids, "scope": purpose_scope},
        )
    ).fetchall()
    sample_size = len(event_rows)

    if sample_size == 0:
        return HealthReport(
            item_id=item_id,
            sample_size=0,
            health_score=0.0,
            anomalies=[],
            metrics={"note": "no response_event in scope"},
            insufficient_sample=True,
        )

    # 3. 计算指标
    correct_values: list[float] = []
    durations: list[float] = []
    selections: list[str] = []  # raw_payload.selected
    for row in event_rows:
        trace = row[3] if isinstance(row[3], dict) else {}
        ds = trace.get("dimension_scores", {}) if isinstance(trace, dict) else {}
        correct_str = ds.get("correct") if isinstance(ds, dict) else None
        try:
            correct_values.append(float(correct_str))
        except (TypeError, ValueError):
            correct_values.append(0.0)
        if row[4] is not None:
            durations.append(float(row[4]))
        raw = row[2] if isinstance(row[2], dict) else {}
        sel = raw.get("selected")
        if isinstance(sel, str):
            selections.append(sel)

    correct_rate = sum(correct_values) / len(correct_values)
    duration_median = _median(durations)

    # 4. 取区分度（从 item_param 读，CTT 标定产出）
    param_row = (
        await db.execute(
            text(_FETCH_LATEST_PARAM_SQL),
            {"iv_ids": iv_ids, "scope": purpose_scope},
        )
    ).first()
    discrimination: Optional[float] = None
    if param_row and isinstance(param_row[0], dict):
        d = param_row[0].get("discrimination")
        if d is not None:
            try:
                discrimination = float(d)
            except (TypeError, ValueError):
                discrimination = None

    # 5. 干扰项分析（单选题）
    distractor_rates: Optional[dict[str, float]] = None
    correct_option: Optional[str] = None
    option_rows = (
        await db.execute(
            text(_FETCH_ITEM_VERSION_OPTIONS_SQL),
            {"iv_ids": iv_ids},
        )
    ).fetchall()
    if option_rows and selections:
        # 取第一个单选题版本的正解与干扰项
        first = option_rows[0]
        scoring_ref = first[2] if isinstance(first[2], dict) else {}
        sp = scoring_ref.get("scorer_params", {}) if isinstance(scoring_ref, dict) else {}
        correct_option = sp.get("answer")
        error_bindings = first[3] if isinstance(first[3], list) else []
        distractor_options = [
            eb.get("option_value") for eb in error_bindings
            if isinstance(eb, dict) and eb.get("option_value")
        ]
        all_options = set([correct_option] + distractor_options) if correct_option else set(distractor_options)
        if all_options and sample_size > 0:
            counts: dict[str, int] = {opt: 0 for opt in all_options}
            for sel in selections:
                if sel in counts:
                    counts[sel] += 1
            distractor_rates = {opt: cnt / sample_size for opt, cnt in counts.items()}

    # 6. 判定异常
    insufficient = sample_size < HEALTH_MIN_SAMPLE
    anomalies = _detect_anomalies(
        sample_size=sample_size,
        correct_rate=correct_rate,
        discrimination=discrimination,
        duration_median=duration_median,
        distractor_rates=distractor_rates,
        correct_option=correct_option,
    )
    health_score = max(0.0, 1.0 - ANOMALY_PENALTY * len(anomalies))

    metrics = {
        "correct_rate": correct_rate,
        "discrimination": discrimination,
        "duration_median_ms": duration_median,
        "distractor_rates": distractor_rates,
        "correct_option": correct_option,
        "purpose_scope": purpose_scope,
    }

    return HealthReport(
        item_id=item_id,
        sample_size=sample_size,
        health_score=health_score,
        anomalies=anomalies,
        metrics=metrics,
        insufficient_sample=insufficient,
    )


# ────────────────────────────────────────────────────────────────────
# 状态机：当前状态 + 转换
# ────────────────────────────────────────────────────────────────────


_GET_LATEST_TRANSITION_SQL = """
SELECT to_state
FROM item_lifecycle_transition
WHERE item_id = :item_id
ORDER BY created_at DESC, transition_id DESC
LIMIT 1
"""


async def get_current_state(
    db: AsyncSession, item_id: str
) -> Optional[ItemLifecycleState]:
    """取 item 当前生命周期状态（最新 transition 的 to_state）.

    无 transition 返回 None（未初始化）。
    """
    row = (
        await db.execute(text(_GET_LATEST_TRANSITION_SQL), {"item_id": item_id})
    ).first()
    if row is None:
        return None
    return ItemLifecycleState(row[0])


class LifecycleTransitionError(ValueError):
    """非法状态转换（from→to 不在允许集合 / 缺门证书 / 终态回边）."""


async def transition_lifecycle(
    db: AsyncSession,
    item_id: str,
    to_state: str,
    *,
    gate_certificate_id: Optional[str] = None,
    reason: Optional[str] = None,
    health_report: Optional[HealthReport] = None,
) -> ItemLifecycleTransition:
    """执行状态机转换（校验规则后 INSERT transition 行）.

    转换规则（验收 §2）：
    - ACTIVE ↔ WATCH：自动（无需门证书）
    - WATCH → QUARANTINED：需门证书
    - 任何 → RETIRED：需门证书
    - RETIRED 为终态，无回边

    Args:
        db：异步会话
        item_id：题目身份 id
        to_state：目标状态（ACTIVE/WATCH/QUARANTINED/RETIRED）
        gate_certificate_id：门证书 id（转入 QUARANTINED/RETIRED 必填）
        reason：变更原因（异常标签 / 人工理由）
        health_report：变更时刻健康度快照（写 health_score + anomaly_tags）

    Returns:
        新插入的 ItemLifecycleTransition（已 flush，含 transition_id）

    Raises:
        LifecycleTransitionError：非法转换 / 缺门证书 / 终态回边
        ValueError：非法 to_state 值
    """
    if to_state not in LIFECYCLE_STATES:
        raise ValueError(
            f"非法 to_state={to_state!r}；合法值 {sorted(LIFECYCLE_STATES)}"
        )

    # 取当前状态
    current = await get_current_state(db, item_id)
    from_state = current.value if current is not None else None

    # 校验转换合法性
    allowed = _ALLOWED_TRANSITIONS.get(from_state, frozenset())
    if to_state not in allowed:
        if from_state in TERMINAL_STATES:
            raise LifecycleTransitionError(
                f"{from_state} 为终态，禁止任何转换（→ {to_state}）"
            )
        raise LifecycleTransitionError(
            f"非法转换 {from_state} → {to_state}；"
            f"允许目标 {sorted(allowed) if allowed else '（终态无回边）'}"
        )

    # 校验门证书
    if to_state in GATE_CERT_REQUIRED_STATES and not gate_certificate_id:
        raise LifecycleTransitionError(
            f"转入 {to_state} 需门证书（gate_certificate_id 必填）"
        )

    # 构造 transition 行
    transition = ItemLifecycleTransition(
        transition_id=str(ulid.new()),
        item_id=item_id,
        from_state=from_state,
        to_state=to_state,
        gate_certificate_id=gate_certificate_id,
        reason=reason,
        health_score=(
            Decimal(str(health_report.health_score)).quantize(Decimal("0.001"))
            if health_report is not None
            else None
        ),
        anomaly_tags=(
            health_report.anomalies if health_report is not None else None
        ),
    )
    db.add(transition)
    await db.flush()
    return transition


# ────────────────────────────────────────────────────────────────────
# 活跃池查询
# ────────────────────────────────────────────────────────────────────

# 活跃池 item_id 子查询：最新 transition 的 to_state ∈ {ACTIVE, WATCH}
# 为什么用 DISTINCT ON：每个 item 取最新一行（created_at DESC），
# PG 专有语法，与 score_run enrichment 同手法。
_ACTIVE_POOL_SQL = """
SELECT item_id
FROM (
    SELECT DISTINCT ON (item_id)
           item_id, to_state, created_at, transition_id
    FROM item_lifecycle_transition
    ORDER BY item_id, created_at DESC, transition_id DESC
) latest
WHERE to_state = ANY(:active_states)
"""


async def query_active_pool_item_ids(
    db: AsyncSession,
) -> list[str]:
    """返回活跃池 item_id 列表（排除 QUARANTINED/RETIRED）.

    活跃池 = 最新 transition 的 to_state ∈ {ACTIVE, WATCH}（验收 §3）。
    无任何 transition 的 item 不在活跃池（未初始化，需先 transition 到 ACTIVE）。
    """
    rows = (
        await db.execute(
            text(_ACTIVE_POOL_SQL),
            {"active_states": sorted(ACTIVE_POOL_STATES)},
        )
    ).fetchall()
    return [r[0] for r in rows]


__all__ = [
    "HEALTH_MIN_SAMPLE",
    "CORRECT_RATE_TOO_HIGH",
    "CORRECT_RATE_TOO_LOW",
    "LOW_DISCRIMINATION",
    "TIME_TOO_FAST_MS",
    "TIME_TOO_SLOW_MS",
    "ANOMALY_PENALTY",
    "GATE_CERT_REQUIRED_STATES",
    "HealthReport",
    "LifecycleTransitionError",
    "evaluate_health",
    "get_current_state",
    "transition_lifecycle",
    "query_active_pool_item_ids",
]
