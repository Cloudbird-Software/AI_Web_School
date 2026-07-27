"""W3-S4 评分执行服务：scoring_ref 调度 + 错误推断装配 + response_event 落账.

落地架构 v2 §4.5 评分域的在线链路：
  response → score（注册表评分器）→ dimension_scores + error_inferences
  → 经 W1 record_event 落 response_event（契约 events/response_event.md）。

职责边界：
- 评分器调度：按 item_version.scoring_ref.scorer_id 从 scorer 注册表取实现
  （学科桶优先、platform 回退；registry.get_scorer）。
- 选择题错误推断：选项→error_type 确定映射（模板 distractor_rules 的产物
  error_bindings，架构 §4.5「选择题=选项→错误类型确定映射」）。映射规则
  随 item_version 内容寻址版本化——rule_version 即 item_version_id。
- 落账：scoring_trace（契约 §3）+ error_inferences（契约 §4）经 record_event
  append-only 写入；本服务不做任何 UPDATE/DELETE。

置信度四层分离（§4.5）：scoring_trace.confidence 只承载评分层（与识别层，
识别层仅拍照链路有）；推断层置信度在 error_inferences[].confidence；
禁止混为单一「AI 置信度」。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping, Optional
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.events.writer import Scene, record_event
from src.core.scoring.registry import ScoreResult, get_scorer


# 选择题「选某项→错误类型」推断的默认置信度。
# 为什么 <1.0：选项→错误类型的映射是确定的（distractor_rules 设计时绑定），
# 但「选了某干扰项 → 持有该错误理解」是证据非因果的推断（架构 §4.5 原文），
# 推断层置信度须如实 <1；error_bindings 条目可用 confidence 键覆盖默认值。
DEFAULT_OPTION_INFER_CONFIDENCE = 0.9

# 会做选项→错误类型映射的交互类型（选择题族）
_CHOICE_INTERACTIONS = frozenset({"single_choice", "multi_choice"})


class ScorerNotRegisteredError(ValueError):
    """item_version.scoring_ref 指向的评分器未注册/未加载."""


class ScoringOutcome(BaseModel):
    """评分执行产出（落账后返回给调用方）.

    - event_id：response_event 事件 id（append-only 账的索引）。
    - correct：对错汇总（dimension_scores['correct'] >= 1.0 为对；
      部分分 <1.0 一律记错——错题回测标记的判定口径）。
    - dimension_scores / error_inferences / scoring_trace：与落账内容一致。
    """

    model_config = ConfigDict(extra="forbid")

    event_id: UUID
    correct: bool
    dimension_scores: dict[str, float]
    error_inferences: list[dict[str, Any]]
    scoring_trace: dict[str, Any]


def _get(obj: Any, key: str, default: Any = None) -> Any:
    """从 dict 或对象取属性（兼容 ORM/Pydantic/dict 三态）."""
    if isinstance(obj, Mapping):
        return obj.get(key, default)
    return getattr(obj, key, default)


def run_scorer(
    item_version: Any,
    response: Any,
    *,
    pack_id: str | None = None,
) -> ScoreResult:
    """按 item_version.scoring_ref 调度注册表评分器并执行.

    Args:
        item_version: 题目版本快照（ORM/Pydantic/dict 三态均可）。
        response: 学生作答（结构由交互类型 response_schema 保证）。
        pack_id: 学科包 id（学科桶优先查找；None 只查 platform 桶）。

    Returns:
        ScoreResult（五要素，scorer.yaml unified_contract）。

    Raises:
        ScorerNotRegisteredError: 评分器未注册（含学科包评分器未加载）。
    """
    scoring_ref = _get(item_version, "scoring_ref") or {}
    scorer_id = _get(scoring_ref, "scorer_id")
    if not scorer_id:
        raise ScorerNotRegisteredError("item_version.scoring_ref 缺 scorer_id")
    params = _get(scoring_ref, "scorer_params") or {}
    try:
        scorer = get_scorer(str(scorer_id), pack_id)
    except KeyError as e:
        raise ScorerNotRegisteredError(str(e)) from e
    result = scorer.score(response, item_version, params)
    # 鸭子类型归一：学科包实现（如 math_equivalence）返回自有 ScoreResult 类，
    # 字段与核心 ScoreResult 同构，按属性读取后落核心模型统一出口。
    return ScoreResult(
        dimension_scores=dict(result.dimension_scores),
        error_inferences=[dict(x) for x in result.error_inferences],
        confidence=dict(result.confidence),
        evidence=dict(result.evidence),
        scorer_version=str(result.scorer_version),
    )


def infer_option_errors(
    item_version: Any,
    response: Any,
) -> list[dict[str, Any]]:
    """选择题选项→错误类型映射（架构 §4.5：模板 distractor_rules 确定映射）.

    从 item_version.error_bindings（A 线引擎按 distractor_rules 生成：
    {option_value, label, error_type_id, ...}）查找被选中选项绑定的
    error_type_id。rule_version = item_version_id（映射规则随内容寻址
    版本化——同内容必同映射，重放可复现，R-D-05）。

    Returns:
        推断条目列表（契约 §4 结构）；非选择题交互或无命中返回空列表。
    """
    interaction_ref = _get(item_version, "interaction_ref") or {}
    interaction_id = _get(interaction_ref, "interaction_id")
    if interaction_id not in _CHOICE_INTERACTIONS:
        return []

    selected = _get(response, "selected")
    if selected is None:
        return []
    selected_values = (
        [str(x) for x in selected] if isinstance(selected, (list, tuple))
        else [str(selected)]
    )

    bindings = _get(item_version, "error_bindings") or []
    item_version_id = str(_get(item_version, "item_version_id", ""))
    inferences: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for binding in bindings:
        option_value = _get(binding, "option_value")
        error_type_id = _get(binding, "error_type_id")
        if option_value is None or not error_type_id:
            continue
        for value in selected_values:
            key = (value, str(error_type_id))
            if str(option_value) == value and key not in seen:
                seen.add(key)
                inferences.append({
                    "error_type_id": str(error_type_id),
                    "confidence": float(
                        _get(binding, "confidence", DEFAULT_OPTION_INFER_CONFIDENCE)
                    ),
                    "rule_version": item_version_id,
                    "evidence": {
                        "selected_option": value,
                        "label": _get(binding, "label"),
                    },
                })
    return inferences


def build_scoring_trace(scorer_id: str, result: ScoreResult) -> dict[str, Any]:
    """装配契约 §3 scoring_trace（评分轨迹）.

    confidence 只承载评分层（+可选识别层，拍照链路传入）；
    推断层置信度在 error_inferences[].confidence，不在此混合（§4.5 四层分离）。

    dimension_scores 随轨迹落账（契约 §3 为可扩展对象，只增不改）：
    S8 CTT 标定的正确性信号取数位置正是
    scoring_trace->'dimension_scores'->>'correct'（见 src/core/data/ctt.py）——
    缺了它，在线作答事件对参数标定不可见，数据飞轮断链。
    """
    confidence: dict[str, Any] = {
        "scoring": float(result.confidence.get("scoring", 1.0)),
        "note": "评分层置信度；推断层见 error_inferences[].confidence（§4.5 四层分离）",
    }
    if "recognition" in result.confidence:
        confidence["recognition"] = float(result.confidence["recognition"])
    return {
        "scorer_id": scorer_id,
        "scorer_version": result.scorer_version,
        "dimension_scores": dict(result.dimension_scores),
        "process": result.evidence,
        "confidence": confidence,
    }


async def score_and_record(
    db: AsyncSession,
    *,
    item_version: Any,
    response: dict[str, Any],
    student_alias_id: UUID,
    scene: Scene,
    pack_id: str | None = None,
    duration_ms: Optional[int] = None,
    session_id: Optional[UUID] = None,
    testlet_id: Optional[str] = None,
    source_ref: Optional[dict[str, Any]] = None,
    now: Optional[datetime] = None,
    event_id: Optional[UUID] = None,
) -> ScoringOutcome:
    """评分执行 + response_event 落账（W3-S4 主入口）.

    流程：run_scorer（注册表调度）→ infer_option_errors（选择题映射）合并
    评分器自报推断（去重）→ build_scoring_trace → record_event（append-only）。

    Args:
        db: 异步会话。
        item_version: 题目版本快照（ORM/Pydantic/dict）。
        response: 原始作答载荷（R-D-01：存作答内容本身）。
        student_alias_id: 匿名学生 id（D7）。
        scene: 场景三值（D5 禁止混估）。
        pack_id: 学科包 id（评分器学科桶查找）。
        duration_ms: 作答耗时；NULL=未知（禁止填 0 冒充，契约 §1）。
        session_id: 作答会话 id；NULL=无会话（纸卷回录场景）。
        testlet_id: 题组 id（R-Z-06）。
        source_ref: 来源追溯 {paper_id, placement_token} 或 {assembly_run_id}。
        now: 事件时间戳（UTC；None=当前时间；测试注入保证确定性）。
        event_id: 幂等键（None=uuid4；调用方可注入保证重试幂等）。

    Returns:
        ScoringOutcome（含 event_id 与落账内容）。
    """
    scoring_ref = _get(item_version, "scoring_ref") or {}
    scorer_id = str(_get(scoring_ref, "scorer_id"))

    result = run_scorer(item_version, response, pack_id=pack_id)

    # 错误推断合并：评分器自报（math_equivalence/keypoint_hit 等）
    # + 选择题选项映射；按 (error_type_id, evidence) 去重。
    error_inferences: list[dict[str, Any]] = []
    seen: set[str] = set()
    for inf in list(result.error_inferences) + infer_option_errors(item_version, response):
        dedup_key = f"{inf.get('error_type_id')}|{inf.get('evidence')}"
        if dedup_key in seen:
            continue
        seen.add(dedup_key)
        error_inferences.append(inf)

    scoring_trace = build_scoring_trace(scorer_id, result)
    correct = float(result.dimension_scores.get("correct", 0.0)) >= 1.0

    eid = event_id or uuid4()
    await record_event(
        db,
        event_id=eid,
        student_alias_id=student_alias_id,
        item_version_id=str(_get(item_version, "item_version_id")),
        scene=scene,
        raw_payload=dict(response),
        scoring_trace=scoring_trace,
        error_inferences=error_inferences,
        created_at=now or datetime.now(timezone.utc),
        duration_ms=duration_ms,
        testlet_id=testlet_id,
        session_id=session_id,
        source_ref=source_ref,
    )

    return ScoringOutcome(
        event_id=eid,
        correct=correct,
        dimension_scores=dict(result.dimension_scores),
        error_inferences=error_inferences,
        scoring_trace=scoring_trace,
    )


__all__ = [
    "DEFAULT_OPTION_INFER_CONFIDENCE",
    "ScorerNotRegisteredError",
    "ScoringOutcome",
    "build_scoring_trace",
    "infer_option_errors",
    "run_scorer",
    "score_and_record",
]
