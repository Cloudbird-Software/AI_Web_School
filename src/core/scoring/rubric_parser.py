"""T-W4-019 量规解析器：量规模板 → 评分 prompt + AI 响应解析.

落地架构 v2 §4.5「AI 维度量规评分器」的解析侧：量规即数据（T-W4-017
RubricTemplate）→ 评分 prompt（强模型按量规打分+逐维理由）→ 解析 AI 响应
为 ``AIRubricScore``（逐维分数+理由+置信度）。

对齐契约：
- 输入侧 ``specs/contracts/registries/scorer.yaml`` 的
  ``ai_rubric.params_schema.rubric``（``{dimensions:[{id,name,anchors,score_bands,
  error_type_rules}]}``）—— 既是 ``RubricTemplate.to_scorer_params()`` 的输出，
  也是 ``AIRubricScorer`` 的 ``params['rubric']`` 输入。
- 输出侧任务卡 T-W4-019 验收①：``{dimensions:[{name,score,max,rationale,confidence}],
  total_score,total_max,overall_confidence}``。

宪法 A5/X6：本模块不 import 任何学科包/学段包；量规数据中性，无学科语义。
"""
from __future__ import annotations

import json
import re
from typing import Any

from src.core.production.rubric_template import GradeBand, RubricTemplate

# 学段中文名（prompt 上下文提示，量规分值不变，仅提示模型学段语义）
_GRADE_BAND_LABEL: dict[str, str] = {
    "L": "低段（小学 1-2 年级）",
    "M": "中段（小学 3-4 年级）",
    "H": "高段（小学 5-6 年级）",
}

# AI 响应置信度阈值（< 此值标记待人工复核，任务卡验收②）
HUMAN_REVIEW_CONFIDENCE_THRESHOLD: float = 0.6

# 量规维度数量上限（防 prompt 膨胀与解析歧义）
_MAX_DIMENSIONS: int = 16


# ────────────────────────────────────────────────────────────────────
# 量规解析：RubricTemplate / dict → ParsedRubric（评分器消费的中性结构）
# ────────────────────────────────────────────────────────────────────


class ParsedDimension:
    """已解析的单维度（中性结构，无 Pydantic 强制以避免循环依赖）.

    Attributes:
        id: 维度 id（snake_case，落 dimension_scores 键）.
        name: 维度中文名（prompt 与结果展示用）.
        anchors: 等级行为锚点描述列表（按 level 升序）.
        score_bands: ``[{level, label, score}]`` 分值带（按 level 升序）.
        max_score: 该维度满分（= max(score_bands.score)）.
        error_type_rules: 维度得分→错误类型规则表（透传，可空）.
    """

    __slots__ = ("id", "name", "anchors", "score_bands", "max_score", "error_type_rules")

    def __init__(
        self,
        *,
        id: str,
        name: str,
        anchors: list[str],
        score_bands: list[dict[str, Any]],
        max_score: float,
        error_type_rules: list[dict[str, Any]] | None = None,
    ) -> None:
        self.id = id
        self.name = name
        self.anchors = anchors
        self.score_bands = score_bands
        self.max_score = max_score
        self.error_type_rules = error_type_rules or []


class ParsedRubric:
    """已解析的量规（评分器消费的中性结构）.

    Attributes:
        dimensions: 已解析维度列表（按输入顺序）.
        total_max_score: 分值合计（= sum(dimensions.max_score)）.
    """

    __slots__ = ("dimensions", "total_max_score")

    def __init__(
        self, *, dimensions: list[ParsedDimension], total_max_score: float
    ) -> None:
        self.dimensions = dimensions
        self.total_max_score = total_max_score


def parse_rubric(rubric: RubricTemplate | dict[str, Any]) -> ParsedRubric:
    """解析量规模板为评分器消费的中性结构.

    支持两种输入（验收③：可序列化为 JSON 被评分器解析执行）：
    - ``RubricTemplate``（T-W4-017 Pydantic 模型）：内部 ``to_scorer_params()`` 后解析.
    - ``dict``：对齐 ``scorer.yaml ai_rubric.params_schema.rubric`` 结构
      （``{dimensions:[{id,name,anchors,score_bands,error_type_rules}], total_max_score}``）.

    Args:
        rubric: 量规模板（Pydantic 实例或 dict）.

    Returns:
        ParsedRubric.

    Raises:
        ValueError: 量规结构非法（缺 dimensions / 维度缺字段 / 等级为空）.
    """
    if isinstance(rubric, RubricTemplate):
        data = rubric.to_scorer_params()
    elif isinstance(rubric, dict):
        data = rubric
    else:
        raise ValueError(
            f"rubric 必须为 RubricTemplate 或 dict，实际为 {type(rubric).__name__}"
        )

    raw_dims = data.get("dimensions")
    if not isinstance(raw_dims, list) or not raw_dims:
        raise ValueError(
            f"量规缺 dimensions 或非 list：{type(raw_dims).__name__}"
        )
    if len(raw_dims) > _MAX_DIMENSIONS:
        raise ValueError(
            f"量规维度数 {len(raw_dims)} 超过上限 {_MAX_DIMENSIONS}（防 prompt 膨胀）"
        )

    parsed_dims: list[ParsedDimension] = []
    for i, dim in enumerate(raw_dims):
        if not isinstance(dim, dict):
            raise ValueError(f"维度 #{i} 非 dict：{type(dim).__name__}")
        dim_id = dim.get("id")
        dim_name = dim.get("name")
        anchors = dim.get("anchors")
        score_bands = dim.get("score_bands")
        if not dim_id or not isinstance(dim_id, str):
            raise ValueError(f"维度 #{i} 缺 id 或非 string")
        if not dim_name or not isinstance(dim_name, str):
            raise ValueError(f"维度 #{i} {dim_id!r} 缺 name 或非 string")
        if not isinstance(anchors, list) or not anchors:
            raise ValueError(
                f"维度 #{i} {dim_id!r} anchors 必须为非空 list"
            )
        if not isinstance(score_bands, list) or not score_bands:
            raise ValueError(
                f"维度 #{i} {dim_id!r} score_bands 必须为非空 list"
            )
        # max_score = max(score_bands[*].score)
        try:
            max_score = max(float(sb.get("score", 0.0)) for sb in score_bands)
        except (TypeError, ValueError) as e:
            raise ValueError(
                f"维度 #{i} {dim_id!r} score_bands[*].score 非数值：{e}"
            ) from e
        parsed_dims.append(
            ParsedDimension(
                id=dim_id,
                name=dim_name,
                anchors=[str(a) for a in anchors],
                score_bands=score_bands,
                max_score=max_score,
                error_type_rules=dim.get("error_type_rules") or [],
            )
        )

    total_max = data.get("total_max_score")
    if total_max is None:
        # 兼容老式输入：未声明 total_max_score 时按维度合计
        total_max = sum(d.max_score for d in parsed_dims)
    else:
        total_max = float(total_max)

    return ParsedRubric(dimensions=parsed_dims, total_max_score=total_max)


# ────────────────────────────────────────────────────────────────────
# 评分 prompt 构建
# ────────────────────────────────────────────────────────────────────


def build_scoring_prompt(
    response_text: str,
    parsed: ParsedRubric,
    grade_band: GradeBand,
) -> str:
    """构建评分 prompt（强模型按量规打分+逐维理由）.

    Prompt 结构（中文，量规与作答均中文）：
    1. 角色与任务说明（作文/看图写话评分器）；
    2. 学段上下文（影响评分宽松度提示，不改变量规分值）；
    3. 量规描述（逐维度：名称+满分+各等级锚点+分值带）；
    4. 学生作答原文（可能含 PII，由总线 pii_filter 在调用 client 前剥离 D7）；
    5. 输出格式约束（严格 JSON，逐维 score/rationale/confidence）.

    Args:
        response_text: 学生作答文本.
        parsed: 已解析量规.
        grade_band: 学段 L/M/H.

    Returns:
        评分 prompt 字符串.
    """
    band_label = _GRADE_BAND_LABEL.get(grade_band, str(grade_band))

    lines: list[str] = [
        "你是小学语文作文/看图写话评分器。请严格按下列量规对学生作答逐维度评分。",
        f"【学段】{band_label}",
        "",
        "【量规】",
    ]
    for dim in parsed.dimensions:
        lines.append(f"- 维度「{dim.name}」（id={dim.id}，满分 {dim.max_score}）")
        for j, anchor in enumerate(dim.anchors):
            sb = (
                dim.score_bands[j]
                if j < len(dim.score_bands)
                else {}
            )
            label = sb.get("label", "?")
            score = sb.get("score", "?")
            lines.append(f"    等级{j + 1}（{label}，{score}分）：{anchor}")
    lines.extend(
        [
            "",
            "【学生作答】",
            response_text,
            "",
            "【输出要求】",
            "请输出严格 JSON（无注释、无 markdown 围栏），结构如下：",
            "{",
            '  "dimensions": [',
            '    {"id": "<维度id>", "score": <分数>, "rationale": "<理由>", "confidence": <0-1>}',
            "  ]",
            "}",
            "约束：score 必须在该维度分值带内；rationale 必须非空且引用具体锚点；",
            "confidence 为对该维度评分的置信度，0.0=完全不确定 / 1.0=完全确定。",
            "只输出 JSON，不要任何其他文字。",
        ]
    )
    return "\n".join(lines)


# ────────────────────────────────────────────────────────────────────
# AI 响应解析
# ────────────────────────────────────────────────────────────────────


# 容错：AI 输出可能含 ```json ... ``` 围栏，提取首个 JSON 对象
_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


class AIRubricScoreDimension:
    """AI 评分结果的单维度（任务卡验收①字段：name/score/max/rationale/confidence）.

    Attributes:
        name: 维度中文名（来自量规，便于教研展示）.
        score: 该维度得分（在该维度分值带内）.
        max: 该维度满分.
        rationale: 评分理由（非空）.
        confidence: 该维度评分置信度（0-1 连续值）.
    """

    __slots__ = ("name", "score", "max", "rationale", "confidence")

    def __init__(
        self,
        *,
        name: str,
        score: float,
        max: float,
        rationale: str,
        confidence: float,
    ) -> None:
        self.name = name
        self.score = score
        self.max = max
        self.rationale = rationale
        self.confidence = confidence

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "score": self.score,
            "max": self.max,
            "rationale": self.rationale,
            "confidence": self.confidence,
        }


class AIRubricScore:
    """AI 量规评分结果（任务卡验收①字段）.

    Attributes:
        dimensions: 逐维评分（含 name/score/max/rationale/confidence）.
        total_score: 总分（= sum(dimensions.score)）.
        total_max: 总满分（= sum(dimensions.max) = rubric.total_max_score）.
        overall_confidence: 整体置信度（= min(dimensions.confidence)）.
        needs_human_review: 是否需要人工复核（overall_confidence < 阈值 或解析失败）.
    """

    __slots__ = (
        "dimensions",
        "total_score",
        "total_max",
        "overall_confidence",
        "needs_human_review",
    )

    def __init__(
        self,
        *,
        dimensions: list[AIRubricScoreDimension],
        total_score: float,
        total_max: float,
        overall_confidence: float,
        needs_human_review: bool = False,
    ) -> None:
        self.dimensions = dimensions
        self.total_score = total_score
        self.total_max = total_max
        self.overall_confidence = overall_confidence
        self.needs_human_review = needs_human_review

    def to_dict(self) -> dict[str, Any]:
        """转 dict（验收①契约：dimensions/total_score/total_max/overall_confidence）."""
        return {
            "dimensions": [d.to_dict() for d in self.dimensions],
            "total_score": self.total_score,
            "total_max": self.total_max,
            "overall_confidence": self.overall_confidence,
            "needs_human_review": self.needs_human_review,
        }


def parse_ai_response(
    content: str,
    parsed: ParsedRubric,
) -> AIRubricScore:
    """解析 AI 返回的 JSON 为 ``AIRubricScore``.

    容错策略：
    - 优先 ``json.loads`` 整段；
    - 失败则正则提取首个 ``{...}`` 再 parse（兼容 markdown 围栏）；
    - 仍失败 → 返回零分、overall_confidence=0、needs_human_review=True（验收②低置信复核）.

    字段缺失/越界处理：
    - 缺维度 → 该维度 score=0 / confidence=0 / rationale='AI 未返回该维度评分'；
    - score 超出分值带 → clamp 到 [min(score_bands), max(score_bands)]；
    - confidence 超出 [0,1] → clamp；
    - rationale 为空 → 替换为占位理由并降低 confidence.

    Args:
        content: AI 输出文本（期望严格 JSON）.
        parsed: 已解析量规（用于校验维度 id 与分值带）.

    Returns:
        AIRubricScore.
    """
    dim_by_id = {d.id: d for d in parsed.dimensions}

    data: dict[str, Any] | None = None
    parse_error: str | None = None
    try:
        data = json.loads(content)
        if not isinstance(data, dict):
            data = None
            parse_error = "顶层非 object"
    except (json.JSONDecodeError, TypeError) as e:
        parse_error = str(e)
        # 容错：提取首个 {...}
        m = _JSON_OBJECT_RE.search(content)
        if m:
            try:
                candidate = json.loads(m.group(0))
                if isinstance(candidate, dict):
                    data = candidate
                    parse_error = None
            except (json.JSONDecodeError, TypeError) as e2:
                parse_error = f"{parse_error} / 提取后仍失败：{e2}"

    if data is None:
        # 解析完全失败：返回零分 + 低置信 + 人工复核标记
        zero_dims = [
            AIRubricScoreDimension(
                name=d.name,
                score=0.0,
                max=d.max_score,
                rationale=f"AI 响应解析失败：{parse_error}",
                confidence=0.0,
            )
            for d in parsed.dimensions
        ]
        return AIRubricScore(
            dimensions=zero_dims,
            total_score=0.0,
            total_max=parsed.total_max_score,
            overall_confidence=0.0,
            needs_human_review=True,
        )

    raw_dims = data.get("dimensions")
    if not isinstance(raw_dims, list):
        raw_dims = []

    # 按 parsed 量规顺序对齐（保证返回结构稳定）
    score_by_id: dict[str, dict[str, Any]] = {}
    for rd in raw_dims:
        if not isinstance(rd, dict):
            continue
        rid = rd.get("id")
        if isinstance(rid, str) and rid in dim_by_id:
            score_by_id[rid] = rd

    out_dims: list[AIRubricScoreDimension] = []
    total_score = 0.0
    min_conf = 1.0
    for dim in parsed.dimensions:
        rd = score_by_id.get(dim.id)
        if rd is None:
            # AI 漏评该维度
            out_dims.append(
                AIRubricScoreDimension(
                    name=dim.name,
                    score=0.0,
                    max=dim.max_score,
                    rationale="AI 未返回该维度评分",
                    confidence=0.0,
                )
            )
            min_conf = 0.0
            continue

        # score clamp 到分值带
        try:
            score = float(rd.get("score", 0.0))
        except (TypeError, ValueError):
            score = 0.0
        band_scores = [float(sb.get("score", 0.0)) for sb in dim.score_bands]
        if band_scores:
            score = max(min(score, max(band_scores)), min(band_scores))

        # rationale 非空（验收②）
        rationale = str(rd.get("rationale") or "").strip()
        if not rationale:
            rationale = "AI 未给出理由（rationale 为空）"

        # confidence clamp [0,1]
        try:
            confidence = float(rd.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
        confidence = max(0.0, min(1.0, confidence))
        if not rationale or rationale == "AI 未给出理由（rationale 为空）":
            # 理由缺失降低置信度
            confidence = min(confidence, 0.3)

        out_dims.append(
            AIRubricScoreDimension(
                name=dim.name,
                score=score,
                max=dim.max_score,
                rationale=rationale,
                confidence=confidence,
            )
        )
        total_score += score
        min_conf = min(min_conf, confidence)

    overall = min_conf
    needs_review = overall < HUMAN_REVIEW_CONFIDENCE_THRESHOLD

    return AIRubricScore(
        dimensions=out_dims,
        total_score=total_score,
        total_max=parsed.total_max_score,
        overall_confidence=overall,
        needs_human_review=needs_review,
    )


__all__ = [
    "AIRubricScore",
    "AIRubricScoreDimension",
    "HUMAN_REVIEW_CONFIDENCE_THRESHOLD",
    "ParsedDimension",
    "ParsedRubric",
    "build_scoring_prompt",
    "parse_ai_response",
    "parse_rubric",
]
