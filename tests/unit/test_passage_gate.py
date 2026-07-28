"""T-W4-014 语篇校验门单元测试.

验收对照：
  #1 三个验证器分别输出 pass/fail/review + evidence；任一 fail 阻断入库。
  #2 事实核查：覆盖政治敏感词、暴力词、明显常识错误（规则+review）。
  #3 适龄性：内容主题与学段匹配（低段不出现复杂社会议题）。
  #4 make accept 全绿；含"未过门语篇入库被 DB 层拒绝"的断言。
  #5 不 import 学科包/学段包。

测试隔离：DB 测试复用 conftest.async_session 的 savepoint 回滚。
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError

from src.core.gate.validator import GateContext, get_validator, list_validators
from src.core.gate.validators import (
    PassageAgeAppropriateValidator,
    PassageDifficultyGateValidator,
    PassageFactCheckValidator,
)
from src.core.models.passage import Passage


# ── 辅助构造 ─────────────────────────────────────────────────────────


def _ctx(payload: dict, artifact_type: str = "passage") -> GateContext:
    """构造 GateContext（platform pack + passage 产物）."""
    return GateContext(
        artifact_type=artifact_type,
        pack_id="platform",
        artifact_payload=payload,
    )


def _clean_passage_payload(**overrides) -> dict:
    """合法的低段记叙文语篇 payload（供验证器消费）."""
    base = {
        "body": "春天来了，花儿开了。小鸟在唱歌。",
        "grade_band": "L",
        "genre": "narrative",
        "difficulty_metrics": {
            "avg_sentence_length": 6.0,
            "oov_rate": 0.1,
            "total_chars": 18,
            "total_sentences": 3,
            "char_freq": {"春": 1},
        },
        "difficulty_target": {"min": 0.05, "max": 0.3},
        "vocab_baseline": {"春", "天", "来", "了", "花", "儿", "开", "小", "鸟", "在", "唱", "歌"},
    }
    base.update(overrides)
    return base


# ── 验收 #2：事实安全验证器 ──────────────────────────────────────────

class TestPassageFactCheck:
    """PassageFactCheckValidator：政治敏感词/暴力词/常识转人工."""

    @pytest.mark.asyncio
    async def test_political_sensitive_fails(self):
        """命中政治敏感词 → fail."""
        payload = _clean_passage_payload(body="这是一篇关于颠覆的内容。")
        validator = PassageFactCheckValidator()
        result = await validator.validate("passage:p1", _ctx(payload))
        assert result.verdict == "fail"
        assert "政治敏感词" in result.evidence["reason"]
        assert "颠覆" in result.evidence["political_hits"]

    @pytest.mark.asyncio
    async def test_violence_term_fails(self):
        """命中暴力词 → fail."""
        payload = _clean_passage_payload(body="故事里有杀人和血腥场面。")
        validator = PassageFactCheckValidator()
        result = await validator.validate("passage:p2", _ctx(payload))
        assert result.verdict == "fail"
        assert "暴力词" in result.evidence["reason"]
        assert "杀人" in result.evidence["violence_hits"]
        assert "血腥" in result.evidence["violence_hits"]

    @pytest.mark.asyncio
    async def test_clean_text_returns_review(self):
        """无敏感词 → review（常识正确性需人工复核）."""
        payload = _clean_passage_payload(body="春天来了，花儿开了。")
        validator = PassageFactCheckValidator()
        result = await validator.validate("passage:p3", _ctx(payload))
        assert result.verdict == "review"
        assert result.evidence["needs_human_review"] is True
        assert result.evidence["political_hits"] == []
        assert result.evidence["violence_hits"] == []

    @pytest.mark.asyncio
    async def test_missing_body_fails(self):
        """缺 body 字段 → fail."""
        payload = {"grade_band": "L", "genre": "narrative"}
        validator = PassageFactCheckValidator()
        result = await validator.validate("passage:p4", _ctx(payload))
        assert result.verdict == "fail"
        assert "body" in result.evidence["reason"]


# ── 验收 #3：适龄性验证器 ────────────────────────────────────────────

class TestPassageAgeAppropriate:
    """PassageAgeAppropriateValidator：学段×体裁/主题/句长."""

    @pytest.mark.asyncio
    async def test_low_band_argumentative_fails(self):
        """低段 + 议论文 → fail（体裁不适配）."""
        payload = _clean_passage_payload(genre="argumentative")
        validator = PassageAgeAppropriateValidator()
        result = await validator.validate("passage:p5", _ctx(payload))
        assert result.verdict == "fail"
        assert "argumentative" in result.evidence["reason"]

    @pytest.mark.asyncio
    async def test_low_band_topic_word_fails(self):
        """低段 + 战争主题词 → fail（主题不适配）."""
        payload = _clean_passage_payload(body="这是一个关于战争的故事。")
        validator = PassageAgeAppropriateValidator()
        result = await validator.validate("passage:p6", _ctx(payload))
        assert result.verdict == "fail"
        assert "战争" in str(result.evidence["topic_hits"])

    @pytest.mark.asyncio
    async def test_sentence_too_long_fails(self):
        """句长显著超学段上限（>1.5x）→ fail."""
        # 低段上限 15，显著超限需 > 22.5
        payload = _clean_passage_payload(
            difficulty_metrics={"avg_sentence_length": 30.0, "oov_rate": 0.1}
        )
        validator = PassageAgeAppropriateValidator()
        result = await validator.validate("passage:p7", _ctx(payload))
        assert result.verdict == "fail"
        assert "显著超" in result.evidence["reason"]

    @pytest.mark.asyncio
    async def test_sentence_slightly_over_returns_review(self):
        """句长轻微超限（上限~1.5x）→ review."""
        # 低段上限 15，轻微超限 15~22.5
        payload = _clean_passage_payload(
            difficulty_metrics={"avg_sentence_length": 18.0, "oov_rate": 0.1}
        )
        validator = PassageAgeAppropriateValidator()
        result = await validator.validate("passage:p8", _ctx(payload))
        assert result.verdict == "review"
        assert "轻微超" in result.evidence["reason"]

    @pytest.mark.asyncio
    async def test_clean_passage_passes(self):
        """合法低段语篇 → pass."""
        payload = _clean_passage_payload()
        validator = PassageAgeAppropriateValidator()
        result = await validator.validate("passage:p9", _ctx(payload))
        assert result.verdict == "pass"
        assert result.evidence["grade_band"] == "L"

    @pytest.mark.asyncio
    async def test_high_band_argumentative_passes(self):
        """高段 + 议论文 → pass（高段可读议论文）."""
        payload = _clean_passage_payload(
            grade_band="H", genre="argumentative",
            difficulty_metrics={"avg_sentence_length": 30.0, "oov_rate": 0.1},
        )
        validator = PassageAgeAppropriateValidator()
        result = await validator.validate("passage:p10", _ctx(payload))
        assert result.verdict == "pass"


# ── 验收 #1：难度一致性验证器 ────────────────────────────────────────

class TestPassageDifficultyGate:
    """PassageDifficultyGateValidator：oov_rate 与目标区间比对."""

    @pytest.mark.asyncio
    async def test_oov_above_target_fails(self):
        """生词率高于目标上限 → fail（语篇过难）."""
        # oov_rate=0.8，目标 [0.05, 0.3] → above
        payload = _clean_passage_payload(
            difficulty_metrics={"avg_sentence_length": 6.0, "oov_rate": 0.8}
        )
        validator = PassageDifficultyGateValidator()
        result = await validator.validate("passage:p11", _ctx(payload))
        assert result.verdict == "fail"
        assert "高于目标上限" in result.evidence["reason"]

    @pytest.mark.asyncio
    async def test_oov_below_target_returns_review(self):
        """生词率低于目标下限 → review（语篇过易）."""
        # oov_rate=0.01，目标 [0.05, 0.3] → below
        payload = _clean_passage_payload(
            difficulty_metrics={"avg_sentence_length": 6.0, "oov_rate": 0.01}
        )
        validator = PassageDifficultyGateValidator()
        result = await validator.validate("passage:p12", _ctx(payload))
        assert result.verdict == "review"
        assert "低于目标下限" in result.evidence["reason"]

    @pytest.mark.asyncio
    async def test_oov_within_target_passes(self):
        """生词率在目标区间内 → pass."""
        # oov_rate=0.15，目标 [0.05, 0.3] → within
        payload = _clean_passage_payload(
            difficulty_metrics={"avg_sentence_length": 6.0, "oov_rate": 0.15}
        )
        validator = PassageDifficultyGateValidator()
        result = await validator.validate("passage:p13", _ctx(payload))
        assert result.verdict == "pass"
        assert result.evidence["oov_rate"] == 0.15

    @pytest.mark.asyncio
    async def test_no_vocab_baseline_returns_review(self):
        """无课标词表 → review（无法计算生词率）."""
        payload = _clean_passage_payload()
        payload.pop("vocab_baseline")
        validator = PassageDifficultyGateValidator()
        result = await validator.validate("passage:p14", _ctx(payload))
        assert result.verdict == "review"
        assert "vocab_baseline" in result.evidence["reason"]

    @pytest.mark.asyncio
    async def test_no_difficulty_target_returns_review(self):
        """无目标区间 → review（无法比对）."""
        payload = _clean_passage_payload()
        payload.pop("difficulty_target")
        validator = PassageDifficultyGateValidator()
        result = await validator.validate("passage:p15", _ctx(payload))
        assert result.verdict == "review"
        assert "difficulty_target" in result.evidence["reason"]

    @pytest.mark.asyncio
    async def test_realtime_analysis_when_no_precomputed_metrics(self):
        """无预计算指标时实时分析（用 body + vocab_baseline）."""
        payload = _clean_passage_payload()
        payload.pop("difficulty_metrics")
        # body 全在 baseline 内 → oov=0，低于目标下限 0.05 → review
        validator = PassageDifficultyGateValidator()
        result = await validator.validate("passage:p16", _ctx(payload))
        assert result.verdict == "review"
        assert "低于目标下限" in result.evidence["reason"]


# ── 验收 #1：验证器注册 ──────────────────────────────────────────────

class TestValidatorRegistration:
    """三个语篇验证器在 platform pack 注册."""

    def test_fact_check_registered(self):
        """passage_fact_check 已注册."""
        assert "passage_fact_check" in list_validators("platform")
        v = get_validator("platform", "passage_fact_check")
        assert isinstance(v, PassageFactCheckValidator)

    def test_age_appropriate_registered(self):
        """passage_age_appropriate 已注册."""
        assert "passage_age_appropriate" in list_validators("platform")
        v = get_validator("platform", "passage_age_appropriate")
        assert isinstance(v, PassageAgeAppropriateValidator)

    def test_difficulty_gate_registered(self):
        """passage_difficulty_gate 已注册."""
        assert "passage_difficulty_gate" in list_validators("platform")
        v = get_validator("platform", "passage_difficulty_gate")
        assert isinstance(v, PassageDifficultyGateValidator)

    def test_all_return_validator_result(self):
        """验证器返回 ValidatorResult（含 verdict/evidence/confidence/validator_id/version）."""
        import asyncio

        from src.core.gate.validator import ValidatorResult

        payload = _clean_passage_payload()
        validator = PassageFactCheckValidator()
        result = asyncio.run(validator.validate("p", _ctx(payload)))
        assert isinstance(result, ValidatorResult)
        assert result.validator_id == "passage_fact_check"
        assert result.version  # 非空版本串
        assert 0 <= float(result.confidence) <= 1


# ── 验收 #4：未过门语篇入库被 DB 层拒绝（D2 门强制）──────────────────

class TestGateEnforcementDB:
    """D2 门强制：published 语篇必须持 gate_certificate_id，DB CHECK 兜底拒绝."""

    @pytest.mark.asyncio
    async def test_published_without_gate_rejected(self, async_session):
        """未过门（无 gate_certificate_id）的 published 语篇被 DB CHECK 拒绝.

        场景：语篇草稿未过校验门，绕过写入服务直写 published 行 →
        ck_passage_published_requires_gate CHECK 约束拒绝（IntegrityError）。
        """
        passage = Passage(
            passage_id="pass_no_gate_001",
            content_hash="sha256:no_gate",
            body="未过门的语篇正文。",
            genre="narrative",
            kp_refs=[{"dimension": "kp", "code": "read.main_idea"}],
            difficulty_metrics={
                "avg_sentence_length": 5.0,
                "oov_rate": 0.1,
                "total_chars": 10,
                "total_sentences": 2,
                "char_freq": {},
            },
            license_id=None,
            grade_band="L",
            subject="subject-chinese",
            status="published",  # published 但无 gate_certificate_id
            gate_certificate_id=None,  # 未过门
        )
        async_session.add(passage)
        with pytest.raises(IntegrityError):
            await async_session.flush()

    @pytest.mark.asyncio
    async def test_published_with_gate_accepted(self, async_session):
        """过门（有 gate_certificate_id）的 published 语篇可入库.

        对照：持合法 gate_certificate_id 的 published 行不被 CHECK 拒绝。
        """
        passage = Passage(
            passage_id="pass_with_gate_001",
            content_hash="sha256:with_gate",
            body="已过门的语篇正文。",
            genre="narrative",
            kp_refs=[{"dimension": "kp", "code": "read.main_idea"}],
            difficulty_metrics={
                "avg_sentence_length": 5.0,
                "oov_rate": 0.1,
                "total_chars": 10,
                "total_sentences": 2,
                "char_freq": {},
            },
            license_id=None,
            grade_band="L",
            subject="subject-chinese",
            status="published",
            gate_certificate_id="gate_cert_001",  # 已过门
        )
        async_session.add(passage)
        await async_session.flush()  # 不抛异常 = DB 接受

    @pytest.mark.asyncio
    async def test_draft_without_gate_accepted(self, async_session):
        """draft 语篇无 gate_certificate_id 可入库（草稿不过门正常）."""
        passage = Passage(
            passage_id="pass_draft_001",
            content_hash="sha256:draft",
            body="草稿语篇正文。",
            genre="narrative",
            kp_refs=[{"dimension": "kp", "code": "read.main_idea"}],
            difficulty_metrics={
                "avg_sentence_length": 5.0,
                "oov_rate": 0.1,
                "total_chars": 10,
                "total_sentences": 2,
                "char_freq": {},
            },
            license_id=None,
            grade_band="L",
            subject="subject-chinese",
            status="draft",  # draft 无 gate 正常
            gate_certificate_id=None,
        )
        async_session.add(passage)
        await async_session.flush()  # 不抛异常


# ── 验收 #5：不 import 学科包/学段包 ─────────────────────────────────

def test_no_subject_pack_import():
    """三个语篇验证器模块不 import 任何学科包/学段包（A5/X6）."""
    import inspect

    from src.core.gate.validators import (
        passage_age_appropriate,
        passage_difficulty_gate,
        passage_fact_check,
    )

    forbidden = ["subject_packs", "grade_band_packs", "from src.packs"]
    for mod in (passage_fact_check, passage_age_appropriate, passage_difficulty_gate):
        source = inspect.getsource(mod)
        for token in forbidden:
            assert token not in source, f"{mod.__name__} 不得 import {token}"
