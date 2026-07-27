"""W3 S7 英语包：模板 lint / 实例化 / 词表校验 / 词表种子 单元测试.

覆盖任务卡四条验收：
  §1 两个 A 线模板（词汇单选/单词拼写）经 DSL Linter 校验通过，
     且交互/评分器均来自平台注册表（D4）。
  §2 实例化确定性（同输入同 item_version_id，D3）与六大块完整。
  §3 词表等级校验（word_in_vocab）：词表内 pass / 词表外 fail / 缺词 review。
  §4 课标词表种子：≥200 个二级词、词目唯一、license_id 已登记 approved。

实现策略：学科包目录 subject-english 含连字符，用 importlib 加载
验证器与管线模块；模板/词表按文件路径加载。
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

from src.core.content.source_registry import SourceRegistry
from src.core.gate.validator import (
    GateContext,
    register_validator,
)
from src.core.instantiation.dsl.linter import lint
from src.core.instantiation.engine import instantiate

# ────────────────────────────────────────────────────────────────────
# 路径常量与模块加载
# ────────────────────────────────────────────────────────────────────
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_PACK_DIR = _PROJECT_ROOT / "src" / "packs" / "subject-english"
_VOCAB_PATH = _PACK_DIR / "corpora" / "curriculum_words.json"
_TEMPLATE_CHOICE_PATH = _PACK_DIR / "templates" / "vocab_single_choice.yaml"
_TEMPLATE_SPELLING_PATH = _PACK_DIR / "templates" / "word_spelling.yaml"
_VALIDATOR_PATH = _PACK_DIR / "validators" / "word_in_vocab.py"
_PIPELINE_PATH = _PACK_DIR / "english_pipeline.py"


def _load_module(mod_name: str, path: Path):
    """以 importlib 加载连字符目录下的模块."""
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载 {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


_vocab_mod = _load_module("subject_english_word_in_vocab_test", _VALIDATOR_PATH)
WordInVocabValidator = _vocab_mod.WordInVocabValidator
register_validator("subject-english", WordInVocabValidator)

_pipeline = _load_module("subject_english_pipeline_test", _PIPELINE_PATH)

_VOCAB: dict[str, Any] = json.loads(_VOCAB_PATH.read_text(encoding="utf-8"))
_TEMPLATE_CHOICE: dict[str, Any] = yaml.safe_load(
    _TEMPLATE_CHOICE_PATH.read_text(encoding="utf-8")
)
_TEMPLATE_SPELLING: dict[str, Any] = yaml.safe_load(
    _TEMPLATE_SPELLING_PATH.read_text(encoding="utf-8")
)

_COMMON_KWARGS = dict(
    pack_digest="sha256:pack-subject-english-test",
    locale="zh-CN",
    corpus_digests=["sha256:corpus-test"],
    seed=0,
    signed_at="2026-07-27T00:00:00+00:00",
)


def _choice_params(word: str, meaning: str) -> dict[str, str]:
    """最小合法单选参数（干扰释义固定，不依赖词表检索）."""
    return {
        "word": word,
        "meaning": meaning,
        "d1": "干扰甲",
        "d2": "干扰乙",
        "d3": "干扰丙",
        "opt_a": meaning,
        "opt_b": "干扰甲",
        "opt_c": "干扰乙",
        "opt_d": "干扰丙",
        "answer_letter": "A",
    }


# ────────────────────────────────────────────────────────────────────
# §1 模板 lint + 注册表纪律
# ────────────────────────────────────────────────────────────────────


class TestTemplateLint:
    """两个模板 lint 通过；交互/评分器来自注册表."""

    def test_choice_template_lint_passes(self) -> None:
        result = lint(_TEMPLATE_CHOICE["spec"])
        assert result.valid, f"单选模板 lint 失败：{[e.model_dump() for e in result.errors]}"

    def test_spelling_template_lint_passes(self) -> None:
        result = lint(_TEMPLATE_SPELLING["spec"])
        assert result.valid, f"拼写模板 lint 失败：{[e.model_dump() for e in result.errors]}"

    def test_registered_interactions(self) -> None:
        """single_choice / text_blank 均在注册表（D4 只复用）."""
        from src.registry.loader import load_interaction_registry

        reg = load_interaction_registry()
        assert reg.get_interaction("single_choice").status == "active"
        assert reg.get_interaction("text_blank").status == "active"

    def test_registered_scorer(self) -> None:
        from src.registry.loader import load_scorer_registry

        reg = load_scorer_registry()
        assert reg.get_scorer("exact_match").status == "active"


# ────────────────────────────────────────────────────────────────────
# §2 实例化确定性与六大块
# ────────────────────────────────────────────────────────────────────


class TestInstantiation:
    """实例化确定性（D3）与产物结构."""

    def test_same_input_same_id_choice(self) -> None:
        params = _choice_params("apple", "苹果")
        kwargs = dict(
            interaction_id="single_choice",
            scorer_id="exact_match",
            scorer_params={"answer": "A"},
            **_COMMON_KWARGS,
        )
        r1 = instantiate(_TEMPLATE_CHOICE, params, **kwargs)
        r2 = instantiate(_TEMPLATE_CHOICE, params, **kwargs)
        assert r1.item_version_id == r2.item_version_id

    def test_same_input_same_id_spelling(self) -> None:
        params = {"word": "apple", "meaning": "苹果", "hint": "a _ _ _ _"}
        kwargs = dict(
            interaction_id="text_blank",
            scorer_id="exact_match",
            scorer_params={"answer": {"b1": "apple"}},
            **_COMMON_KWARGS,
        )
        r1 = instantiate(_TEMPLATE_SPELLING, params, **kwargs)
        r2 = instantiate(_TEMPLATE_SPELLING, params, **kwargs)
        assert r1.item_version_id == r2.item_version_id

    def test_different_word_different_id(self) -> None:
        kwargs = dict(
            interaction_id="text_blank",
            scorer_id="exact_match",
            scorer_params={"answer": {"b1": "apple"}},
            **_COMMON_KWARGS,
        )
        r1 = instantiate(
            _TEMPLATE_SPELLING,
            {"word": "apple", "meaning": "苹果", "hint": "a _ _ _ _"},
            **kwargs,
        )
        r2 = instantiate(
            _TEMPLATE_SPELLING,
            {"word": "banana", "meaning": "香蕉", "hint": "b _ _ _ _ _"},
            **{**kwargs, "scorer_params": {"answer": {"b1": "banana"}}},
        )
        assert r1.item_version_id != r2.item_version_id

    def test_choice_content_has_options(self) -> None:
        """单选产物 content.blocks 含四个选项文本."""
        r = instantiate(
            _TEMPLATE_CHOICE,
            _choice_params("grape", "葡萄"),
            interaction_id="single_choice",
            scorer_id="exact_match",
            scorer_params={"answer": "A"},
            **_COMMON_KWARGS,
        )
        rendered = [b["rendered"] for b in r.content["blocks"]]
        assert any("葡萄" in t for t in rendered)
        assert any(t.startswith("A. ") for t in rendered)
        assert any(t.startswith("D. ") for t in rendered)
        # 干扰项绑错误类型（R-Q-06）
        assert len(r.error_bindings) == 3
        error_types = {b["error_type_id"] for b in r.error_bindings}
        assert "err.english.vocab.confuse-word-form" in error_types

    def test_spelling_lineage_normalized_word(self) -> None:
        """lineage.params.normalized.word 携带目标词（词表校验依赖此路径）."""
        r = instantiate(
            _TEMPLATE_SPELLING,
            {"word": "banana", "meaning": "香蕉", "hint": "b _ _ _ _ _"},
            interaction_id="text_blank",
            scorer_id="exact_match",
            scorer_params={"answer": {"b1": "banana"}},
            **_COMMON_KWARGS,
        )
        assert r.lineage["params"]["normalized"]["word"] == "banana"
        assert r.interaction_ref["interaction_id"] == "text_blank"
        assert r.scoring_ref["scorer_id"] == "exact_match"


# ────────────────────────────────────────────────────────────────────
# §3 词表等级校验
# ────────────────────────────────────────────────────────────────────


class TestWordInVocabValidator:
    """word_in_vocab：词表内 pass / 词表外 fail / 缺词 review."""

    @pytest.fixture
    def validator(self) -> WordInVocabValidator:
        return WordInVocabValidator()

    def test_pass_when_word_in_vocab(self, validator) -> None:
        ctx = GateContext(
            artifact_type="item",
            pack_id="subject-english",
            artifact_payload={"lineage": {"params": {"normalized": {"word": "apple"}}}},
            target_word="apple",
        )
        result = asyncio_run(validator.validate("ref", ctx))
        assert result.verdict == "pass"
        assert result.evidence["level"] == "二级"

    def test_pass_case_insensitive(self, validator) -> None:
        """大小写不敏感（题面首字母大写不应误判）."""
        ctx = GateContext(
            artifact_type="item",
            pack_id="subject-english",
            artifact_payload={"lineage": {"params": {"normalized": {"word": "Apple"}}}},
            target_word="Apple",
        )
        result = asyncio_run(validator.validate("ref", ctx))
        assert result.verdict == "pass"

    def test_fail_when_word_out_of_vocab(self, validator) -> None:
        """词表外词（超纲词）→ fail."""
        ctx = GateContext(
            artifact_type="item",
            pack_id="subject-english",
            artifact_payload={
                "lineage": {"params": {"normalized": {"word": "pneumonoultramicroscopicsilicovolcanoconiosis"}}}
            },
            target_word="pneumonoultramicroscopicsilicovolcanoconiosis",
        )
        result = asyncio_run(validator.validate("ref", ctx))
        assert result.verdict == "fail"
        assert "词表外" in result.evidence["reason"]

    def test_fail_on_misspelled_word(self, validator) -> None:
        """拼写错误词（appel）→ fail."""
        ctx = GateContext(
            artifact_type="item",
            pack_id="subject-english",
            artifact_payload={"lineage": {"params": {"normalized": {"word": "appel"}}}},
            target_word="appel",
        )
        result = asyncio_run(validator.validate("ref", ctx))
        assert result.verdict == "fail"

    def test_review_when_no_target(self, validator) -> None:
        ctx = GateContext(
            artifact_type="item",
            pack_id="subject-english",
            artifact_payload={"lineage": {"params": {"normalized": {}}}},
        )
        result = asyncio_run(validator.validate("ref", ctx))
        assert result.verdict == "review"

    def test_review_when_payload_none(self, validator) -> None:
        ctx = GateContext(
            artifact_type="item",
            pack_id="subject-english",
            artifact_payload=None,
        )
        result = asyncio_run(validator.validate("ref", ctx))
        assert result.verdict == "review"

    def test_word_inferred_from_lineage(self, validator) -> None:
        ctx = GateContext(
            artifact_type="item",
            pack_id="subject-english",
            artifact_payload={"lineage": {"params": {"normalized": {"word": "tiger"}}}},
        )
        result = asyncio_run(validator.validate("ref", ctx))
        assert result.verdict == "pass"

    def test_validator_registered(self) -> None:
        from src.core.gate.validator import list_validators

        register_validator("subject-english", WordInVocabValidator)
        assert "word_in_vocab" in list_validators("subject-english")

    def test_validator_class_attributes(self) -> None:
        assert WordInVocabValidator.validator_id == "word_in_vocab"
        assert WordInVocabValidator.blocking is True
        assert WordInVocabValidator.cost_tier == "cheap"


def asyncio_run(coro):
    """测试内同步驱动协程（避免为纯校验测试引入 async 依赖）."""
    import asyncio

    return asyncio.new_event_loop().run_until_complete(coro)


# ────────────────────────────────────────────────────────────────────
# §4 词表种子
# ────────────────────────────────────────────────────────────────────


class TestVocabSeed:
    """课标词表种子完整性."""

    def test_at_least_200_words(self) -> None:
        words = _VOCAB["words"]
        assert len(words) >= 200, f"词表仅 {len(words)} 词（要求 ≥200）"

    def test_all_level_2(self) -> None:
        assert {w["level"] for w in _VOCAB["words"]} == {"二级"}

    def test_words_unique(self) -> None:
        words = [w["word"] for w in _VOCAB["words"]]
        assert len(words) == len(set(words))

    def test_entries_have_required_fields(self) -> None:
        for w in _VOCAB["words"]:
            assert w["word"] and w["meaning"] and w["pos"]
            assert w["gradeband"] == "H"

    def test_license_approved(self) -> None:
        """词表 license_id 在来源登记表中 approved（R-Q-18）."""
        reg = SourceRegistry.from_yaml()
        assert reg.is_approved(_VOCAB["license_id"])


# ────────────────────────────────────────────────────────────────────
# 管线纯函数（选词/干扰项/提示/选项洗牌）
# ────────────────────────────────────────────────────────────────────


class TestPipelineHelpers:
    """管线纯函数：确定性 + 干扰项约束."""

    def test_make_hint(self) -> None:
        assert _pipeline.make_hint("apple") == "a _ _ _ _"
        assert _pipeline.make_hint("go") == "g _"

    def test_pick_words_deterministic(self) -> None:
        w1 = _pipeline.pick_words(_VOCAB, 12)
        w2 = _pipeline.pick_words(_VOCAB, 12)
        assert [w["word"] for w in w1] == [w["word"] for w in w2]
        assert len(w1) == 12

    def test_pick_distractors_constraints(self) -> None:
        """三个干扰释义互不相同、≠ 目标释义、且都在词表内."""
        target = _VOCAB["words"][0]
        d1, d2, d3 = _pipeline.pick_distractors(_VOCAB, target, seed=0)
        meanings = {d1["meaning"], d2["meaning"], d3["meaning"]}
        assert len(meanings) == 3
        assert target["meaning"] not in meanings
        vocab_words = {w["word"] for w in _VOCAB["words"]}
        assert {d1["word"], d2["word"], d3["word"]} <= vocab_words

    def test_build_choice_params_answer_letter_correct(self) -> None:
        target = _VOCAB["words"][0]
        d1, d2, d3 = _pipeline.pick_distractors(_VOCAB, target, seed=0)
        params = _pipeline.build_choice_params(target, d1, d2, d3, seed=7)
        letter = params["answer_letter"]
        assert letter in ("A", "B", "C", "D")
        # answer_letter 指向的选项必须是正确释义
        assert params[f"opt_{letter.lower()}"] == target["meaning"]
        # 四个选项 = 正确释义 + 三个干扰释义
        opts = {params["opt_a"], params["opt_b"], params["opt_c"], params["opt_d"]}
        assert opts == {target["meaning"], d1["meaning"], d2["meaning"], d3["meaning"]}

    def test_build_choice_params_deterministic(self) -> None:
        target = _VOCAB["words"][1]
        d1, d2, d3 = _pipeline.pick_distractors(_VOCAB, target, seed=1)
        p1 = _pipeline.build_choice_params(target, d1, d2, d3, seed=42)
        p2 = _pipeline.build_choice_params(target, d1, d2, d3, seed=42)
        assert p1 == p2
