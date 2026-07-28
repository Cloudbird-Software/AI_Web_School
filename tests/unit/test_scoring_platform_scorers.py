"""W3-S4 评分器注册表 + 平台三评分器单元测试.

覆盖：
- 注册表机制：import 即注册 platform 三评分器 / 学科桶回退 / 未注册 KeyError /
  非法对象 TypeError。
- exact_match：单选（含 int 答案 vs str 作答）/多选（集合+分项）/排序（序列）/
  文本与数值填空（逐空）/匹配/规范化（全角/大小写）/缺 answer 置信度 0。
- keypoint_hit：全命中/未命中推断（error_type_id+置信度）/min_pass/正则/
  blanks 文本提取/缺 keypoints 置信度 0。
- stepwise_rubric：逐步判分汇总/缺步 missing_step/子评分器跨桶查找
  （math_equivalence 学科桶）/缺 steps 置信度 0。
- infer_option_errors：选择题选项→错误类型映射（rule_version=item_version_id）/
  非选择题不映射/多选去重。
- build_scoring_trace：契约 §3 结构（四层置信度之评分层）。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

import src.core.scoring.platform_scorers  # noqa: F401 —— import 即注册 platform 评分器
from src.core.scoring.platform_scorers import (
    ExactMatchScorer,
    KeypointHitScorer,
    StepwiseRubricScorer,
    normalize_text,
)
from src.core.scoring.registry import (
    ScoreResult,
    get_scorer,
    list_scorers,
    register_scorer,
)
from src.core.scoring.service import (
    build_scoring_trace,
    infer_option_errors,
    run_scorer,
)

# ────────────────────────────────────────────────────────────────────
# 数学包评分器注册（importlib 加载连字符目录，触发 register_scorer）
# ────────────────────────────────────────────────────────────────────

_MATH_SCORERS_PATH = (
    Path(__file__).resolve().parents[2]
    / "src" / "packs" / "subject-math" / "scorers" / "__init__.py"
)


def _register_math_scorers() -> None:
    spec = importlib.util.spec_from_file_location(
        "subject_math_scorers_pkg", _MATH_SCORERS_PATH
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["subject_math_scorers_pkg"] = mod
    spec.loader.exec_module(mod)
    mod.register_math_scorers()


_register_math_scorers()


# ────────────────────────────────────────────────────────────────────
# 构造 item_version dict 的辅助
# ────────────────────────────────────────────────────────────────────

def _iv(
    interaction_id: str,
    scorer_id: str,
    scorer_params: dict,
    *,
    error_bindings: list | None = None,
    item_version_id: str = "sha256:test-iv",
) -> dict:
    return {
        "item_version_id": item_version_id,
        "interaction_ref": {"interaction_id": interaction_id, "interaction_params": {}},
        "scoring_ref": {"scorer_id": scorer_id, "scorer_params": scorer_params},
        "error_bindings": error_bindings or [],
    }


# ════════════════════════════════════════════════════════════════════
# 注册表机制
# ════════════════════════════════════════════════════════════════════

def test_platform_scorers_registered_on_import():
    """import platform_scorers 即注册三个 platform 评分器."""
    assert set(list_scorers("platform")) >= {
        "exact_match", "keypoint_hit", "stepwise_rubric",
    }


def test_math_scorer_registered_in_subject_bucket():
    """数学包 register_math_scorers 把 math_equivalence 注入 subject-math 桶."""
    assert "math_equivalence" in list_scorers("subject-math")
    scorer = get_scorer("math_equivalence", "subject-math")
    assert scorer.scorer_id == "math_equivalence"


def test_get_scorer_platform_fallback():
    """学科桶未命中时回退 platform 桶."""
    scorer = get_scorer("exact_match", "subject-chinese")
    assert isinstance(scorer, ExactMatchScorer)


def test_get_scorer_unknown_raises():
    with pytest.raises(KeyError):
        get_scorer("nonexistent_scorer", "subject-math")


def test_register_scorer_rejects_invalid():
    class _NoAttrs:
        pass

    with pytest.raises(TypeError):
        register_scorer("platform", _NoAttrs())


# ════════════════════════════════════════════════════════════════════
# exact_match
# ════════════════════════════════════════════════════════════════════

class TestExactMatch:
    def test_single_choice_correct(self):
        iv = _iv("single_choice", "exact_match", {"answer": "B"})
        r = run_scorer(iv, {"selected": "B"})
        assert r.dimension_scores["correct"] == 1.0
        assert r.confidence["scoring"] == 1.0

    def test_single_choice_wrong(self):
        iv = _iv("single_choice", "exact_match", {"answer": "B"})
        r = run_scorer(iv, {"selected": "A"})
        assert r.dimension_scores["correct"] == 0.0
        assert r.error_inferences == []  # 选项映射不在评分器内（service 职责）

    def test_single_choice_int_answer_vs_str_response(self):
        """黄金样例形态：answer=7（int），selected='7'（str）→ 判对."""
        iv = _iv("single_choice", "exact_match", {"answer": 7})
        r = run_scorer(iv, {"selected": "7"})
        assert r.dimension_scores["correct"] == 1.0

    def test_multi_choice_set_compare(self):
        iv = _iv("multi_choice", "exact_match", {"answer": ["6", "9"]})
        assert run_scorer(iv, {"selected": ["9", "6"]}).dimension_scores["correct"] == 1.0
        assert run_scorer(iv, {"selected": ["6"]}).dimension_scores["correct"] == 0.0
        # 多选错误项 → 判错
        assert run_scorer(
            iv, {"selected": ["6", "9", "7"]}
        ).dimension_scores["correct"] == 0.0

    def test_multi_choice_partial_credit(self):
        iv = _iv(
            "multi_choice", "exact_match",
            {"answer": ["6", "9"], "partial_credit": {"per_item": True}},
        )
        r = run_scorer(iv, {"selected": ["6"]})
        assert r.dimension_scores["correct"] == 0.5
        assert r.dimension_scores["part:6"] == 1.0
        assert r.dimension_scores["part:9"] == 0.0

    def test_ordering_sequence(self):
        iv = _iv("ordering", "exact_match", {"answer": ["a", "b", "c"]})
        assert run_scorer(
            iv, {"sequence": ["a", "b", "c"]}
        ).dimension_scores["correct"] == 1.0
        assert run_scorer(
            iv, {"sequence": ["b", "a", "c"]}
        ).dimension_scores["correct"] == 0.0

    def test_text_blank_per_blank(self):
        iv = _iv(
            "text_blank", "exact_match",
            {"answer": {"b1": "春天", "b2": "花开"}},
        )
        assert run_scorer(
            iv, {"blanks": {"b1": "春天", "b2": "花开"}}
        ).dimension_scores["correct"] == 1.0
        assert run_scorer(
            iv, {"blanks": {"b1": "春天", "b2": "花落"}}
        ).dimension_scores["correct"] == 0.0

    def test_numeric_blank_value_wrapper(self):
        """numeric_blank 的 blanks 值是 {value, unit?} 包装."""
        iv = _iv("numeric_blank", "exact_match", {"answer": {"b1": "42"}})
        r = run_scorer(iv, {"blanks": {"b1": {"value": "42"}}})
        assert r.dimension_scores["correct"] == 1.0

    def test_matching_pairs(self):
        iv = _iv(
            "matching", "exact_match",
            {"answer": {"L1": "R2", "L2": "R1"}},
        )
        r = run_scorer(
            iv, {"pairs": [{"left_id": "L1", "right_id": "R2"},
                           {"left_id": "L2", "right_id": "R1"}]}
        )
        assert r.dimension_scores["correct"] == 1.0

    def test_normalization_fullwidth_and_case(self):
        iv = _iv(
            "text_blank", "exact_match",
            {"answer": {"b1": "ABC7"},
             "normalization": {"fullwidth_to_half": True, "casefold": True}},
        )
        r = run_scorer(iv, {"blanks": {"b1": "ａｂｃ７"}})
        assert r.dimension_scores["correct"] == 1.0

    def test_missing_answer_zero_confidence(self):
        iv = _iv("single_choice", "exact_match", {})
        r = run_scorer(iv, {"selected": "A"})
        assert r.dimension_scores["correct"] == 0.0
        assert r.confidence["scoring"] == 0.0


# ════════════════════════════════════════════════════════════════════
# keypoint_hit
# ════════════════════════════════════════════════════════════════════

class TestKeypointHit:
    def _iv(self, keypoints, **extra):
        params = {"keypoints": keypoints}
        params.update(extra)
        return _iv("short_answer", "keypoint_hit", params)

    def test_all_hit(self):
        iv = self._iv([
            {"id": "k1", "patterns": ["周长"], "score": 1.0},
            {"id": "k2", "patterns": ["边长"], "score": 1.0},
        ])
        r = run_scorer(iv, {"text": "周长等于边长乘4"})
        assert r.dimension_scores["correct"] == 1.0
        assert r.dimension_scores["total"] == 2.0
        assert r.error_inferences == []

    def test_miss_with_error_type_inference(self):
        iv = self._iv([
            {"id": "k1", "patterns": ["周长"], "score": 1.0},
            {"id": "k2", "patterns": ["面积"], "score": 1.0,
             "error_type_id": "et_area_confuse"},
        ])
        r = run_scorer(iv, {"text": "周长是边长乘4"})
        assert r.dimension_scores["correct"] == 0.0
        assert r.dimension_scores["kp:k2"] == 0.0
        assert len(r.error_inferences) == 1
        inf = r.error_inferences[0]
        assert inf["error_type_id"] == "et_area_confuse"
        # 推断层置信度 <1（证据非因果，§4.5）
        assert 0 < inf["confidence"] < 1
        assert inf["rule_version"] == KeypointHitScorer.version
        assert inf["evidence"]["missed_keypoint"] == "k2"

    def test_min_pass(self):
        iv = self._iv(
            [
                {"id": "k1", "patterns": ["周长"], "score": 1.0},
                {"id": "k2", "patterns": ["面积"], "score": 1.0},
            ],
            min_pass=1.0,
        )
        r = run_scorer(iv, {"text": "周长是边长乘4"})
        assert r.dimension_scores["correct"] == 1.0

    def test_regex_pattern(self):
        iv = self._iv([
            {"id": "k1", "patterns": [r"re:面积.*平方"], "score": 1.0},
        ])
        assert run_scorer(
            iv, {"text": "面积是4平方厘米"}
        ).dimension_scores["correct"] == 1.0
        assert run_scorer(
            iv, {"text": "面积是4厘米"}
        ).dimension_scores["correct"] == 0.0

    def test_blanks_text_extraction(self):
        iv = self._iv([{"id": "k1", "patterns": ["春天"], "score": 1.0}])
        r = run_scorer(iv, {"blanks": {"b1": "春天来了"}})
        assert r.dimension_scores["correct"] == 1.0

    def test_empty_keypoints_zero_confidence(self):
        iv = self._iv([])
        r = run_scorer(iv, {"text": "任意"})
        assert r.confidence["scoring"] == 0.0


# ════════════════════════════════════════════════════════════════════
# stepwise_rubric
# ════════════════════════════════════════════════════════════════════

class TestStepwiseRubric:
    def test_two_steps_mixed(self):
        iv = _iv(
            "stepwise_process", "stepwise_rubric",
            {"steps": [
                {"step_id": "s1", "scorer": "math_equivalence",
                 "scorer_params": {"answer_expr": "7"}, "max_score": 2},
                {"step_id": "s2", "scorer": "exact_match",
                 "scorer_params": {"answer": "B"}, "max_score": 3},
            ]},
        )
        r = run_scorer(iv, {"steps": [
            {"step_id": "s1", "response": {"value": "7"}},
            {"step_id": "s2", "response": {"selected": "A"}},
        ]}, pack_id="subject-math")
        assert r.dimension_scores["step:s1"] == 2.0
        assert r.dimension_scores["step:s2"] == 0.0
        assert r.dimension_scores["total"] == 2.0
        assert r.dimension_scores["correct"] == pytest.approx(0.4)

    def test_missing_step_inference(self):
        iv = _iv(
            "stepwise_process", "stepwise_rubric",
            {"steps": [
                {"step_id": "s1", "scorer": "exact_match",
                 "scorer_params": {"answer": "B"}, "max_score": 1},
            ]},
        )
        r = run_scorer(iv, {"steps": []})
        assert r.dimension_scores["step:s1"] == 0.0
        assert any(
            i["error_type_id"] == "missing_step" for i in r.error_inferences
        )

    def test_sub_scorer_inference_carries_step_id(self):
        """子评分器（math_equivalence）的错误推断带 step_id 证据."""
        iv = _iv(
            "stepwise_process", "stepwise_rubric",
            {"steps": [
                {"step_id": "s1", "scorer": "math_equivalence",
                 "scorer_params": {"answer_expr": "8"}, "max_score": 2},
            ]},
        )
        r = run_scorer(iv, {"steps": [
            {"step_id": "s1", "response": {"value": "9"}},
        ]}, pack_id="subject-math")
        assert r.error_inferences
        assert r.error_inferences[0]["evidence"]["step_id"] == "s1"

    def test_empty_steps_zero_confidence(self):
        iv = _iv("stepwise_process", "stepwise_rubric", {})
        r = run_scorer(iv, {"steps": []})
        assert r.confidence["scoring"] == 0.0


# ════════════════════════════════════════════════════════════════════
# infer_option_errors（选择题选项→错误类型映射）
# ════════════════════════════════════════════════════════════════════

class TestInferOptionErrors:
    def test_single_choice_distractor_mapping(self):
        iv = _iv(
            "single_choice", "exact_match", {"answer": "B"},
            error_bindings=[
                {"option_value": "A", "label": "0.3 > 0.4",
                 "error_type_id": "et_comp_flaw"},
            ],
        )
        infs = infer_option_errors(iv, {"selected": "A"})
        assert len(infs) == 1
        inf = infs[0]
        assert inf["error_type_id"] == "et_comp_flaw"
        # rule_version = item_version_id（映射规则随内容寻址版本化）
        assert inf["rule_version"] == "sha256:test-iv"
        assert inf["evidence"]["selected_option"] == "A"
        assert 0 < inf["confidence"] < 1  # 证据非因果

    def test_correct_option_no_inference(self):
        iv = _iv(
            "single_choice", "exact_match", {"answer": "B"},
            error_bindings=[
                {"option_value": "A", "error_type_id": "et_x"},
            ],
        )
        assert infer_option_errors(iv, {"selected": "B"}) == []

    def test_multi_choice_multiple_dedup(self):
        iv = _iv(
            "multi_choice", "exact_match", {"answer": ["6", "9"]},
            error_bindings=[
                {"option_value": "7", "error_type_id": "et_not_multiple"},
                {"option_value": "7", "error_type_id": "et_not_multiple"},
                {"option_value": "10", "error_type_id": "et_carry"},
            ],
        )
        infs = infer_option_errors(iv, {"selected": ["6", "7", "10"]})
        assert {i["error_type_id"] for i in infs} == {"et_not_multiple", "et_carry"}
        assert len(infs) == 2  # 同 (option, error_type) 去重

    def test_non_choice_interaction_no_mapping(self):
        iv = _iv(
            "short_answer", "keypoint_hit", {"keypoints": []},
            error_bindings=[{"option_value": "A", "error_type_id": "et_x"}],
        )
        assert infer_option_errors(iv, {"selected": "A"}) == []


# ════════════════════════════════════════════════════════════════════
# build_scoring_trace（契约 §3）
# ════════════════════════════════════════════════════════════════════

def test_build_scoring_trace_structure():
    result = ScoreResult(
        dimension_scores={"correct": 1.0},
        error_inferences=[],
        confidence={"scoring": 1.0},
        evidence={"note": "判定明细"},
        scorer_version="1.0.0+platform",
    )
    trace = build_scoring_trace("exact_match", result)
    assert trace["scorer_id"] == "exact_match"
    assert trace["scorer_version"] == "1.0.0+platform"
    # T-W4-048：process.correct 显式 bool（复习排程 derive_correctness 优先读）
    assert trace["process"] == {"note": "判定明细", "correct": True}
    # 置信度四层分离：本层只承载评分层
    assert trace["confidence"]["scoring"] == 1.0
    assert "note" in trace["confidence"]


def test_normalize_text():
    assert normalize_text("  春天  花开 ", None) == "春天 花开"
    assert normalize_text("ＡＢＣ７", {"fullwidth_to_half": True}) == "ABC7"
    assert normalize_text("AbC", {"casefold": True}) == "abc"
