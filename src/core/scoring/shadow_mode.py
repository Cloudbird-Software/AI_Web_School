"""T-W4-020 影子模式框架 + 基准验证.

落地架构 v2 §4.5「AI 维度量规评分器」上线四步之第二步「影子运行」：
- 调 ``AIRubricScorer``（T-W4-019）对模拟作答打分；
- 结果写入 ``shadow_score`` 表（迁移 0019），**不触碰** ``response_event`` 主
  score 字段（验收①：不影响真实分数）；
- 与基准数据集（``tests/golden/shadow_dataset.json`` 20 篇 + 人工量规结论）
  对比，计算一致率（验收③：逐维偏差≤1 分视为一致，整体一致率≥70%）；
- 上线四步只到影子模式（公开基准验证→影子运行），不抽检、不灰度.

为什么不复用 score_run（T-W4-003）：score_run 是「重判平行账」，绑 event_id
（FK→response_event）；shadow_score 是「AI 量规自验」，作答可能是合成的基准
数据集，无 event_id 可绑。语义不同，独立表.

宪法 A5/X6：本模块不 import 任何学科包/学段包.
宪法 D1/D8：shadow_score 是 append-only；不输出排名（一致率是内部验证指标）.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.production.rubric_template import GradeBand, RubricTemplate
from src.core.scoring.ai_rubric_scorer import score as ai_rubric_score
from src.core.scoring.rubric_parser import (
    AIRubricScore,
    HUMAN_REVIEW_CONFIDENCE_THRESHOLD,
    parse_rubric,
)

# 默认基准数据集路径（tests/golden/shadow_dataset.json）
DEFAULT_DATASET_PATH: Path = (
    Path(__file__).resolve().parent.parent.parent.parent
    / "tests"
    / "golden"
    / "shadow_dataset.json"
)

# 一致性判定阈值（验收③：5 分制逐维偏差≤1 分视为一致）
DEFAULT_CONSISTENCY_TOLERANCE: float = 1.0

# 一致率门槛（验收③：整体一致率≥70%）
DEFAULT_CONSISTENCY_RATE_THRESHOLD: float = 0.70

# 默认基准数据集 id
DEFAULT_DATASET_ID: str = "shadow-baseline-v1"


# ────────────────────────────────────────────────────────────────────
# 一致性计算（验收③：AI 量规 vs 人工结论）
# ────────────────────────────────────────────────────────────────────


class DimensionComparison:
    """单维度对比结果.

    Attributes:
        dimension_id: 维度 id.
        ai_score: AI 评分.
        human_score: 人工评分.
        delta: |ai_score - human_score|.
        consistent: delta ≤ tolerance.
    """

    __slots__ = ("dimension_id", "ai_score", "human_score", "delta", "consistent")

    def __init__(
        self,
        *,
        dimension_id: str,
        ai_score: float,
        human_score: float,
        delta: float,
        consistent: bool,
    ) -> None:
        self.dimension_id = dimension_id
        self.ai_score = ai_score
        self.human_score = human_score
        self.delta = delta
        self.consistent = consistent


class ConsistencyResult:
    """单 case 一致性结果.

    Attributes:
        case_id: 数据集 case id.
        consistent: 所有维度均一致（delta ≤ tolerance）.
        dimensions: 逐维对比详情.
        max_delta: 最大偏差（用于诊断）.
    """

    __slots__ = ("case_id", "consistent", "dimensions", "max_delta")

    def __init__(
        self,
        *,
        case_id: str,
        consistent: bool,
        dimensions: list[DimensionComparison],
        max_delta: float,
    ) -> None:
        self.case_id = case_id
        self.consistent = consistent
        self.dimensions = dimensions
        self.max_delta = max_delta

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "consistent": self.consistent,
            "max_delta": self.max_delta,
            "dimensions": [
                {
                    "dimension_id": d.dimension_id,
                    "ai_score": d.ai_score,
                    "human_score": d.human_score,
                    "delta": d.delta,
                    "consistent": d.consistent,
                }
                for d in self.dimensions
            ],
        }


def compute_consistency(
    ai_score: AIRubricScore,
    human_score: dict[str, Any],
    *,
    tolerance: float = DEFAULT_CONSISTENCY_TOLERANCE,
) -> bool:
    """计算 AI 评分与人工结论是否逐维一致（验收③）.

    判定规则：所有维度 |ai_score - human_score| ≤ tolerance → 一致.

    Args:
        ai_score: AI 量规评分结果.
        human_score: 人工量规结论 dict（``{dimensions:[{id, score, ...}]}``）.
        tolerance: 允许的逐维偏差（默认 1.0，5 分制）.

    Returns:
        True 若所有维度偏差均 ≤ tolerance.
    """
    human_by_id: dict[str, float] = {}
    for hd in human_score.get("dimensions", []) or []:
        if isinstance(hd, dict) and "id" in hd:
            try:
                human_by_id[str(hd["id"])] = float(hd.get("score", 0.0))
            except (TypeError, ValueError):
                human_by_id[str(hd["id"])] = 0.0

    # AI 评分的 dimensions 顺序对应 ParsedRubric.dimensions，但 id 不在 AIRubricScoreDimension
    # 直接字段里——这里通过 ai_score.dimensions 与 human dimensions 按「顺序对齐」对比
    # （数据集中人工 dimensions 顺序与量规 dimensions 一致，见 shadow_dataset.json）。
    human_dims = human_score.get("dimensions", []) or []
    for i, ai_dim in enumerate(ai_score.dimensions):
        if i >= len(human_dims):
            return False  # 人工结论缺维度 → 不一致
        hd = human_dims[i]
        if not isinstance(hd, dict):
            return False
        try:
            h_score = float(hd.get("score", 0.0))
        except (TypeError, ValueError):
            h_score = 0.0
        if abs(ai_dim.score - h_score) > tolerance:
            return False
    return True


def _compute_consistency_detail(
    ai_score: AIRubricScore,
    human_score: dict[str, Any],
    case_id: str,
    *,
    tolerance: float = DEFAULT_CONSISTENCY_TOLERANCE,
) -> ConsistencyResult:
    """计算一致性详情（含逐维对比，用于基准报告）."""
    human_dims = human_score.get("dimensions", []) or []
    comparisons: list[DimensionComparison] = []
    max_delta = 0.0
    for i, ai_dim in enumerate(ai_score.dimensions):
        if i < len(human_dims) and isinstance(human_dims[i], dict):
            hd = human_dims[i]
            dim_id = str(hd.get("id", f"dim{i}"))
            try:
                h_score = float(hd.get("score", 0.0))
            except (TypeError, ValueError):
                h_score = 0.0
        else:
            dim_id = f"dim{i}"
            h_score = 0.0
        delta = abs(ai_dim.score - h_score)
        max_delta = max(max_delta, delta)
        comparisons.append(
            DimensionComparison(
                dimension_id=dim_id,
                ai_score=ai_dim.score,
                human_score=h_score,
                delta=delta,
                consistent=delta <= tolerance,
            )
        )
    consistent = all(c.consistent for c in comparisons) if comparisons else False
    return ConsistencyResult(
        case_id=case_id,
        consistent=consistent,
        dimensions=comparisons,
        max_delta=max_delta,
    )


# ────────────────────────────────────────────────────────────────────
# 影子评分主入口：shadow_score
# ────────────────────────────────────────────────────────────────────


def _sha256_text(s: str) -> str:
    """sha256 of text（用于 response_text_digest，dedup/replay）."""
    return "sha256:" + hashlib.sha256(s.encode("utf-8")).hexdigest()


class ShadowScoreRecord:
    """影子评分记录（对应 shadow_score 表一行）.

    Attributes:
        shadow_id: 行 id（应用层 ULID 或派生）.
        dataset_id: 数据集 id.
        case_id: case id.
        rubric_id: 量规 id.
        grade_band: 学段.
        writing_type: 写作类型.
        response_text: 被评分作答文本.
        response_text_digest: 作答文本 sha256.
        ai_score: AIRubricScore.
        human_score: 人工量规结论（可空）.
        consistency_status: pending/consistent/inconsistent.
    """

    __slots__ = (
        "shadow_id", "dataset_id", "case_id", "rubric_id",
        "grade_band", "writing_type", "response_text", "response_text_digest",
        "ai_score", "human_score", "consistency_status",
    )

    def __init__(
        self,
        *,
        shadow_id: str,
        dataset_id: str,
        case_id: str,
        rubric_id: str,
        grade_band: str,
        writing_type: str,
        response_text: str,
        response_text_digest: str,
        ai_score: AIRubricScore,
        human_score: dict[str, Any] | None = None,
        consistency_status: str = "pending",
    ) -> None:
        self.shadow_id = shadow_id
        self.dataset_id = dataset_id
        self.case_id = case_id
        self.rubric_id = rubric_id
        self.grade_band = grade_band
        self.writing_type = writing_type
        self.response_text = response_text
        self.response_text_digest = response_text_digest
        self.ai_score = ai_score
        self.human_score = human_score
        self.consistency_status = consistency_status

    def to_dict(self) -> dict[str, Any]:
        return {
            "shadow_id": self.shadow_id,
            "dataset_id": self.dataset_id,
            "case_id": self.case_id,
            "rubric_id": self.rubric_id,
            "grade_band": self.grade_band,
            "writing_type": self.writing_type,
            "response_text": self.response_text,
            "response_text_digest": self.response_text_digest,
            "ai_score": self.ai_score.to_dict(),
            "human_score": self.human_score,
            "consistency_status": self.consistency_status,
        }


def shadow_score(
    response_text: str,
    rubric: RubricTemplate | dict[str, Any],
    grade_band: GradeBand,
    *,
    writing_type: str = "composition",
    case_id: str = "ad-hoc",
    dataset_id: str = "ad-hoc",
    clients: dict[str, Any] | None = None,
    bypass_pii_filter: bool = False,
    human_score: dict[str, Any] | None = None,
    tolerance: float = DEFAULT_CONSISTENCY_TOLERANCE,
) -> ShadowScoreRecord:
    """影子评分主入口（验收①：写入 shadow_score 表，不触碰 response_event）.

    流程：
      1. 调 ``ai_rubric_score``（T-W4-019）对作答打分；
      2. 若提供 ``human_score``，计算一致状态（pending/consistent/inconsistent）；
      3. 返回 ``ShadowScoreRecord``（调用方持 ``AsyncSession`` 时由 ``persist``
         落 ``shadow_score`` 表；本函数不直接写 DB，便于纯函数测试）.

    为什么本函数不直接写 DB：评分与持久化职责分离；评分纯函数化便于在无 DB
    环境（如基准验证脚本）运行；持久化由 ``persist_shadow_score`` 承载.

    Args:
        response_text: 被评分作答文本.
        rubric: 量规模板.
        grade_band: 学段 L/M/H.
        writing_type: composition / picture_writing.
        case_id: case id（基准数据集内 id；ad-hoc 场景默认 'ad-hoc'）.
        dataset_id: 数据集 id（默认 'ad-hoc'）.
        clients: 注入 LLM 客户端（测试用 mock）.
        bypass_pii_filter: 测试用绕过 PII 剥离.
        human_score: 人工量规结论（用于一致性判定；非基准场景为 None）.
        tolerance: 一致性判定阈值.

    Returns:
        ShadowScoreRecord.
    """
    ai_score = ai_rubric_score(
        response_text=response_text,
        rubric_template=rubric,
        grade_band=grade_band,
        clients=clients,
        bypass_pii_filter=bypass_pii_filter,
    )

    # 派生 shadow_id：dataset_id + case_id + response_text_digest（确定性，便于重放）
    response_digest = _sha256_text(response_text)
    rubric_id = (
        rubric.rubric_id
        if isinstance(rubric, RubricTemplate)
        else str(rubric.get("rubric_id", "ad-hoc-rubric"))
    )
    shadow_id = _sha256_text(
        f"{dataset_id}|{case_id}|{rubric_id}|{response_digest}"
    )

    consistency_status = "pending"
    if human_score is not None:
        is_consistent = compute_consistency(
            ai_score, human_score, tolerance=tolerance
        )
        consistency_status = "consistent" if is_consistent else "inconsistent"

    return ShadowScoreRecord(
        shadow_id=shadow_id,
        dataset_id=dataset_id,
        case_id=case_id,
        rubric_id=rubric_id,
        grade_band=grade_band,
        writing_type=writing_type,
        response_text=response_text,
        response_text_digest=response_digest,
        ai_score=ai_score,
        human_score=human_score,
        consistency_status=consistency_status,
    )


async def persist_shadow_score(
    session: AsyncSession,
    record: ShadowScoreRecord,
) -> None:
    """将影子评分记录落 ``shadow_score`` 表（验收①：写入影子表）.

    重复写入（同 shadow_id）由 append-only 触发器拒绝 UPDATE；如需重判应生成
    新 shadow_id（content-addressed，避免覆盖历史评分）.

    Args:
        session: AsyncSession.
        record: ``shadow_score`` 返回的记录.
    """
    await session.execute(
        text(
            "INSERT INTO shadow_score "
            "(shadow_id, dataset_id, case_id, rubric_id, grade_band, writing_type, "
            " response_text, response_text_digest, ai_score_payload, "
            " overall_confidence, needs_human_review, human_score_payload, "
            " consistency_status) "
            "VALUES "
            "(:sid, :did, :cid, :rid, :gb, :wt, :rt, :rtd, "
            " CAST(:aip AS JSONB), :oc, :nhr, CAST(:hsp AS JSONB), :cs)"
        ),
        {
            "sid": record.shadow_id,
            "did": record.dataset_id,
            "cid": record.case_id,
            "rid": record.rubric_id,
            "gb": record.grade_band,
            "wt": record.writing_type,
            "rt": record.response_text,
            "rtd": record.response_text_digest,
            "aip": json.dumps(record.ai_score.to_dict(), ensure_ascii=False),
            "oc": float(record.ai_score.overall_confidence),
            "nhr": bool(record.ai_score.needs_human_review),
            "hsp": (
                json.dumps(record.human_score, ensure_ascii=False)
                if record.human_score is not None
                else None
            ),
            "cs": record.consistency_status,
        },
    )


# ────────────────────────────────────────────────────────────────────
# 基准数据集 + 一致率报告
# ────────────────────────────────────────────────────────────────────


class BenchmarkReport:
    """基准验证报告（验收③：整体一致率≥70%）.

    Attributes:
        dataset_id: 数据集 id.
        total_cases: 总 case 数.
        consistent_cases: 一致 case 数.
        consistency_rate: 一致率（0-1）.
        passed: 一致率 ≥ 阈值（70%）.
        per_case: 逐 case 一致性详情.
    """

    __slots__ = (
        "dataset_id", "total_cases", "consistent_cases",
        "consistency_rate", "passed", "per_case",
    )

    def __init__(
        self,
        *,
        dataset_id: str,
        total_cases: int,
        consistent_cases: int,
        consistency_rate: float,
        passed: bool,
        per_case: list[ConsistencyResult],
    ) -> None:
        self.dataset_id = dataset_id
        self.total_cases = total_cases
        self.consistent_cases = consistent_cases
        self.consistency_rate = consistency_rate
        self.passed = passed
        self.per_case = per_case

    def to_dict(self) -> dict[str, Any]:
        return {
            "dataset_id": self.dataset_id,
            "total_cases": self.total_cases,
            "consistent_cases": self.consistent_cases,
            "consistency_rate": self.consistency_rate,
            "passed": self.passed,
            "threshold": DEFAULT_CONSISTENCY_RATE_THRESHOLD,
            "per_case": [c.to_dict() for c in self.per_case],
        }


def load_shadow_dataset(
    path: Path | str = DEFAULT_DATASET_PATH,
) -> dict[str, Any]:
    """加载基准数据集（``tests/golden/shadow_dataset.json``）.

    Args:
        path: 数据集路径（默认 tests/golden/shadow_dataset.json）.

    Returns:
        数据集 dict（含 dataset_id / rubric / cases[*]）.

    Raises:
        FileNotFoundError: 文件不存在.
        ValueError: 结构非法.
    """
    p = Path(path) if isinstance(path, str) else path
    if not p.is_file():
        raise FileNotFoundError(f"基准数据集不存在：{p}")
    with p.open(encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"数据集顶层非 object：{type(data)}")
    if "cases" not in data or not isinstance(data["cases"], list):
        raise ValueError("数据集缺 cases 或非 list")
    if len(data["cases"]) < 1:
        raise ValueError("数据集 cases 为空")
    return data


def benchmark_against_dataset(
    dataset: dict[str, Any],
    *,
    clients: dict[str, Any] | None = None,
    bypass_pii_filter: bool = True,
    tolerance: float = DEFAULT_CONSISTENCY_TOLERANCE,
    consistency_rate_threshold: float = DEFAULT_CONSISTENCY_RATE_THRESHOLD,
) -> BenchmarkReport:
    """对基准数据集跑影子评分 + 一致率计算（验收③）.

    Args:
        dataset: ``load_shadow_dataset`` 返回的数据集 dict.
        clients: 注入 LLM 客户端（测试用 mock；生产空走注册的/桩）.
        bypass_pii_filter: 测试用绕过 PII 剥离（基准数据集无真实 PII）.
        tolerance: 逐维一致性阈值（默认 1.0）.
        consistency_rate_threshold: 一致率门槛（默认 0.70）.

    Returns:
        BenchmarkReport：含逐 case 一致性与整体一致率.
    """
    dataset_id = str(dataset.get("dataset_id", DEFAULT_DATASET_ID))
    rubric = dataset.get("rubric")
    if not isinstance(rubric, dict):
        raise ValueError("数据集缺 rubric 或非 object")

    cases = dataset["cases"]
    per_case: list[ConsistencyResult] = []
    consistent_count = 0
    for case in cases:
        case_id = str(case.get("case_id", "unknown"))
        grade_band = case.get("grade_band", "M")
        writing_type = str(case.get("writing_type", "composition"))
        response_text = str(case.get("response_text", ""))
        human_score = case.get("human_score") or {}

        record = shadow_score(
            response_text=response_text,
            rubric=rubric,
            grade_band=grade_band,  # type: ignore[arg-type]
            writing_type=writing_type,
            case_id=case_id,
            dataset_id=dataset_id,
            clients=clients,
            bypass_pii_filter=bypass_pii_filter,
            human_score=human_score,
            tolerance=tolerance,
        )

        detail = _compute_consistency_detail(
            record.ai_score, human_score, case_id, tolerance=tolerance
        )
        per_case.append(detail)
        if detail.consistent:
            consistent_count += 1

    total = len(cases)
    rate = consistent_count / total if total > 0 else 0.0
    passed = rate >= consistency_rate_threshold

    return BenchmarkReport(
        dataset_id=dataset_id,
        total_cases=total,
        consistent_cases=consistent_count,
        consistency_rate=rate,
        passed=passed,
        per_case=per_case,
    )


__all__ = [
    "BenchmarkReport",
    "ConsistencyResult",
    "DEFAULT_CONSISTENCY_RATE_THRESHOLD",
    "DEFAULT_CONSISTENCY_TOLERANCE",
    "DEFAULT_DATASET_ID",
    "DEFAULT_DATASET_PATH",
    "DimensionComparison",
    "ShadowScoreRecord",
    "benchmark_against_dataset",
    "compute_consistency",
    "load_shadow_dataset",
    "persist_shadow_score",
    "shadow_score",
]
