"""T-W2-004 确定性实例化引擎单元测试.

覆盖范围（验收 §1-§4）：
  - 返回完整 ItemVersion dict（六大块字段齐全）
  - A/B 级 item_version_id 与公式一一致；两次相同输入同 id
  - 3 个黄金样例（单选/数值填空/匹配）逐字节回归
  - 不 import 学科包
  - 异常路径：spec 不合规、参数未知槽、表达式求值失败、干扰项碰撞
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from src.core.instantiation.engine import (
    ENGINE_DIGEST,
    ItemVersionResult,
    instantiate,
    normalize_params,
)
from src.core.instantiation.dsl.schema import ItemTemplateSpec, Slot

# ────────────────────────────────────────────────────────────────────
# 黄金样例加载
# ────────────────────────────────────────────────────────────────────

_GOLDEN_DIR = Path(__file__).resolve().parent.parent / "golden" / "instantiation"
_GOLDEN_FILES = sorted(_GOLDEN_DIR.glob("sample_*.yaml"))


def _load_golden(path: Path) -> dict[str, Any]:
    """加载一个黄金样例 yaml."""
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


@pytest.fixture(scope="module")
def golden_cases() -> list[tuple[str, dict[str, Any]]]:
    """加载全部黄金样例."""
    return [(p.stem, _load_golden(p)) for p in _GOLDEN_FILES]


# ────────────────────────────────────────────────────────────────────
# 验收 §1：返回完整 ItemVersion dict
# ────────────────────────────────────────────────────────────────────


class TestItemVersionStructure:
    """实例化产物结构与六大块齐全."""

    @pytest.fixture
    def single_choice_case(self, golden_cases) -> dict[str, Any]:
        return next(c for _, c in golden_cases if c["case_id"] == "single_choice_addition")

    def test_returns_result_type(self, single_choice_case: dict) -> None:
        result = instantiate(
            single_choice_case["template_version"],
            single_choice_case["params"],
            pack_digest=single_choice_case["pack_digest"],
            interaction_id=single_choice_case["interaction_id"],
            scorer_id=single_choice_case["scorer_id"],
            scorer_params=single_choice_case["scorer_params"],
            locale=single_choice_case["locale"],
            corpus_digests=single_choice_case["corpus_digests"],
            seed=single_choice_case["seed"],
            signed_at="2026-07-27T00:00:00+00:00",  # 固定时间便于回归
        )
        assert isinstance(result, ItemVersionResult)

    def test_six_blocks_present(self, single_choice_case: dict) -> None:
        result = instantiate(
            single_choice_case["template_version"],
            single_choice_case["params"],
            pack_digest=single_choice_case["pack_digest"],
            interaction_id=single_choice_case["interaction_id"],
            scorer_id=single_choice_case["scorer_id"],
            scorer_params=single_choice_case["scorer_params"],
            locale=single_choice_case["locale"],
            corpus_digests=single_choice_case["corpus_digests"],
            seed=single_choice_case["seed"],
            signed_at="2026-07-27T00:00:00+00:00",
        )
        # 验收 §1：含 objective/interaction_ref/content/scoring_ref/error_bindings/lineage
        assert result.objective
        assert result.interaction_ref
        assert result.content
        assert result.scoring_ref
        assert isinstance(result.error_bindings, list)
        assert result.lineage
        # item_version_id 与 item_id
        assert result.item_version_id.startswith("sha256:")
        # A/B 级：item_id = item_version_id（自引用）
        assert result.item_id == result.item_version_id
        # status 默认 draft
        assert result.status == "draft"

    def test_objective_carried_from_template(self, single_choice_case: dict) -> None:
        result = instantiate(
            single_choice_case["template_version"],
            single_choice_case["params"],
            pack_digest=single_choice_case["pack_digest"],
            interaction_id=single_choice_case["interaction_id"],
            scorer_id=single_choice_case["scorer_id"],
            locale=single_choice_case["locale"],
            signed_at="2026-07-27T00:00:00+00:00",
        )
        spec = single_choice_case["template_version"]["spec"]
        assert result.objective["kp_set"][0]["code"] == spec["objective"]["kp_set"][0]["code"]
        assert result.objective["cognitive_level"] == spec["objective"]["cognitive_level"]

    def test_content_interpolated(self, single_choice_case: dict) -> None:
        result = instantiate(
            single_choice_case["template_version"],
            single_choice_case["params"],
            pack_digest=single_choice_case["pack_digest"],
            interaction_id=single_choice_case["interaction_id"],
            scorer_id=single_choice_case["scorer_id"],
            locale=single_choice_case["locale"],
            signed_at="2026-07-27T00:00:00+00:00",
        )
        # 单选样例模板 "{a} + {b} = ?" → "3 + 4 = ?"
        rendered = result.content["blocks"][0]["rendered"]
        assert rendered == "3 + 4 = ?"

    def test_error_bindings_with_options(self, single_choice_case: dict) -> None:
        result = instantiate(
            single_choice_case["template_version"],
            single_choice_case["params"],
            pack_digest=single_choice_case["pack_digest"],
            interaction_id=single_choice_case["interaction_id"],
            scorer_id=single_choice_case["scorer_id"],
            locale=single_choice_case["locale"],
            signed_at="2026-07-27T00:00:00+00:00",
        )
        # 2 条 deterministic 规则，每条 1 个 option → 2 个 error_binding
        assert len(result.error_bindings) == 2
        eb = result.error_bindings[0]
        assert "option_value" in eb
        assert "error_type_id" in eb
        assert "label" in eb
        # a=3, b=4 → 干扰项 = 8（多 1）或 6（少 1）
        assert eb["option_value"] in (7 + 1, 7 - 1)

    def test_lineage_tier_a(self, single_choice_case: dict) -> None:
        result = instantiate(
            single_choice_case["template_version"],
            single_choice_case["params"],
            pack_digest=single_choice_case["pack_digest"],
            interaction_id=single_choice_case["interaction_id"],
            scorer_id=single_choice_case["scorer_id"],
            locale=single_choice_case["locale"],
            signed_at="2026-07-27T00:00:00+00:00",
        )
        assert result.lineage["tier"] == "A"
        assert result.lineage["pipeline"]["id"] == "instantiation-engine"
        assert result.lineage["template_version_id"] == single_choice_case["template_version"]["template_version_id"]
        assert result.lineage["seed"] == single_choice_case["seed"]


# ────────────────────────────────────────────────────────────────────
# 验收 §2：确定性——同输入同 id
# ────────────────────────────────────────────────────────────────────


class TestDeterminism:
    """同一 (template, params, pack, engine, corpus, locale) 必得同一 id."""

    def test_same_input_same_id_all_golden(self, golden_cases: list) -> None:
        """遍历全部黄金样例，验证任意输入两次实例化必得同一 id（D3）."""
        for stem, case in golden_cases:
            kwargs = dict(
                pack_digest=case["pack_digest"],
                interaction_id=case["interaction_id"],
                scorer_id=case["scorer_id"],
                scorer_params=case.get("scorer_params"),
                locale=case["locale"],
                corpus_digests=case["corpus_digests"],
                seed=case["seed"],
                signed_at="2026-07-27T00:00:00+00:00",
            )
            r1 = instantiate(case["template_version"], case["params"], **kwargs)
            r2 = instantiate(case["template_version"], case["params"], **kwargs)
            assert r1.item_version_id == r2.item_version_id, (
                f"黄金样例 {case['case_id']} 两次实例化 id 不一致："
                f"{r1.item_version_id} vs {r2.item_version_id}"
            )

    def test_different_params_different_id(self, golden_cases: list) -> None:
        case = next(c for _, c in golden_cases if c["case_id"] == "single_choice_addition")
        kwargs = dict(
            pack_digest=case["pack_digest"],
            interaction_id=case["interaction_id"],
            scorer_id=case["scorer_id"],
            locale=case["locale"],
            signed_at="2026-07-27T00:00:00+00:00",
        )
        r1 = instantiate(case["template_version"], {"a": 3, "b": 4}, **kwargs)
        r2 = instantiate(case["template_version"], {"a": 5, "b": 6}, **kwargs)
        assert r1.item_version_id != r2.item_version_id

    def test_different_locale_different_id(self, golden_cases: list) -> None:
        case = next(c for _, c in golden_cases if c["case_id"] == "single_choice_addition")
        kwargs = dict(
            pack_digest=case["pack_digest"],
            interaction_id=case["interaction_id"],
            scorer_id=case["scorer_id"],
            signed_at="2026-07-27T00:00:00+00:00",
        )
        r1 = instantiate(case["template_version"], case["params"], locale="zh-CN", **kwargs)
        r2 = instantiate(case["template_version"], case["params"], locale="en-US", **kwargs)
        assert r1.item_version_id != r2.item_version_id


# ────────────────────────────────────────────────────────────────────
# 验收 §3：3 个黄金样例逐字节回归
# ────────────────────────────────────────────────────────────────────


class TestGoldenRegression:
    """黄金样例 expected_item_version_id 回归.

    每个样例的 expected_item_version_id 必须已填入实际 id；
    若为空（首次开发未回填），assert 失败并打印实际 id 供回填——
    不使用 pytest 跳过机制绕过失败（违反 X1）。
    """

    def _assert_golden(self, case: dict, golden_cases: list) -> None:
        """公共断言：实例化结果与 expected_item_version_id 逐字节一致."""
        result = instantiate(
            case["template_version"],
            case["params"],
            pack_digest=case["pack_digest"],
            interaction_id=case["interaction_id"],
            scorer_id=case["scorer_id"],
            scorer_params=case.get("scorer_params"),
            locale=case["locale"],
            corpus_digests=case["corpus_digests"],
            seed=case["seed"],
            signed_at="2026-07-27T00:00:00+00:00",
        )
        expected = case.get("expected_item_version_id", "")
        assert result.item_version_id == expected, (
            f"黄金样例 {case['case_id']} item_version_id 不匹配："
            f"expected={expected!r}, actual={result.item_version_id!r}；"
            f"若 expected 为空，请将 actual 值填回 yaml"
        )

    def test_single_choice_golden(self, golden_cases: list) -> None:
        case = next(c for _, c in golden_cases if c["case_id"] == "single_choice_addition")
        self._assert_golden(case, golden_cases)

    def test_numeric_blank_golden(self, golden_cases: list) -> None:
        case = next(c for _, c in golden_cases if c["case_id"] == "numeric_blank_pythagorean")
        self._assert_golden(case, golden_cases)

    def test_matching_golden(self, golden_cases: list) -> None:
        case = next(c for _, c in golden_cases if c["case_id"] == "matching_number_hanzi")
        self._assert_golden(case, golden_cases)

    def test_three_golden_cases_count(self, golden_cases: list) -> None:
        """验收 §3：至少 3 个黄金样例（单选/数值填空/匹配各 1）."""
        assert len(golden_cases) >= 3
        case_ids = {c["case_id"] for _, c in golden_cases}
        assert "single_choice_addition" in case_ids
        assert "numeric_blank_pythagorean" in case_ids
        assert "matching_number_hanzi" in case_ids


# ────────────────────────────────────────────────────────────────────
# 异常路径
# ────────────────────────────────────────────────────────────────────


class TestErrorPaths:
    """异常路径覆盖."""

    def _minimal_spec(self) -> dict[str, Any]:
        return {
            "objective": {
                "kp_set": [{"dimension": "kp", "code": "test.x"}],
                "kp_set_mode": "single",
                "cognitive_level": "remember",
                "gradeband": "L",
                "graph_release": "2026.1",
            },
            "slots": {"a": {"type": "int", "difficulty_relevant": False}},
            "variation_axes": {"axes": []},
            "presentation": {"blocks": [{"kind": "text", "template": "{a}"}]},
            "answer_program": {"expression": "a + 1", "returns": "number"},
            "distractor_rules": {"rules": []},
        }

    def _minimal_tv(self, spec: dict | None = None) -> dict:
        return {
            "template_version_id": "sha256:test-tv-001",
            "template_id": "tpl-test-001",
            "dsl_version": "1",
            "spec": spec or self._minimal_spec(),
        }

    def test_unknown_slot_rejected(self) -> None:
        with pytest.raises(ValueError, match="未知槽名"):
            instantiate(
                self._minimal_tv(),
                {"a": 1, "unknown_slot": 99},
                pack_digest="sha256:p",
                interaction_id="single_choice",
                scorer_id="exact_match",
                signed_at="2026-07-27T00:00:00+00:00",
            )

    def test_missing_required_slot_field(self) -> None:
        """spec 缺必填块 → Pydantic ValidationError."""
        from pydantic import ValidationError
        bad_spec = self._minimal_spec()
        del bad_spec["objective"]
        with pytest.raises(ValidationError):
            instantiate(
                self._minimal_tv(bad_spec),
                {"a": 1},
                pack_digest="sha256:p",
                interaction_id="single_choice",
                scorer_id="exact_match",
                signed_at="2026-07-27T00:00:00+00:00",
            )

    def test_answer_program_eval_failure(self) -> None:
        spec = self._minimal_spec()
        spec["answer_program"]["expression"] = "1 / 0"
        with pytest.raises(ValueError, match="answer_program"):
            instantiate(
                self._minimal_tv(spec),
                {"a": 1},
                pack_digest="sha256:p",
                interaction_id="single_choice",
                scorer_id="exact_match",
                signed_at="2026-07-27T00:00:00+00:00",
            )

    def test_distractor_collision_rejected(self) -> None:
        """干扰项与正解碰撞 → ValueError 包装."""
        spec = self._minimal_spec()
        # a=1 → 正解=2；干扰项 a+1=2 → 碰撞
        spec["distractor_rules"] = {
            "rules": [
                {
                    "rule_type": "deterministic",
                    "error_type_id": "err.x",
                    "expression": "a + 1",
                }
            ]
        }
        with pytest.raises(ValueError, match="碰撞"):
            instantiate(
                self._minimal_tv(spec),
                {"a": 1},
                pack_digest="sha256:p",
                interaction_id="single_choice",
                scorer_id="exact_match",
                signed_at="2026-07-27T00:00:00+00:00",
            )

    def test_template_version_missing_id(self) -> None:
        tv = self._minimal_tv()
        del tv["template_version_id"]
        with pytest.raises(ValueError, match="template_version_id"):
            instantiate(
                tv,
                {"a": 1},
                pack_digest="sha256:p",
                interaction_id="single_choice",
                scorer_id="exact_match",
                signed_at="2026-07-27T00:00:00+00:00",
            )

    def test_invalid_template_version_type(self) -> None:
        with pytest.raises(ValueError, match="template_version 必须为"):
            instantiate(
                "not a dict",  # type: ignore[arg-type]
                {"a": 1},
                pack_digest="sha256:p",
                interaction_id="single_choice",
                scorer_id="exact_match",
                signed_at="2026-07-27T00:00:00+00:00",
            )

    def test_presentation_template_missing_slot(self) -> None:
        spec = self._minimal_spec()
        spec["presentation"]["blocks"][0]["template"] = "{a} + {b}"
        # spec.slots 没有 b，但 params 也没传 b
        # 这里测试模板引用了未声明槽 → 不会触发（normalize_params 只校验 params）
        # 真正会触发的是：模板引用 {b}，slots 里有 b，但 params 没传 b
        spec["slots"]["b"] = {"type": "int", "difficulty_relevant": False}
        # 引擎把 KeyError 包装为 ValueError（保留原异常 __cause__），
        # 断言 ValueError 且消息含"未提供的槽"——既验证错误传播也验证错误信息可读性
        with pytest.raises(ValueError, match="未提供的槽"):
            instantiate(
                self._minimal_tv(spec),
                {"a": 1},  # 缺 b
                pack_digest="sha256:p",
                interaction_id="single_choice",
                scorer_id="exact_match",
                signed_at="2026-07-27T00:00:00+00:00",
            )


# ────────────────────────────────────────────────────────────────────
# 验收 §5：normalize_params 与规范化
# ────────────────────────────────────────────────────────────────────


class TestNormalizeParams:
    """参数规范化."""

    def test_int_normalization(self) -> None:
        slots = {"a": Slot(type="int", difficulty_relevant=False)}
        assert normalize_params({"a": 5}, slots) == {"a": 5}
        assert normalize_params({"a": "5"}, slots) == {"a": 5}
        assert normalize_params({"a": 5.0}, slots) == {"a": 5}

    def test_decimal_normalization(self) -> None:
        slots = {"a": Slot(type="decimal", difficulty_relevant=False)}
        # '3.14' 字符串 → Decimal('3.14') → '3.14'
        assert normalize_params({"a": "3.14"}, slots) == {"a": "3.14"}
        # 3.14 float → Decimal(str(3.14)) = Decimal('3.14') → '3.14'
        assert normalize_params({"a": 3.14}, slots) == {"a": "3.14"}
        # 3.10 → Decimal('3.1') → '3.1'（去尾零）
        assert normalize_params({"a": "3.10"}, slots) == {"a": "3.1"}

    def test_fraction_normalization(self) -> None:
        slots = {"a": Slot(type="fraction", difficulty_relevant=False)}
        # "3/4" → "3/4"
        assert normalize_params({"a": "3/4"}, slots) == {"a": "3/4"}
        # "0.75" → Fraction(3, 4) → "3/4"
        assert normalize_params({"a": "0.75"}, slots) == {"a": "3/4"}
        # "6/8" 约分 → "3/4"
        assert normalize_params({"a": "6/8"}, slots) == {"a": "3/4"}

    def test_string_normalization(self) -> None:
        slots = {"a": Slot(type="string", difficulty_relevant=False)}
        assert normalize_params({"a": "hello"}, slots) == {"a": "hello"}
        assert normalize_params({"a": 123}, slots) == {"a": "123"}

    def test_bool_normalization(self) -> None:
        slots = {"a": Slot(type="bool", difficulty_relevant=False)}
        assert normalize_params({"a": True}, slots) == {"a": True}
        assert normalize_params({"a": False}, slots) == {"a": False}

    def test_choice_normalization(self) -> None:
        slots = {"a": Slot(type="choice", difficulty_relevant=False)}
        assert normalize_params({"a": "opt_b"}, slots) == {"a": "opt_b"}

    def test_unknown_slot_rejected(self) -> None:
        slots = {"a": Slot(type="int", difficulty_relevant=False)}
        with pytest.raises(ValueError, match="未知槽名"):
            normalize_params({"a": 1, "b": 2}, slots)

    def test_non_dict_params_rejected(self) -> None:
        slots = {"a": Slot(type="int", difficulty_relevant=False)}
        with pytest.raises(ValueError, match="params 必须为 dict"):
            normalize_params(["a", 1], slots)  # type: ignore[arg-type]

    def test_decimal_determinism(self) -> None:
        """同一浮点输入任意次规范化结果一致（避免浮点漂移）."""
        slots = {"a": Slot(type="decimal", difficulty_relevant=False)}
        results = [normalize_params({"a": 0.1 + 0.2}, slots) for _ in range(5)]
        assert all(r == results[0] for r in results)
        # 0.1+0.2 在 float 是 0.30000000000000004；str 化后是 "0.30000000000000004"
        assert results[0]["a"] == str(0.1 + 0.2)

    def test_fraction_determinism(self) -> None:
        slots = {"a": Slot(type="fraction", difficulty_relevant=False)}
        results = [normalize_params({"a": "1/3"}, slots) for _ in range(5)]
        assert all(r == results[0] for r in results)
        assert results[0]["a"] == "1/3"


# ────────────────────────────────────────────────────────────────────
# 验收 §5：不 import 学科包
# ────────────────────────────────────────────────────────────────────


def test_no_subject_package_imports() -> None:
    """静态检查：engine 模块不 import 任何学科包/学段包（宪法 X6）."""
    import src.core.instantiation.engine.engine as engine_mod
    import inspect
    src_text = inspect.getsource(engine_mod)
    forbidden_patterns = [
        "import subject_",
        "import gradeband_",
        "from subject_",
        "from gradeband_",
        "import packs",
        "from packs",
    ]
    for pat in forbidden_patterns:
        assert pat not in src_text, f"违反 X6：发现禁用 import 模式 {pat!r}"


# ────────────────────────────────────────────────────────────────────
# 公式一与 ENGINE_DIGEST 不变性
# ────────────────────────────────────────────────────────────────────


class TestEngineDigest:
    """ENGINE_DIGEST 是确定性常量."""

    def test_engine_digest_format(self) -> None:
        assert ENGINE_DIGEST.startswith("sha256:")
        # sha256: + 64 hex chars
        assert len(ENGINE_DIGEST) == len("sha256:") + 64

    def test_engine_digest_deterministic(self) -> None:
        """同一版本引擎摘要恒定."""
        import src.core.instantiation.engine.engine as engine_mod
        # 重新计算应得同一值
        import hashlib
        expected = "sha256:" + hashlib.sha256(engine_mod.ENGINE_VERSION.encode()).hexdigest()
        assert engine_mod.ENGINE_DIGEST == expected
