"""T-W4-019 AI 维度量规评分器（scorer.yaml ai_rubric 落地）.

实现架构 v2 §4.5「AI 维度量规评分器」：解析量规模板 → 按维度构建评分 prompt →
经 S2 总线 L2/L3 档调用强模型 → 返回逐维分数 + 理由 + 置信度。总分与维度分
均返回，不输出排名（宪法 D8）。

对齐契约：
- ``scorer.yaml ai_rubric.params_schema.rubric`` —— 量规即数据（T-W4-017
  ``RubricTemplate.to_scorer_params()`` 输出），评分器不感知量规语义，只按
  维度/锚点/分值带让强模型打分。
- ``scorer.yaml unified_contract.output_schema`` —— ``ScoreResult`` 五要素
  （dimension_scores/error_inferences/confidence/evidence/scorer_version）。
- 任务卡验收①：``score(response_text, rubric_template, grade_band)`` 返回
  ``{dimensions:[{name,score,max,rationale,confidence}], total_score, total_max,
  overall_confidence}``。

AI 调用经 S2 总线（T-W4-007 ``ai_call``），PII 已剥离（T-W4-008 ``pii_filter``
在 router 入口完成），台账由 ledger 记录（T-W4-008）；本评分器不直接调用
任何 LLM 供应商 SDK（宪法 X6 等价约束）。

宪法 A5/X6：本模块不 import 任何学科包/学段包。
"""
from __future__ import annotations

from typing import Any, ClassVar

from src.core.ai.bus.router import ai_call
from src.core.production.rubric_template import GradeBand, RubricTemplate
from src.core.scoring.registry import ScoreResult, Scorer, register_scorer
from src.core.scoring.rubric_parser import (
    AIRubricScore,
    HUMAN_REVIEW_CONFIDENCE_THRESHOLD,
    ParsedRubric,
    build_scoring_prompt,
    parse_ai_response,
    parse_rubric,
)

# ai_rubric 评分器使用的 AI 总线档位（L2 强模型生产与裁判）
# 为什么默认 L2：架构 v2 §4.8 L2 = 强模型生产与裁判；L3 用于双模型或 AI+人工复核
# （低置信自动转 human_confirm 队列时，可由调用方升级到 L3 二次评分）
DEFAULT_TASK_LEVEL: str = "L2"

# 评分器版本（含代码 digest 提示，重判时据此写平行 score_run，R-D-05）
SCORER_VERSION: str = "1.0.0+ai-rubric"


# ────────────────────────────────────────────────────────────────────
# 主入口：score(response_text, rubric_template, grade_band) → AIRubricScore
# ────────────────────────────────────────────────────────────────────


def score(
    response_text: str,
    rubric_template: RubricTemplate | dict[str, Any],
    grade_band: GradeBand,
    *,
    task_level: str = DEFAULT_TASK_LEVEL,
    clients: dict[str, Any] | None = None,
    bypass_pii_filter: bool = False,
    context: dict[str, Any] | None = None,
) -> AIRubricScore:
    """AI 维度量规评分主入口（任务卡 T-W4-019 验收①）.

    流程（验收③：AI 调用经 S2 总线 + PII 剥离 + 台账记录）：
      1. ``parse_rubric`` → ParsedRubric（中性结构）；
      2. ``build_scoring_prompt`` → 评分 prompt（量规+作答+学段+输出格式约束）；
      3. ``ai_call(L2, prompt)`` → AIResult；
         - prompt 含 PII 时由 ``pii_filter.strip`` 在 router 入口剥离（D7）；
         - 台账由 ledger 在 ``ai_call`` 内部记录（T-W4-008）；
      4. ``parse_ai_response`` → AIRubricScore（逐维 score/rationale/confidence）；
      5. 整体置信度 < 阈值 → needs_human_review=True（验收②待人工复核）.

    Args:
        response_text: 学生作答文本（开放式作文/看图写话；可能含 PII，由总线剥离）.
        rubric_template: 量规模板（``RubricTemplate`` 或已序列化的 dict）.
        grade_band: 学段 L/M/H（影响 prompt 上下文，不改变量规分值）.
        task_level: AI 总线档位（默认 L2 强模型）.
        clients: 注入的供应商客户端映射（测试用 mock；生产空走注册的/桩）.
        bypass_pii_filter: 测试用绕过 PII 剥离（生产禁止传 True，D7）.
        context: 透传给 ledger 的上下文（artifact_ref 等，可选）.

    Returns:
        AIRubricScore：{dimensions, total_score, total_max, overall_confidence,
        needs_human_review}.

    Raises:
        ValueError: 量规结构非法（``parse_rubric`` 失败）.
        Exception: ``ai_call`` 主客户端失败且无 fallback 时向上抛出（验收②异常路径）.
    """
    parsed: ParsedRubric = parse_rubric(rubric_template)
    prompt = build_scoring_prompt(response_text, parsed, grade_band)

    # 验收③：AI 调用经 S2 总线；PII 在 router 入口剥离（除非 bypass）
    ai_result = ai_call(
        task_level=task_level,  # type: ignore[arg-type]
        prompt=prompt,
        context=context,
        clients=clients,
        bypass_pii_filter=bypass_pii_filter,
    )

    rubric_score = parse_ai_response(ai_result.content, parsed)

    # 附加审计元信息（不破坏 AIRubricScore 结构；调用方可从返回值的 to_dict 取用）
    # 这里不修改 AIRubricScore 的字段，但 parse_ai_response 已根据 overall_confidence
    # 设定 needs_human_review；若 AI 调用走 fallback 或返回空（L0），parse_ai_response
    # 已将零分/低置信/复核标记正确置位。
    return rubric_score


# ────────────────────────────────────────────────────────────────────
# Scorer 实现：注册到评分器注册表（scorer_id='ai_rubric'）
# ────────────────────────────────────────────────────────────────────


def _extract_response_text(response: Any) -> str:
    """从 response 提取作答文本.

    支持形态：
    - 裸字符串（直接用）；
    - ``{text: "..."}``（writing / short_answer 交互类型）；
    - ``{blanks: {b1: "..."}}``（text_blank 拼接）.
    """
    if isinstance(response, str):
        return response
    if response is None:
        return ""
    if isinstance(response, dict):
        text = response.get("text")
        if isinstance(text, str) and text:
            return text
        blanks = response.get("blanks")
        if isinstance(blanks, dict) and blanks:
            parts = []
            for val in blanks.values():
                if isinstance(val, dict):
                    v = val.get("value")
                    if v is not None:
                        parts.append(str(v))
                elif val is not None:
                    parts.append(str(val))
            return " ".join(parts)
    return str(response or "")


def _extract_grade_band(item_version: Any) -> GradeBand:
    """从 item_version 推断学段（缺省 M，影响 prompt 上下文但不改量规分值）."""
    try:
        objective = (
            item_version.get("objective")
            if isinstance(item_version, dict)
            else getattr(item_version, "objective", None)
        )
        if isinstance(objective, dict):
            gb = objective.get("gradeband")
            if gb in ("L", "M", "H"):
                return gb  # type: ignore[return-value]
    except Exception:  # noqa: BLE001
        pass
    return "M"


class AIRubricScorer(Scorer):
    """ai_rubric 评分器实现（scorer.yaml ai_rubric 落地）.

    实现 ``Scorer`` 基类契约（``score(response, item_version, params) -> ScoreResult``）：
    - 从 ``params['rubric']`` 取量规（对齐 scorer.yaml ai_rubric.params_schema.rubric）；
    - 从 ``params['model_tier']`` 取 AI 总线档位（默认 L2）；
    - 从 ``response`` 取作答文本，``item_version.objective.gradeband`` 取学段；
    - 调 ``score()`` 主函数（经 S2 总线 + PII 剥离）；
    - 转换为 ``ScoreResult``：dimension_scores={dim_id: score},
      evidence 含逐维理由与置信度，error_inferences 含 human_confirm 触发条目.
    """

    scorer_id: ClassVar[str] = "ai_rubric"
    version: ClassVar[str] = SCORER_VERSION
    deterministic: ClassVar[bool] = False  # AI 评分非确定性

    def score(
        self,
        response: Any,
        item_version: Any,
        params: dict[str, Any] | None = None,
    ) -> ScoreResult:
        params = params or {}
        rubric = params.get("rubric")
        if not rubric:
            return ScoreResult(
                dimension_scores={"correct": 0.0},
                error_inferences=[],
                confidence={"scoring": 0.0},
                evidence={"reason": "scorer_params 缺 rubric"},
                scorer_version=self.version,
            )

        response_text = _extract_response_text(response)
        grade_band = _extract_grade_band(item_version)
        task_level = params.get("model_tier", DEFAULT_TASK_LEVEL)
        # 透传调用方注入的 clients（生产空走注册的/桩）
        clients = params.get("clients")

        rubric_score: AIRubricScore = score(
            response_text=response_text,
            rubric_template=rubric,  # type: ignore[arg-type]
            grade_band=grade_band,
            task_level=task_level,  # type: ignore[arg-type]
            clients=clients,
            # PII 剥离由 ai_call 内部执行（D7）；此处不 bypass
        )

        # 转换为 ScoreResult 五要素（scorer.yaml unified_contract.output_schema）
        dimension_scores: dict[str, float] = {}
        dim_evidence: list[dict[str, Any]] = []
        for parsed_dim, scored in zip(_iter_parsed_dims(rubric), rubric_score.dimensions):
            dimension_scores[parsed_dim] = scored.score
            dim_evidence.append(
                {
                    "id": parsed_dim,
                    "name": scored.name,
                    "score": scored.score,
                    "max": scored.max,
                    "rationale": scored.rationale,
                    "confidence": scored.confidence,
                }
            )
        dimension_scores["total"] = rubric_score.total_score

        error_inferences: list[dict[str, Any]] = []
        if rubric_score.needs_human_review:
            # 验收②：低置信（<0.6）标记待人工复核 → human_confirm 队列
            error_inferences.append(
                {
                    "error_type_id": "low_confidence_needs_human_review",
                    "confidence": rubric_score.overall_confidence,
                    "rule_version": self.version,
                    "evidence": {
                        "overall_confidence": rubric_score.overall_confidence,
                        "threshold": HUMAN_REVIEW_CONFIDENCE_THRESHOLD,
                    },
                }
            )

        return ScoreResult(
            dimension_scores=dimension_scores,
            error_inferences=error_inferences,
            confidence={"scoring": rubric_score.overall_confidence},
            evidence={
                "dimensions": dim_evidence,
                "total_score": rubric_score.total_score,
                "total_max": rubric_score.total_max,
                "overall_confidence": rubric_score.overall_confidence,
                "needs_human_review": rubric_score.needs_human_review,
            },
            scorer_version=self.version,
        )


def _iter_parsed_dims(rubric: RubricTemplate | dict[str, Any]) -> list[str]:
    """从 rubric 提取维度 id 列表（保持顺序，用于 dimension_scores 键）."""
    try:
        parsed = parse_rubric(rubric)
        return [d.id for d in parsed.dimensions]
    except ValueError:
        return []


# 注册（import 即生效，与 platform_scorers.py 同模式）
register_scorer("platform", AIRubricScorer())


__all__ = [
    "AIRubricScorer",
    "DEFAULT_TASK_LEVEL",
    "SCORER_VERSION",
    "score",
]
