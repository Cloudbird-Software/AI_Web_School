"""T-W4-048 评分器 scoring_trace.process.correct 键覆盖测试.

验收对照（T-W4-048）：
  §1 全部现役评分器（exact_match/keypoint_hit/stepwise_rubric/math_equivalence/
     ai_rubric）输出 scoring_trace 含 process.correct 布尔值。
  §2 response_event 写入后 process.correct 存在且非空（DB 回读验证）。
  §3 既有评分测试不退化；本测试覆盖全部评分器类型的 correct 键。
  §5 不 import 任何学科包/学段包——本文件是测试，可 import 学科包评分器
     以验证；核心域 src/core/scoring 仍零学科包 import（另见
     test_ai_rubric_scorer.TestNoSubjectPackImport）。

口径约定（与 ScoringOutcome.correct 一致）：
  dimension_scores['correct'] >= 1.0 ⇒ process.correct=True；
  部分分 <1.0 或缺 correct 键 ⇒ process.correct=False（错题回测标记口径）。

消费侧契约（src/core/review/scheduler.py::derive_correctness）：
  process.correct 必须 isinstance(bool)——非 bool 脏数据不被采用。
  故本测试钉死 bool 类型，防回归（numpy/float 等会被消费侧静默忽略）。
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

import src.core.scoring.platform_scorers  # noqa: F401 —— import 即注册 platform 评分器
import src.core.scoring.ai_rubric_scorer  # noqa: F401 —— import 即注册 ai_rubric
from src.core.scoring.registry import ScoreResult, get_scorer
from src.core.scoring.service import build_scoring_trace, run_scorer, score_and_record

# ────────────────────────────────────────────────────────────────────
# 数学包评分器注册（importlib 加载连字符目录，触发 register_scorer）
# ────────────────────────────────────────────────────────────────────

_MATH_SCORERS_PATH = (
    Path(__file__).resolve().parents[2]
    / "src" / "packs" / "subject-math" / "scorers" / "__init__.py"
)


def _register_math_scorers() -> None:
    spec = importlib.util.spec_from_file_location(
        "subject_math_scorers_pkg_for_trace_correct", _MATH_SCORERS_PATH
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["subject_math_scorers_pkg_for_trace_correct"] = mod
    spec.loader.exec_module(mod)
    mod.register_math_scorers()


_register_math_scorers()


# ────────────────────────────────────────────────────────────────────
# 辅助：构造 item_version
# ────────────────────────────────────────────────────────────────────

def _iv(
    interaction_id: str,
    scorer_id: str,
    scorer_params: dict,
    *,
    error_bindings: list | None = None,
    item_version_id: str = "sha256:trace-correct-iv",
) -> dict:
    return {
        "item_version_id": item_version_id,
        "interaction_ref": {"interaction_id": interaction_id, "interaction_params": {}},
        "scoring_ref": {"scorer_id": scorer_id, "scorer_params": scorer_params},
        "error_bindings": error_bindings or [],
    }


def _assert_correct_contract(trace: dict, expected: bool) -> None:
    """断言 scoring_trace.process.correct 满足消费侧契约.

    - process 是 dict（契约 §3 可扩展对象）；
    - process.correct 存在；
    - process.correct 是 Python bool（review/scheduler 用 isinstance 判断）；
    - process.correct 与预期一致（口径：dimension_scores['correct'] >= 1.0）。
    """
    assert "process" in trace, "scoring_trace 缺 process 键"
    process = trace["process"]
    assert isinstance(process, dict), f"process 非 dict：{type(process)}"
    assert "correct" in process, "process 缺 correct 键（T-W4-048）"
    correct = process["correct"]
    # 消费侧 isinstance(correct, bool) 严格检查——numpy.bool_ 等会被静默忽略
    assert isinstance(correct, bool), (
        f"process.correct 非 Python bool：{type(correct)}（消费侧不采用）"
    )
    assert correct is expected, (
        f"process.correct={correct} 与预期 {expected} 不符"
    )


# ════════════════════════════════════════════════════════════════════
# §1 各评分器输出 process.correct
# ════════════════════════════════════════════════════════════════════

class TestExactMatchCorrect:
    """exact_match：对/错两种情形的 process.correct."""

    def test_correct_single_choice(self):
        iv = _iv("single_choice", "exact_match", {"answer": "B"})
        r = run_scorer(iv, {"selected": "B"})
        trace = build_scoring_trace("exact_match", r)
        _assert_correct_contract(trace, expected=True)

    def test_wrong_single_choice(self):
        iv = _iv("single_choice", "exact_match", {"answer": "B"})
        r = run_scorer(iv, {"selected": "A"})
        trace = build_scoring_trace("exact_match", r)
        _assert_correct_contract(trace, expected=False)

    def test_partial_credit_marks_wrong(self):
        """部分分 <1.0 一律记错（错题回测口径）."""
        iv = _iv(
            "multi_choice", "exact_match",
            {"answer": ["6", "9"], "partial_credit": {"per_item": True}},
        )
        r = run_scorer(iv, {"selected": ["6"]})  # 0.5 部分
        assert r.dimension_scores["correct"] == 0.5
        trace = build_scoring_trace("exact_match", r)
        _assert_correct_contract(trace, expected=False)


class TestKeypointHitCorrect:
    """keypoint_hit：全命中/未命中."""

    def test_all_hit(self):
        iv = _iv("short_answer", "keypoint_hit", {
            "keypoints": [
                {"id": "k1", "patterns": ["周长"], "score": 1.0},
                {"id": "k2", "patterns": ["边长"], "score": 1.0},
            ],
        })
        r = run_scorer(iv, {"text": "周长等于边长乘4"})
        trace = build_scoring_trace("keypoint_hit", r)
        _assert_correct_contract(trace, expected=True)

    def test_miss(self):
        iv = _iv("short_answer", "keypoint_hit", {
            "keypoints": [
                {"id": "k1", "patterns": ["周长"], "score": 1.0},
                {"id": "k2", "patterns": ["面积"], "score": 1.0,
                 "error_type_id": "et_area_confuse"},
            ],
        })
        r = run_scorer(iv, {"text": "周长是边长乘4"})
        trace = build_scoring_trace("keypoint_hit", r)
        _assert_correct_contract(trace, expected=False)


class TestStepwiseRubricCorrect:
    """stepwise_rubric：部分分（correct<1.0 → False）."""

    def test_partial_steps_marks_wrong(self):
        iv = _iv("stepwise_process", "stepwise_rubric", {
            "steps": [
                {"step_id": "s1", "scorer": "math_equivalence",
                 "scorer_params": {"answer_expr": "7"}, "max_score": 2},
                {"step_id": "s2", "scorer": "exact_match",
                 "scorer_params": {"answer": "B"}, "max_score": 3},
            ],
        })
        r = run_scorer(
            iv,
            {"steps": [
                {"step_id": "s1", "response": {"value": "7"}},
                {"step_id": "s2", "response": {"selected": "A"}},
            ]},
            pack_id="subject-math",
        )
        # total=2/5=0.4 <1.0 → 错
        assert r.dimension_scores["correct"] < 1.0
        trace = build_scoring_trace("stepwise_rubric", r)
        _assert_correct_contract(trace, expected=False)


class TestMathEquivalenceCorrect:
    """math_equivalence（subject-math 桶）：对/错."""

    def test_correct_numeric(self):
        iv = _iv("numeric_blank", "math_equivalence", {"answer_expr": "1/2"})
        r = run_scorer(
            iv, {"blanks": {"b1": {"value": "0.5"}}}, pack_id="subject-math"
        )
        trace = build_scoring_trace("math_equivalence", r)
        _assert_correct_contract(trace, expected=True)

    def test_wrong_numeric(self):
        iv = _iv("numeric_blank", "math_equivalence", {"answer_expr": "8"})
        r = run_scorer(
            iv, {"blanks": {"b1": {"value": "9"}}}, pack_id="subject-math"
        )
        trace = build_scoring_trace("math_equivalence", r)
        _assert_correct_contract(trace, expected=False)


class TestAIRubricCorrect:
    """ai_rubric（platform 桶）：mock LLM 响应.

    ai_rubric 成功时 dimension_scores 不含 'correct' 键（含 dim_id 与 total），
    build_scoring_trace 按 get('correct', 0.0) → 0.0 → process.correct=False。
    这是技术正确的：作文评分无对错二值，复习排程不消费作文 correct。
    """

    @staticmethod
    def _make_mock_client() -> object:
        from src.core.ai.bus.models import AIResult

        class _MockLLMClient:
            def complete(self, prompt, *, model, temperature, max_tokens):
                return AIResult(
                    content=json.dumps(
                        {"dimensions": [
                            {"id": "content", "score": 5,
                             "rationale": "主题明确", "confidence": 0.9},
                            {"id": "structure", "score": 4,
                             "rationale": "段落清晰", "confidence": 0.85},
                        ]},
                        ensure_ascii=False,
                    ),
                    model=model,
                    token_in=len(prompt),
                    token_out=10,
                    duration_ms=0.5,
                )

        return _MockLLMClient()

    def test_ai_rubric_process_correct_is_bool(self):
        scorer = get_scorer("ai_rubric")
        rubric_params = {
            "dimensions": [
                {"id": "content", "name": "内容",
                 "anchors": ["主题明确", "主题基本明确", "主题模糊"],
                 "score_bands": [
                     {"level": 1, "label": "优秀", "score": 5},
                     {"level": 2, "label": "合格", "score": 3},
                     {"level": 3, "label": "待改进", "score": 1},
                 ],
                 "error_type_rules": []},
            ],
            "total_max_score": 5,
        }
        iv = {
            "item_version_id": "sha256:ai-rubric-iv",
            "interaction_ref": {"interaction_id": "writing", "interaction_params": {}},
            "scoring_ref": {
                "scorer_id": "ai_rubric",
                "scorer_params": {
                    "rubric": rubric_params,
                    "clients": {"deepseek": self._make_mock_client()},
                },
            },
            "objective": {"gradeband": "M"},
            "error_bindings": [],
        }
        r = run_scorer(iv, {"text": "春天来了。"})
        trace = build_scoring_trace("ai_rubric", r)
        # ai_rubric 无 correct 维度 → process.correct=False（bool 契约仍满足）
        _assert_correct_contract(trace, expected=False)


# ════════════════════════════════════════════════════════════════════
# §1 通用：correct 键与 dimension_scores 口径一致
# ════════════════════════════════════════════════════════════════════

def test_correct_consistent_with_dimension_scores():
    """process.correct 必须与 dimension_scores['correct'] >= 1.0 口径一致."""
    for correct_value in (0.0, 0.5, 0.99, 1.0, 1.5):
        result = ScoreResult(
            dimension_scores={"correct": correct_value},
            error_inferences=[],
            confidence={"scoring": 1.0},
            evidence={"note": "test"},
            scorer_version="1.0.0+test",
        )
        trace = build_scoring_trace("exact_match", result)
        expected = float(correct_value) >= 1.0
        assert trace["process"]["correct"] is expected, (
            f"correct_value={correct_value} 期望 {expected}，"
            f"实际 {trace['process']['correct']}"
        )


def test_correct_overrides_evidence_correct_key():
    """evidence 含同名 correct 键时，build_scoring_trace 汇总口径覆盖之.

    设计意图（service.py 注释）：避免评分器自报与汇总口径不一致；
    消费侧只信任 dimension_scores['correct'] 派生的 bool。
    """
    result = ScoreResult(
        dimension_scores={"correct": 1.0},
        error_inferences=[],
        confidence={"scoring": 1.0},
        evidence={"correct": False, "note": "脏数据"},
        scorer_version="1.0.0+test",
    )
    trace = build_scoring_trace("exact_match", result)
    # 汇总口径（dimension_scores['correct']=1.0 → True）覆盖 evidence 的 False
    assert trace["process"]["correct"] is True


def test_correct_missing_dimension_scores_defaults_false():
    """dimension_scores 缺 correct 键时 → process.correct=False（错题口径）."""
    result = ScoreResult(
        dimension_scores={"total": 8.0},  # 无 correct 键
        error_inferences=[],
        confidence={"scoring": 1.0},
        evidence={"note": "无对错维度"},
        scorer_version="1.0.0+test",
    )
    trace = build_scoring_trace("ai_rubric", result)
    _assert_correct_contract(trace, expected=False)


# ════════════════════════════════════════════════════════════════════
# §2 response_event 落账后 process.correct 可回读（DB 验证）
# ════════════════════════════════════════════════════════════════════

@pytest_asyncio.fixture(autouse=True)
async def _truncate_response_event(async_session: AsyncSession):
    await async_session.execute(
        text("TRUNCATE TABLE response_event RESTART IDENTITY CASCADE")
    )
    await async_session.commit()
    yield


async def test_response_event_persists_process_correct(async_session: AsyncSession):
    """§2 response_event 写入后，scoring_trace.process.correct 存在且非空."""
    iv = {
        "item_version_id": "sha256:persist-correct-iv",
        "interaction_ref": {"interaction_id": "single_choice", "interaction_params": {}},
        "scoring_ref": {"scorer_id": "exact_match", "scorer_params": {"answer": "B"}},
        "error_bindings": [],
    }
    outcome = await score_and_record(
        async_session,
        item_version=iv,
        response={"selected": "B"},
        student_alias_id=uuid4(),
        scene="practice",
        now=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
    )
    assert outcome.correct is True

    row = (
        await async_session.execute(
            text(
                "SELECT scoring_trace FROM response_event WHERE event_id = :eid"
            ),
            {"eid": outcome.event_id},
        )
    ).one()
    trace = row._mapping["scoring_trace"]
    # 落账后仍可回读 process.correct（bool，非空）
    assert isinstance(trace["process"]["correct"], bool)
    assert trace["process"]["correct"] is True


async def test_response_event_persists_wrong_answer(async_session: AsyncSession):
    """§2 答错事件 process.correct=False 落账可回读."""
    iv = {
        "item_version_id": "sha256:persist-wrong-iv",
        "interaction_ref": {"interaction_id": "single_choice", "interaction_params": {}},
        "scoring_ref": {"scorer_id": "exact_match", "scorer_params": {"answer": "B"}},
        "error_bindings": [
            {"option_value": "A", "label": "x", "error_type_id": "et_x"},
        ],
    }
    outcome = await score_and_record(
        async_session,
        item_version=iv,
        response={"selected": "A"},
        student_alias_id=uuid4(),
        scene="practice",
        now=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
    )
    assert outcome.correct is False
    row = (
        await async_session.execute(
            text("SELECT scoring_trace FROM response_event WHERE event_id = :eid"),
            {"eid": outcome.event_id},
        )
    ).one()
    trace = row._mapping["scoring_trace"]
    assert trace["process"]["correct"] is False
