"""T-W2-032 看拼音写词语全链路单元测试.

覆盖任务卡三条验收：
  §1 模板 lint 通过（T-W2-001 DSL Linter）
  §2 实例化确定性（同输入同 item_version_id，T-W2-004）
  §3 字规范校验拦截库外字（CharInCorpusValidator）

实现策略：
  - 学科包目录 subject-chinese 含连字符，用 importlib 加载验证器模块；
  - 模板/语料库 YAML 直接按文件路径加载；
  - 核心域模块（engine/linter/validator）走正常 import。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

from src.core.gate.validator import (
    GateContext,
    ValidatorResult,
    register_validator,
    reset_registry,
)
from src.core.instantiation.dsl.linter import lint
from src.core.instantiation.engine import instantiate

# ────────────────────────────────────────────────────────────────────
# 路径常量
# ────────────────────────────────────────────────────────────────────
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_PACK_DIR = (
    _PROJECT_ROOT / "src" / "packs" / "subject-chinese"
)
_TEMPLATE_PATH = _PACK_DIR / "templates" / "pinyin_to_word.yaml"
_CORPUS_PATH = _PACK_DIR / "corpora" / "character_word.yaml"
_VALIDATOR_PATH = _PACK_DIR / "validators" / "char_in_corpus.py"


# ────────────────────────────────────────────────────────────────────
# 加载被测模块
# ────────────────────────────────────────────────────────────────────


def _load_char_in_corpus_module():
    """以 importlib 加载 char_in_corpus.py（连字符目录无法用普通 import）.

    模块加载时 register_validator('subject-chinese', CharInCorpusValidator) 自动执行。
    """
    mod_name = "subject_chinese_char_in_corpus_under_test"
    if mod_name in sys.modules:
        # 缓存命中：模块不重执行，但 reset_registry() 可能已清空注册表——
        # register_validator 幂等（覆盖语义），确保注册存在（测试互染修复）
        mod = sys.modules[mod_name]
        register_validator("subject-chinese", mod.CharInCorpusValidator)
        return mod
    spec = importlib.util.spec_from_file_location(mod_name, _VALIDATOR_PATH)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载 {_VALIDATOR_PATH}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


_char_module = _load_char_in_corpus_module()
CharInCorpusValidator = _char_module.CharInCorpusValidator


# ────────────────────────────────────────────────────────────────────
# 加载模板与语料库
# ────────────────────────────────────────────────────────────────────


def _load_template() -> dict[str, Any]:
    """加载母题模板 YAML."""
    with _TEMPLATE_PATH.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def _load_corpus() -> dict[str, Any]:
    """加载字词库 YAML."""
    with _CORPUS_PATH.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


_TEMPLATE = _load_template()
_CORPUS = _load_corpus()


# ────────────────────────────────────────────────────────────────────
# 共享 fixture
# ────────────────────────────────────────────────────────────────────


@pytest.fixture
def template_version() -> dict[str, Any]:
    """返回母题模板版本 dict."""
    return _TEMPLATE


@pytest.fixture
def char_validator() -> CharInCorpusValidator:
    """返回 CharInCorpusValidator 实例."""
    return CharInCorpusValidator()


@pytest.fixture
def in_corpus_word() -> str:
    """返回字库内的一个词（用于正向测试）."""
    # 从字库 words 中取第一个 gradeband=L 的词
    for w in _CORPUS.get("words", []):
        if w.get("gradeband") == "L":
            return w["word"]
    return _CORPUS["words"][0]["word"]


# ────────────────────────────────────────────────────────────────────
# §1 模板 lint 通过
# ────────────────────────────────────────────────────────────────────


class TestTemplateLint:
    """验收 §1：母题模板经 DSL Linter 校验通过."""

    def test_lint_passes(self, template_version: dict) -> None:
        """模板 spec 经 lint 返回 valid=True（六大块齐全 + slot 类型合法）."""
        result = lint(template_version["spec"])
        assert result.valid, f"模板 lint 失败：{[e.model_dump() for e in result.errors]}"

    def test_lint_catches_missing_block(self) -> None:
        """lint 能拦截缺块（反向验证：linter 真的在跑）."""
        bad_spec = {"objective": _TEMPLATE["spec"]["objective"]}
        result = lint(bad_spec)
        assert not result.valid
        codes = [e.code for e in result.errors]
        assert "missing_block" in codes

    def test_template_has_required_fields(self, template_version: dict) -> None:
        """模板顶层含 template_version_id / template_id / dsl_version / spec."""
        assert template_version["template_version_id"]
        assert template_version["template_id"]
        assert template_version["dsl_version"]
        assert isinstance(template_version["spec"], dict)

    def test_template_uses_registered_interaction(self, template_version: dict) -> None:
        """模板配套交互类型 text_blank 已在注册表注册（D4：只能复用注册表）."""
        from src.registry.loader import load_interaction_registry

        reg = load_interaction_registry()
        # 模板本身不含 interaction_id（由调用方注入），这里验证 text_blank 在注册表
        interaction = reg.get_interaction("text_blank")
        assert interaction.status == "active"

    def test_template_uses_registered_scorer(self) -> None:
        """配套评分器 exact_match 已在注册表注册（D4：只能复用注册表）."""
        from src.registry.loader import load_scorer_registry

        reg = load_scorer_registry()
        scorer = reg.get_scorer("exact_match")
        assert scorer.status == "active"


# ────────────────────────────────────────────────────────────────────
# §2 实例化确定性
# ────────────────────────────────────────────────────────────────────


class TestInstantiationDeterminism:
    """验收 §2：同输入必得同 item_version_id（D3 可复现）."""

    def test_same_input_same_id(
        self, template_version: dict, in_corpus_word: str
    ) -> None:
        """两次相同参数实例化必得同一 item_version_id."""
        params = {"word": in_corpus_word, "pinyin": "tōng xué"}
        common_kwargs = dict(
            pack_digest="sha256:pack-subject-chinese-test",
            interaction_id="text_blank",
            scorer_id="exact_match",
            scorer_params={"answer": {"b1": in_corpus_word}},
            locale="zh-CN",
            corpus_digests=["sha256:corpus-test"],
            seed=0,
            signed_at="2026-07-27T00:00:00+00:00",
        )
        r1 = instantiate(template_version, params, **common_kwargs)
        r2 = instantiate(template_version, params, **common_kwargs)
        assert r1.item_version_id == r2.item_version_id

    def test_different_word_different_id(
        self, template_version: dict
    ) -> None:
        """不同 word 参数必得不同 item_version_id."""
        common_kwargs = dict(
            pack_digest="sha256:pack-subject-chinese-test",
            interaction_id="text_blank",
            scorer_id="exact_match",
            locale="zh-CN",
            corpus_digests=[],
            seed=0,
            signed_at="2026-07-27T00:00:00+00:00",
        )
        r1 = instantiate(
            template_version,
            {"word": "同学", "pinyin": "tóng xué"},
            scorer_params={"answer": {"b1": "同学"}},
            **common_kwargs,
        )
        r2 = instantiate(
            template_version,
            {"word": "朋友", "pinyin": "péng yǒu"},
            scorer_params={"answer": {"b1": "朋友"}},
            **common_kwargs,
        )
        assert r1.item_version_id != r2.item_version_id

    def test_instantiation_returns_six_blocks(
        self, template_version: dict, in_corpus_word: str
    ) -> None:
        """实例化产物含六大块（objective/interaction_ref/content/scoring_ref/
        error_bindings/lineage）."""
        result = instantiate(
            template_version,
            {"word": in_corpus_word, "pinyin": "cè shì"},
            pack_digest="sha256:pack-test",
            interaction_id="text_blank",
            scorer_id="exact_match",
            scorer_params={"answer": {"b1": in_corpus_word}},
            signed_at="2026-07-27T00:00:00+00:00",
        )
        d = result.model_dump()
        assert d["objective"]
        assert d["interaction_ref"]["interaction_id"] == "text_blank"
        assert d["content"]["blocks"]
        assert d["scoring_ref"]["scorer_id"] == "exact_match"
        assert isinstance(d["error_bindings"], list)
        assert d["lineage"]["template_version_id"]

    def test_instantiation_content_has_pinyin(
        self, template_version: dict
    ) -> None:
        """实例化产物 content.blocks 含拼音文本（presentation 插值成功）."""
        result = instantiate(
            template_version,
            {"word": "同学", "pinyin": "tóng xué"},
            pack_digest="sha256:pack-test",
            interaction_id="text_blank",
            scorer_id="exact_match",
            scorer_params={"answer": {"b1": "同学"}},
            signed_at="2026-07-27T00:00:00+00:00",
        )
        blocks = result.content["blocks"]
        rendered_texts = [b["rendered"] for b in blocks]
        # 拼音应出现在某个 block 的 rendered 字段
        assert any("tóng xué" == t for t in rendered_texts), (
            f"拼音未出现在 content.blocks：{rendered_texts}"
        )

    def test_lineage_carries_normalized_word(
        self, template_version: dict
    ) -> None:
        """lineage.params.normalized.word 携带目标词（CharInCorpusValidator 依赖此路径）."""
        result = instantiate(
            template_version,
            {"word": "学习", "pinyin": "xué xí"},
            pack_digest="sha256:pack-test",
            interaction_id="text_blank",
            scorer_id="exact_match",
            scorer_params={"answer": {"b1": "学习"}},
            signed_at="2026-07-27T00:00:00+00:00",
        )
        normalized = result.lineage["params"]["normalized"]
        assert normalized["word"] == "学习"


# ────────────────────────────────────────────────────────────────────
# §3 字规范校验拦截库外字
# ────────────────────────────────────────────────────────────────────


class TestCharInCorpusValidator:
    """验收 §3：CharInCorpusValidator 拦截字库外汉字."""

    @pytest.mark.asyncio
    async def test_pass_when_word_in_corpus(
        self, char_validator: CharInCorpusValidator, in_corpus_word: str
    ) -> None:
        """字库内词 → verdict='pass'."""
        ctx = GateContext(
            artifact_type="item",
            pack_id="subject-chinese",
            artifact_payload={
                "lineage": {
                    "params": {"normalized": {"word": in_corpus_word}}
                }
            },
            target_word=in_corpus_word,
        )
        result = await char_validator.validate("test-ref", ctx)
        assert result.verdict == "pass"
        assert result.evidence["target_word"] == in_corpus_word

    @pytest.mark.asyncio
    async def test_fail_when_word_has_out_of_corpus_char(
        self, char_validator: CharInCorpusValidator
    ) -> None:
        """含库外字的词 → verdict='fail'，evidence 列出外字."""
        # 选一个生僻字（不在 500 字库内）：U+9F9D 龝（古同"秋"）
        # 验证它确实不在字库
        corpus_chars = {ch["char"] for ch in _CORPUS["characters"]}
        out_char = "龝"
        assert out_char not in corpus_chars, "测试前提失败：龝 应不在字库内"

        bad_word = f"同{out_char}"  # "同"在库内，"龝"不在
        ctx = GateContext(
            artifact_type="item",
            pack_id="subject-chinese",
            artifact_payload={
                "lineage": {
                    "params": {"normalized": {"word": bad_word}}
                }
            },
            target_word=bad_word,
        )
        result = await char_validator.validate("test-ref", ctx)
        assert result.verdict == "fail"
        assert result.evidence["out_of_corpus_chars"] == [out_char]
        assert result.evidence["target_word"] == bad_word

    @pytest.mark.asyncio
    async def test_review_when_no_target_word(
        self, char_validator: CharInCorpusValidator
    ) -> None:
        """未提供 target_word 且 lineage 无 word → verdict='review'."""
        ctx = GateContext(
            artifact_type="item",
            pack_id="subject-chinese",
            artifact_payload={"lineage": {"params": {"normalized": {}}}},
        )
        result = await char_validator.validate("test-ref", ctx)
        assert result.verdict == "review"
        assert "reason" in result.evidence

    @pytest.mark.asyncio
    async def test_review_when_payload_none(
        self, char_validator: CharInCorpusValidator
    ) -> None:
        """artifact_payload 为 None → verdict='review'."""
        ctx = GateContext(
            artifact_type="item",
            pack_id="subject-chinese",
            artifact_payload=None,
        )
        result = await char_validator.validate("test-ref", ctx)
        assert result.verdict == "review"

    @pytest.mark.asyncio
    async def test_word_inferred_from_lineage(
        self, char_validator: CharInCorpusValidator, in_corpus_word: str
    ) -> None:
        """未传 target_word 时从 lineage.params.normalized.word 推断."""
        ctx = GateContext(
            artifact_type="item",
            pack_id="subject-chinese",
            artifact_payload={
                "lineage": {
                    "params": {"normalized": {"word": in_corpus_word}}
                }
            },
            # 故意不传 target_word，验证从 lineage 推断
        )
        result = await char_validator.validate("test-ref", ctx)
        assert result.verdict == "pass"
        assert result.evidence["target_word"] == in_corpus_word

    @pytest.mark.asyncio
    async def test_non_hanzi_chars_ignored(
        self, char_validator: CharInCorpusValidator, in_corpus_word: str
    ) -> None:
        """非汉字字符（标点/数字/英文）不参与字库校验."""
        # 在词库内词后加标点和数字，应 pass
        mixed_word = f"{in_corpus_word}，123abc"
        ctx = GateContext(
            artifact_type="item",
            pack_id="subject-chinese",
            artifact_payload={
                "lineage": {
                    "params": {"normalized": {"word": mixed_word}}
                }
            },
            target_word=mixed_word,
        )
        result = await char_validator.validate("test-ref", ctx)
        assert result.verdict == "pass"


# ────────────────────────────────────────────────────────────────────
# §4 验证器注册（D4：学科验证器注册到 pack_id='subject-chinese'）
# ────────────────────────────────────────────────────────────────────


class TestValidatorRegistration:
    """验证器注册正确性."""

    def test_validator_registered_in_subject_chinese_pack(self) -> None:
        """char_in_corpus 已注册到 pack_id='subject-chinese'."""
        from src.core.gate.validator import get_validator, list_validators

        # 自包含：显式确保模块加载与注册（其他测试可能已 reset_registry）
        _load_char_in_corpus_module()
        assert "char_in_corpus" in list_validators("subject-chinese")

    def test_validator_class_attributes(self) -> None:
        """验证器类属性符合契约."""
        assert CharInCorpusValidator.validator_id == "char_in_corpus"
        assert CharInCorpusValidator.version == "1.0.0+subject-chinese"
        assert CharInCorpusValidator.blocking is True
        assert CharInCorpusValidator.cost_tier == "cheap"

    def test_validator_is_subclass_of_validator(self) -> None:
        """CharInCorpusValidator 是 Validator 子类."""
        from src.core.gate.validator import Validator

        assert issubclass(CharInCorpusValidator, Validator)
