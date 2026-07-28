"""T-W4-019 AI 维度量规评分器单元测试.

验收对照：
  §1 ``score(response_text, rubric_template, grade_band)`` 返回
     ``{dimensions:[{name,score,max,rationale,confidence}], total_score,
     total_max, overall_confidence}``.
  §2 逐维理由非空，置信度 0–1 连续值；低置信（<0.6）标记待人工复核.
  §3 AI 调用经 S2 总线（T-W4-007），PII 已剥离（T-W4-008），台账已记录.
  §4 ``make accept TASK=T-W4-019`` 全绿；单元测试使用 mock AI 响应验证解析逻辑.
  §5 不 import 任何学科包/学段包.

测试不消耗真实 API：``_MockLLMClient`` 实现 ``LLMClient`` Protocol，
返回预设 JSON 响应；PII 剥离由 ``ai_call`` 内部 ``pii_filter`` 执行（验收③）.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from src.core.ai.bus.models import AIResult
from src.core.production.rubric_template import (
    GradeBand,
    RubricDimension,
    RubricLevel,
    RubricTemplate,
)
from src.core.scoring.ai_rubric_scorer import (
    AIRubricScorer,
    DEFAULT_TASK_LEVEL,
    SCORER_VERSION,
    score,
)
from src.core.scoring.registry import get_scorer, list_scorers
from src.core.scoring.rubric_parser import (
    HUMAN_REVIEW_CONFIDENCE_THRESHOLD,
    build_scoring_prompt,
    parse_ai_response,
    parse_rubric,
)


# ────────────────────────────────────────────────────────────────────
# Mock LLM 客户端
# ────────────────────────────────────────────────────────────────────


class _MockLLMClient:
    """记录调用参数的 mock 客户端（实现 LLMClient Protocol）.

    ``response_content`` 为返回的 content；``calls`` 记录每次调用的 prompt 与参数.
    """

    def __init__(self, *, response_content: str = "") -> None:
        self.calls: list[dict[str, Any]] = []
        self._response_content = response_content

    def complete(
        self, prompt: str, *, model: str, temperature: float, max_tokens: int
    ) -> AIResult:
        self.calls.append(
            {
                "prompt": prompt,
                "model": model,
                "temperature": temperature,
                "max_tokens": max_tokens,
            }
        )
        return AIResult(
            content=self._response_content,
            model=model,
            token_in=len(prompt),
            token_out=len(self._response_content),
            duration_ms=0.5,
        )


# ────────────────────────────────────────────────────────────────────
# 测试夹具：合法量规
# ────────────────────────────────────────────────────────────────────


def _make_rubric(grade_band: str = "M") -> RubricTemplate:
    """构造一份四维量规（内容/结构/语言/书写，各 5 分满分，3 档）."""
    return RubricTemplate(
        rubric_id=f"sha256:test-rubric-{grade_band}-v1",
        name=f"作文量规-{grade_band}段",
        grade_band=grade_band,  # type: ignore[arg-type]
        dimensions=[
            RubricDimension(
                id="content",
                name="内容",
                max_score=5,
                levels=[
                    RubricLevel(level=1, label="优秀", description="主题明确", score=5),
                    RubricLevel(level=2, label="合格", description="主题基本明确", score=3),
                    RubricLevel(level=3, label="待改进", description="主题模糊", score=1),
                ],
            ),
            RubricDimension(
                id="structure",
                name="结构",
                max_score=5,
                levels=[
                    RubricLevel(level=1, label="优秀", description="段落清晰", score=5),
                    RubricLevel(level=2, label="合格", description="段落较清晰", score=3),
                    RubricLevel(level=3, label="待改进", description="段落混乱", score=1),
                ],
            ),
        ],
        total_max_score=10,
        version="1.0.0",
    )


def _make_ai_json_response(
    *, content_score: float = 5,
    structure_score: float = 4,
    content_conf: float = 0.9,
    structure_conf: float = 0.85,
) -> str:
    """构造合法 AI JSON 响应（验收①输出结构）."""
    return json.dumps(
        {
            "dimensions": [
                {"id": "content", "score": content_score,
                 "rationale": "主题明确，内容充实，符合优秀档锚点。",
                 "confidence": content_conf},
                {"id": "structure", "score": structure_score,
                 "rationale": "段落较清晰，但过渡略显生硬，介于优秀与合格之间。",
                 "confidence": structure_conf},
            ]
        },
        ensure_ascii=False,
    )


@pytest.fixture
def rubric() -> RubricTemplate:
    return _make_rubric("M")


@pytest.fixture
def mock_client(rubric: RubricTemplate) -> _MockLLMClient:
    return _MockLLMClient(response_content=_make_ai_json_response())


# ────────────────────────────────────────────────────────────────────
# §1 score() 返回结构契约
# ────────────────────────────────────────────────────────────────────


class TestScoreContract:
    """验收 §1：score 返回 {dimensions, total_score, total_max, overall_confidence}."""

    def test_returns_required_top_level_fields(
        self, rubric: RubricTemplate, mock_client: _MockLLMClient
    ) -> None:
        """返回 dict 含 dimensions/total_score/total_max/overall_confidence."""
        result = score(
            "春天来了，万物复苏。",
            rubric,
            "M",
            clients={"deepseek": mock_client},
            bypass_pii_filter=True,
        )
        d = result.to_dict()
        assert "dimensions" in d
        assert "total_score" in d
        assert "total_max" in d
        assert "overall_confidence" in d

    def test_dimensions_have_required_fields(
        self, rubric: RubricTemplate, mock_client: _MockLLMClient
    ) -> None:
        """每个 dimension 含 name/score/max/rationale/confidence（验收①）."""
        result = score(
            "春天来了。", rubric, "M",
            clients={"deepseek": mock_client}, bypass_pii_filter=True,
        )
        assert len(result.dimensions) == 2
        for dim in result.dimensions:
            assert isinstance(dim.name, str) and dim.name
            assert isinstance(dim.score, (int, float))
            assert isinstance(dim.max, (int, float)) and dim.max == 5
            assert isinstance(dim.rationale, str) and dim.rationale
            assert 0.0 <= dim.confidence <= 1.0

    def test_total_score_is_sum_of_dimensions(
        self, rubric: RubricTemplate, mock_client: _MockLLMClient
    ) -> None:
        """total_score = sum(dimensions[*].score)."""
        result = score(
            "春天来了。", rubric, "M",
            clients={"deepseek": mock_client}, bypass_pii_filter=True,
        )
        assert result.total_score == pytest.approx(
            sum(d.score for d in result.dimensions)
        )

    def test_total_max_matches_rubric(
        self, rubric: RubricTemplate, mock_client: _MockLLMClient
    ) -> None:
        """total_max = sum(dimensions[*].max) = rubric.total_max_score."""
        result = score(
            "春天来了。", rubric, "M",
            clients={"deepseek": mock_client}, bypass_pii_filter=True,
        )
        assert result.total_max == rubric.total_max_score == 10

    def test_overall_confidence_is_min_of_dimensions(
        self, rubric: RubricTemplate, mock_client: _MockLLMClient
    ) -> None:
        """overall_confidence = min(dimensions[*].confidence)（串联取弱环）."""
        result = score(
            "春天来了。", rubric, "M",
            clients={"deepseek": mock_client}, bypass_pii_filter=True,
        )
        expected = min(d.confidence for d in result.dimensions)
        assert result.overall_confidence == pytest.approx(expected)

    def test_grade_band_affects_prompt(
        self, rubric: RubricTemplate, mock_client: _MockLLMClient
    ) -> None:
        """不同学段传入后 prompt 应含学段标签."""
        for band in ("L", "M", "H"):
            mock_client.calls.clear()
            score(
                "春天来了。", rubric, band,  # type: ignore[arg-type]
                clients={"deepseek": mock_client}, bypass_pii_filter=True,
            )
            assert mock_client.calls, f"band={band} 未触发 AI 调用"
            prompt = mock_client.calls[-1]["prompt"]
            # prompt 含学段关键字（低/中/高 段）
            band_label_map = {"L": "低段", "M": "中段", "H": "高段"}
            assert band_label_map[band] in prompt, (
                f"band={band} prompt 缺学段标签：{prompt[:120]}"
            )

    def test_rubric_dict_input_accepted(self, mock_client: _MockLLMClient) -> None:
        """rubric_template 接受 dict 输入（对齐 scorer.yaml params_schema.rubric）."""
        rubric_dict = {
            "dimensions": [
                {
                    "id": "content", "name": "内容",
                    "anchors": ["主题明确", "主题基本明确", "主题模糊"],
                    "score_bands": [
                        {"level": 1, "label": "优秀", "score": 5},
                        {"level": 2, "label": "合格", "score": 3},
                        {"level": 3, "label": "待改进", "score": 1},
                    ],
                    "error_type_rules": [],
                }
            ],
            "total_max_score": 5,
        }
        result = score(
            "春天来了。", rubric_dict, "M",
            clients={"deepseek": mock_client}, bypass_pii_filter=True,
        )
        assert len(result.dimensions) == 1
        assert result.dimensions[0].name == "内容"
        assert result.total_max == 5


# ────────────────────────────────────────────────────────────────────
# §2 逐维理由非空 + 置信度连续值 + 低置信人工复核
# ────────────────────────────────────────────────────────────────────


class TestRationaleAndConfidence:
    """验收 §2：理由非空 / 置信度 0-1 / 低置信标记人工复核."""

    def test_rationale_non_empty_when_ai_provides(
        self, rubric: RubricTemplate, mock_client: _MockLLMClient
    ) -> None:
        """AI 提供理由时，逐维 rationale 非空."""
        result = score(
            "春天来了。", rubric, "M",
            clients={"deepseek": mock_client}, bypass_pii_filter=True,
        )
        for dim in result.dimensions:
            assert dim.rationale, f"{dim.name} rationale 为空"
            # 理由应引用锚点（AI 响应中的「优秀档锚点」）
            assert isinstance(dim.rationale, str)

    def test_low_confidence_triggers_human_review(
        self, rubric: RubricTemplate
    ) -> None:
        """overall_confidence < 0.6 → needs_human_review=True（验收②）."""
        low_conf_json = _make_ai_json_response(
            content_conf=0.3, structure_conf=0.4
        )
        client = _MockLLMClient(response_content=low_conf_json)
        result = score(
            "春天来了。", rubric, "M",
            clients={"deepseek": client}, bypass_pii_filter=True,
        )
        assert result.overall_confidence < HUMAN_REVIEW_CONFIDENCE_THRESHOLD
        assert result.needs_human_review is True

    def test_high_confidence_no_human_review(
        self, rubric: RubricTemplate, mock_client: _MockLLMClient
    ) -> None:
        """overall_confidence ≥ 0.6 → needs_human_review=False."""
        result = score(
            "春天来了。", rubric, "M",
            clients={"deepseek": mock_client}, bypass_pii_filter=True,
        )
        assert result.overall_confidence >= HUMAN_REVIEW_CONFIDENCE_THRESHOLD
        assert result.needs_human_review is False

    def test_confidence_continuous_float(
        self, rubric: RubricTemplate, mock_client: _MockLLMClient
    ) -> None:
        """置信度为 0-1 连续值（非 0/1 二值）."""
        result = score(
            "春天来了。", rubric, "M",
            clients={"deepseek": mock_client}, bypass_pii_filter=True,
        )
        for dim in result.dimensions:
            assert isinstance(dim.confidence, float)
            assert 0.0 <= dim.confidence <= 1.0

    def test_score_clamped_to_score_bands(self, rubric: RubricTemplate) -> None:
        """AI 返回超界 score 时 clamp 到分值带内（max=5 / min=1）."""
        bad_json = json.dumps(
            {
                "dimensions": [
                    {"id": "content", "score": 99.0,
                     "rationale": "强行满分", "confidence": 0.9},
                    {"id": "structure", "score": -10.0,
                     "rationale": "强行零分", "confidence": 0.9},
                ]
            },
            ensure_ascii=False,
        )
        client = _MockLLMClient(response_content=bad_json)
        result = score(
            "春天来了。", rubric, "M",
            clients={"deepseek": client}, bypass_pii_filter=True,
        )
        # content clamp 到 max=5
        assert result.dimensions[0].score == 5.0
        # structure clamp 到 min=1
        assert result.dimensions[1].score == 1.0

    def test_empty_rationale_degrades_confidence(self, rubric: RubricTemplate) -> None:
        """AI 返回空 rationale 时降低置信度并替换占位理由."""
        bad_json = json.dumps(
            {
                "dimensions": [
                    {"id": "content", "score": 5, "rationale": "",
                     "confidence": 0.9},
                    {"id": "structure", "score": 4, "rationale": "段落清晰",
                     "confidence": 0.9},
                ]
            },
            ensure_ascii=False,
        )
        client = _MockLLMClient(response_content=bad_json)
        result = score(
            "春天来了。", rubric, "M",
            clients={"deepseek": client}, bypass_pii_filter=True,
        )
        # 空 rationale 被替换为占位文本，且置信度降至 ≤0.3
        assert result.dimensions[0].rationale
        assert result.dimensions[0].confidence <= 0.3
        assert result.needs_human_review is True  # 低置信触发复核

    def test_invalid_json_response_triggers_review(
        self, rubric: RubricTemplate
    ) -> None:
        """AI 返回非 JSON 时，零分 + 低置信 + 人工复核标记."""
        client = _MockLLMClient(response_content="这不是JSON")
        result = score(
            "春天来了。", rubric, "M",
            clients={"deepseek": client}, bypass_pii_filter=True,
        )
        assert result.overall_confidence == 0.0
        assert result.needs_human_review is True
        assert result.total_score == 0.0
        for dim in result.dimensions:
            assert dim.rationale, "失败时仍需给出占位理由"

    def test_markdown_fenced_json_extracted(self, rubric: RubricTemplate) -> None:
        """AI 输出含 markdown 围栏 ```` ```json ... ``` ```` 时正则提取."""
        content = "```json\n" + _make_ai_json_response() + "\n```"
        client = _MockLLMClient(response_content=content)
        result = score(
            "春天来了。", rubric, "M",
            clients={"deepseek": client}, bypass_pii_filter=True,
        )
        # 解析成功：分数非零
        assert result.total_score == pytest.approx(9.0)  # 5+4
        assert result.overall_confidence >= HUMAN_REVIEW_CONFIDENCE_THRESHOLD


# ────────────────────────────────────────────────────────────────────
# §3 AI 调用经 S2 总线 + PII 剥离 + 台账记录
# ────────────────────────────────────────────────────────────────────


class TestAIBusIntegration:
    """验收 §3：AI 调用经 S2 总线（T-W4-007），PII 已剥离（T-W4-008）."""

    def test_uses_default_l2_task_level(
        self, rubric: RubricTemplate, mock_client: _MockLLMClient
    ) -> None:
        """默认 task_level=L2（DEFAULT_TASK_LEVEL）."""
        assert DEFAULT_TASK_LEVEL == "L2"
        score(
            "春天来了。", rubric, "M",
            clients={"deepseek": mock_client}, bypass_pii_filter=True,
        )
        # mock_client.calls 每条记录 model 字段（L2 命中 deepseek-chat）
        assert mock_client.calls
        # ai_call 路由到 L2 policy.model（不强行断言具体模型名，仅断言被调用）
        assert mock_client.calls[-1]["model"]

    def test_pii_stripped_before_llm_call(
        self, rubric: RubricTemplate
    ) -> None:
        """作答含 PII（手机号）时，prompt 传给 LLM 前已剥离（D7）."""
        client = _MockLLMClient(response_content=_make_ai_json_response())
        # 作答文本含手机号（PII）
        response_with_pii = "我叫张三，电话 13812345678，写春天。"
        score(
            response_with_pii, rubric, "M",
            clients={"deepseek": client},
            # 不 bypass：让 pii_filter 真正执行
            bypass_pii_filter=False,
        )
        assert client.calls, "应触发 LLM 调用"
        prompt_sent = client.calls[-1]["prompt"]
        # 手机号应被替换为 [PHONE]（pii_filter.strip 行为）
        assert "13812345678" not in prompt_sent, (
            f"PII 未被剥离：prompt 仍含手机号。prompt 末段：{prompt_sent[-200:]}"
        )
        assert "[PHONE]" in prompt_sent, "prompt 应含 [PHONE] 占位"

    def test_pii_filter_failure_degrades_gracefully(
        self, rubric: RubricTemplate, mock_client: _MockLLMClient
    ) -> None:
        """bypass_pii_filter=True 时不剥离（测试用绕过；生产禁止）."""
        response_with_pii = "电话 13812345678"
        score(
            response_with_pii, rubric, "M",
            clients={"deepseek": mock_client},
            bypass_pii_filter=True,
        )
        prompt_sent = mock_client.calls[-1]["prompt"]
        # bypass 时 PII 未剥离
        assert "13812345678" in prompt_sent


# ────────────────────────────────────────────────────────────────────
# §4 Scorer 注册 + Scorer.score() 实现（注册表契约）
# ────────────────────────────────────────────────────────────────────


class TestScorerRegistration:
    """验收 §4：AIRubricScorer 注册到 platform 桶，scorer_id='ai_rubric'."""

    def test_scorer_registered_in_platform_bucket(self) -> None:
        """ai_rubric 已注册到 platform 桶."""
        # import 即注册（ai_rubric_scorer 模块加载时 register_scorer）
        import src.core.scoring.ai_rubric_scorer  # noqa: F401

        assert "ai_rubric" in list_scorers("platform")

    def test_get_scorer_returns_instance(self) -> None:
        """get_scorer('ai_rubric') 返回 AIRubricScorer 实例."""
        import src.core.scoring.ai_rubric_scorer  # noqa: F401

        scorer = get_scorer("ai_rubric")
        assert isinstance(scorer, AIRubricScorer)

    def test_scorer_class_attributes(self) -> None:
        """scorer_id / version / deterministic 类属性符合契约."""
        assert AIRubricScorer.scorer_id == "ai_rubric"
        assert AIRubricScorer.version == SCORER_VERSION
        assert AIRubricScorer.deterministic is False  # AI 非确定性

    def test_scorer_score_returns_score_result(self, rubric: RubricTemplate) -> None:
        """Scorer.score() 返回 ScoreResult 五要素."""
        import src.core.scoring.ai_rubric_scorer  # noqa: F401

        scorer = AIRubricScorer()
        mock_client = _MockLLMClient(response_content=_make_ai_json_response())
        # item_version 最小结构：含 objective.gradeband
        item_version = {"objective": {"gradeband": "M"}, "interaction_ref": {"interaction_id": "writing"}}
        result = scorer.score(
            response={"text": "春天来了。"},
            item_version=item_version,
            params={
                "rubric": rubric.to_scorer_params(),
                "clients": {"deepseek": mock_client},
            },
        )
        # ScoreResult 五要素
        assert "correct" in result.dimension_scores or "total" in result.dimension_scores
        assert "content" in result.dimension_scores
        assert "structure" in result.dimension_scores
        assert "total" in result.dimension_scores
        assert isinstance(result.error_inferences, list)
        assert "scoring" in result.confidence
        assert result.scorer_version == SCORER_VERSION
        # evidence 含逐维理由
        assert "dimensions" in result.evidence
        assert len(result.evidence["dimensions"]) == 2
        for dim_ev in result.evidence["dimensions"]:
            assert dim_ev["rationale"], "rationale 非空"

    def test_scorer_low_confidence_adds_error_inference(
        self, rubric: RubricTemplate
    ) -> None:
        """低置信时 error_inferences 含 human_confirm 触发条目（验收②）."""
        scorer = AIRubricScorer()
        low_conf_json = _make_ai_json_response(
            content_conf=0.2, structure_conf=0.3
        )
        client = _MockLLMClient(response_content=low_conf_json)
        item_version = {"objective": {"gradeband": "M"}}
        result = scorer.score(
            response={"text": "春天。"},
            item_version=item_version,
            params={
                "rubric": rubric.to_scorer_params(),
                "clients": {"deepseek": client},
            },
        )
        assert len(result.error_inferences) == 1
        inf = result.error_inferences[0]
        assert inf["error_type_id"] == "low_confidence_needs_human_review"
        assert inf["confidence"] < HUMAN_REVIEW_CONFIDENCE_THRESHOLD
        assert inf["rule_version"] == SCORER_VERSION

    def test_scorer_missing_rubric_param(self) -> None:
        """scorer_params 缺 rubric 时返回零分低置信（配置错误兜底）."""
        scorer = AIRubricScorer()
        result = scorer.score(
            response={"text": "..."}, item_version={}, params={}
        )
        assert result.confidence["scoring"] == 0.0
        assert "reason" in result.evidence
        assert result.evidence["reason"] == "scorer_params 缺 rubric"


# ────────────────────────────────────────────────────────────────────
# §5 不 import 任何学科包/学段包
# ────────────────────────────────────────────────────────────────────


class TestNoSubjectPackImport:
    """验收 §5：核心域 src/core/scoring/ 禁止 import 学科包/学段包（宪法 A5/X6）."""

    def test_no_packs_import_in_scoring(self) -> None:
        scoring_dir = (
            Path(__file__).resolve().parent.parent.parent
            / "src"
            / "core"
            / "scoring"
        )
        assert scoring_dir.is_dir(), f"目录不存在：{scoring_dir}"
        pattern = re.compile(
            r"^\s*(?:from\s+(?:packs|src\.packs)"
            r"|import\s+(?:packs|src\.packs))",
            re.MULTILINE,
        )
        violations: list[str] = []
        for py_file in sorted(scoring_dir.rglob("*.py")):
            text_src = py_file.read_text(encoding="utf-8")
            if pattern.findall(text_src):
                violations.append(str(py_file.relative_to(scoring_dir)))
        assert not violations, (
            f"core/scoring 存在学科包 import（违反 A5）：{violations}"
        )

    def test_no_scorer_sdk_import(self) -> None:
        """核心域 scoring 禁止 import openai/deepseek/anthropic（X6 等价）."""
        scoring_dir = (
            Path(__file__).resolve().parent.parent.parent
            / "src"
            / "core"
            / "scoring"
        )
        pattern = re.compile(
            r"^\s*(?:from\s+(?:openai|deepseek|anthropic)"
            r"|import\s+(?:openai|deepseek|anthropic))",
            re.MULTILINE,
        )
        violations: list[str] = []
        for py_file in sorted(scoring_dir.rglob("*.py")):
            text_src = py_file.read_text(encoding="utf-8")
            if pattern.findall(text_src):
                violations.append(str(py_file.relative_to(scoring_dir)))
        assert not violations, (
            f"core/scoring 直接 import LLM SDK（违反 X6 等价约束）：{violations}"
        )


# ────────────────────────────────────────────────────────────────────
# 补充：rubric_parser 单元测试（解析逻辑）
# ────────────────────────────────────────────────────────────────────


class TestRubricParser:
    """rubric_parser 单元测试：解析量规与构建 prompt."""

    def test_parse_rubric_from_template(self, rubric: RubricTemplate) -> None:
        """parse_rubric 接受 RubricTemplate."""
        parsed = parse_rubric(rubric)
        assert len(parsed.dimensions) == 2
        assert parsed.dimensions[0].id == "content"
        assert parsed.dimensions[0].name == "内容"
        assert parsed.dimensions[0].max_score == 5
        assert len(parsed.dimensions[0].anchors) == 3
        assert len(parsed.dimensions[0].score_bands) == 3
        assert parsed.total_max_score == 10

    def test_parse_rubric_from_dict(self) -> None:
        """parse_rubric 接受 dict（scorer.yaml params_schema.rubric）."""
        rubric_dict = {
            "dimensions": [
                {
                    "id": "x", "name": "X",
                    "anchors": ["a1", "a2"],
                    "score_bands": [
                        {"level": 1, "label": "高", "score": 5},
                        {"level": 2, "label": "低", "score": 2},
                    ],
                    "error_type_rules": [],
                }
            ],
            "total_max_score": 5,
        }
        parsed = parse_rubric(rubric_dict)
        assert parsed.dimensions[0].max_score == 5
        assert parsed.total_max_score == 5

    def test_parse_rubric_invalid_raises(self) -> None:
        """parse_rubric 对非法结构抛 ValueError."""
        with pytest.raises(ValueError):
            parse_rubric({"dimensions": []})  # 空 dimensions
        with pytest.raises(ValueError):
            parse_rubric({"dimensions": [{"id": "x"}]})  # 缺 name/anchors
        with pytest.raises(ValueError):
            parse_rubric("not a dict")  # type: ignore[arg-type]

    def test_build_prompt_contains_rubric_and_response(
        self, rubric: RubricTemplate
    ) -> None:
        """build_scoring_prompt 输出含量规维度、作答文本、学段."""
        parsed = parse_rubric(rubric)
        prompt = build_scoring_prompt("学生作答原文", parsed, "M")
        assert "内容" in prompt
        assert "结构" in prompt
        assert "学生作答原文" in prompt
        assert "中段" in prompt
        # 输出格式约束
        assert "JSON" in prompt
        assert "rationale" in prompt
        assert "confidence" in prompt

    def test_parse_ai_response_extracts_dimensions(
        self, rubric: RubricTemplate
    ) -> None:
        """parse_ai_response 从合法 JSON 提取逐维分数."""
        parsed = parse_rubric(rubric)
        content = _make_ai_json_response(content_score=5, structure_score=3)
        result = parse_ai_response(content, parsed)
        assert len(result.dimensions) == 2
        assert result.dimensions[0].score == 5
        assert result.dimensions[0].name == "内容"
        assert result.dimensions[0].max == 5
        assert result.dimensions[1].score == 3
        assert result.total_score == 8
        assert result.total_max == 10
