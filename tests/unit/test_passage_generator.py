"""T-W4-013 AI 语篇起草器单元测试.

验收对照：
  #1 generate_passage(prompt_direction, grade_band) 经 ai_call(L2, ...) 返回
     语篇正文 + 生成元数据（模型/prompt_hash/token）。
  #3 AI 调用经 S2 总线（T-W4-007），调用记录入台账（T-W4-008）。
  #4 make accept TASK=T-W4-013 全绿；单元测试使用 mock AI 响应。
  #5 不 import 学科包/学段包。

测试不消耗真实 API：_MockClient（鸭子类型实现 LLMClient Protocol）+ 隔离台账。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from src.core.ai.bus.models import AIResult
from src.core.ai.ledger.ledger import Ledger, set_default_ledger
from src.core.content.passage_generator import (
    GenerationMeta,
    PassageDraft,
    generate_passage,
)
from src.core.content.passage_schema import (
    DifficultyTarget,
    PromptDirection,
)
from src.core.models.item_version import KpRef


# ── 测试夹具 ─────────────────────────────────────────────────────────

class _MockClient:
    """记录调用参数的 mock LLMClient（实现 LLMClient Protocol）.

    返回固定的语篇正文，便于断言 generate_passage 的返回值；
    fail=True 时抛异常，用于测试异常传播。
    """

    def __init__(self, *, content: str = "春天来了，花儿开了。", fail: bool = False) -> None:
        self.calls: list[dict[str, Any]] = []
        self._content = content
        self._fail = fail

    def complete(
        self, prompt: str, *, model: str, temperature: float, max_tokens: int
    ) -> AIResult:
        if self._fail:
            raise RuntimeError(f"mock failure for {model}")
        self.calls.append(
            {
                "prompt": prompt,
                "model": model,
                "temperature": temperature,
                "max_tokens": max_tokens,
            }
        )
        return AIResult(
            content=self._content,
            model=model,
            token_in=len(prompt),
            token_out=len(self._content),
            duration_ms=1.5,
        )


@pytest.fixture
def isolated_ledger(tmp_path: Path) -> Ledger:
    """每个测试用独立 tmp_path 台账，互不污染."""
    ledger = Ledger(tmp_path / "ai_ledger.jsonl")
    set_default_ledger(ledger)
    yield ledger
    set_default_ledger(None)


def _make_direction(grade_band: str = "L", genre: str = "narrative") -> PromptDirection:
    """构造合法命题方向（语文低段记叙文）."""
    return PromptDirection(
        kp_refs=[KpRef(dimension="kp", code="chinese.read.narrative")],
        genre=genre,
        difficulty_target=DifficultyTarget(min=0.2, max=0.5),
        grade_band=grade_band,
        subject="subject-chinese",
        word_count_target=(100, 200),
    )


# ── 验收 #1：经 ai_call(L2) 返回正文 + 生成元数据 ────────────────────

class TestGeneratePassage:
    """generate_passage 经 L2 总线返回草稿 + 元数据."""

    def test_returns_body_and_meta(self, isolated_ledger: Ledger):
        """返回语篇正文 + 生成元数据（模型/prompt_hash/token）."""
        client = _MockClient(content="春天来了，万物复苏。")
        draft = generate_passage(
            _make_direction(),
            clients={"deepseek": client},
            bypass_pii_filter=True,
        )

        assert isinstance(draft, PassageDraft)
        assert draft.body == "春天来了，万物复苏。"
        # 元数据完整
        meta = draft.generation_meta
        assert isinstance(meta, GenerationMeta)
        assert meta.model == "deepseek-reasoner"  # policy.yaml L2 配置
        assert meta.prompt_hash  # 非空 sha256 前16位
        assert meta.token_in > 0
        assert meta.token_out > 0
        assert meta.duration_ms == 1.5
        assert meta.fallback is False
        assert meta.task_level == "L2"
        assert meta.prompt_version == "v1"

    def test_uses_l2_task_level(self, isolated_ledger: Ledger):
        """经 ai_call L2 档调用（policy.yaml 路由到 deepseek-reasoner）."""
        client = _MockClient()
        generate_passage(
            _make_direction(),
            clients={"deepseek": client},
            bypass_pii_filter=True,
        )
        assert len(client.calls) == 1
        # L2 命中 deepseek-reasoner（policy.yaml 配置）
        assert client.calls[0]["model"] == "deepseek-reasoner"

    def test_prompt_contains_direction_info(self, isolated_ledger: Ledger):
        """渲染后的 prompt 含体裁/学段/学科/知识点."""
        client = _MockClient()
        generate_passage(
            _make_direction(),
            clients={"deepseek": client},
            bypass_pii_filter=True,
        )
        prompt = client.calls[0]["prompt"]
        assert "体裁：narrative" in prompt
        assert "学段：L" in prompt
        assert "学科：subject-chinese" in prompt
        assert "chinese.read.narrative" in prompt
        assert "字数区间：100-200" in prompt

    def test_prompt_hash_matches_prompt(self, isolated_ledger: Ledger):
        """generation_meta.prompt_hash 与渲染后 prompt 的 sha256 前16位一致."""
        import hashlib

        client = _MockClient()
        draft = generate_passage(
            _make_direction(),
            clients={"deepseek": client},
            bypass_pii_filter=True,
        )
        expected = hashlib.sha256(
            client.calls[0]["prompt"].encode("utf-8")
        ).hexdigest()[:16]
        assert draft.generation_meta.prompt_hash == expected

    def test_direction_attached_to_draft(self, isolated_ledger: Ledger):
        """草稿携带命题方向（落 Passage.kp_refs 等字段）."""
        direction = _make_direction()
        draft = generate_passage(
            direction,
            clients={"deepseek": _MockClient()},
            bypass_pii_filter=True,
        )
        assert draft.direction is direction
        assert draft.direction.genre == "narrative"

    def test_grade_band_mismatch_raises(self, isolated_ledger: Ledger):
        """grade_band 参数与 direction.grade_band 不一致时报错."""
        with pytest.raises(ValueError, match="grade_band 不一致"):
            generate_passage(
                _make_direction(grade_band="L"),
                grade_band="M",  # 不一致
                clients={"deepseek": _MockClient()},
                bypass_pii_filter=True,
            )


# ── 验收 #1：命题方向校验失败抛 ValueError ───────────────────────────

class TestDirectionValidation:
    """命题方向校验：知识点/难度/学段."""

    def test_invalid_genre_raises(self, isolated_ledger: Ledger):
        """非法体裁抛 ValueError."""
        direction = PromptDirection(
            kp_refs=[KpRef(dimension="kp", code="chinese.read")],
            genre="invalid_genre",  # 非法
            difficulty_target=DifficultyTarget(min=0.2, max=0.5),
            grade_band="L",
            subject="subject-chinese",
        )
        with pytest.raises(ValueError, match="命题方向校验失败"):
            generate_passage(
                direction,
                clients={"deepseek": _MockClient()},
                bypass_pii_filter=True,
            )

    def test_low_band_argumentative_raises(self, isolated_ledger: Ledger):
        """低段（L）+ 议论文：适龄性校验失败."""
        direction = PromptDirection(
            kp_refs=[KpRef(dimension="kp", code="chinese.argue")],
            genre="argumentative",  # 低段不适配
            difficulty_target=DifficultyTarget(min=0.2, max=0.5),
            grade_band="L",
            subject="subject-chinese",
        )
        with pytest.raises(ValueError, match="低段"):
            generate_passage(
                direction,
                clients={"deepseek": _MockClient()},
                bypass_pii_filter=True,
            )

    def test_empty_kp_refs_raises(self, isolated_ledger: Ledger):
        """kp_refs 为空（Pydantic 层拦截）."""
        with pytest.raises(Exception):  # pydantic ValidationError
            PromptDirection(
                kp_refs=[],  # min_length=1
                genre="narrative",
                difficulty_target=DifficultyTarget(min=0.2, max=0.5),
                grade_band="L",
                subject="subject-chinese",
            )


# ── 验收 #3：调用记录入台账（T-W4-008）──────────────────────────────

class TestLedgerRecording:
    """AI 调用经总线后入台账."""

    def test_ledger_records_call(self, isolated_ledger: Ledger):
        """生成后台账含一条 draft_passage 记录."""
        client = _MockClient()
        draft = generate_passage(
            _make_direction(),
            clients={"deepseek": client},
            artifact_ref="item_revision:test-001",
            bypass_pii_filter=True,
        )

        entries = isolated_ledger.query_all()
        assert len(entries) == 1
        e = entries[0]
        assert e.task_level == "L2"
        assert e.task_name == "draft_passage"
        assert e.task_stage == "draft"
        assert e.model == "deepseek-reasoner"
        assert e.artifact_ref == "item_revision:test-001"
        assert e.token_in > 0
        assert e.token_out > 0
        # call_id 落 GenerationMeta
        assert draft.generation_meta.call_id == e.call_id

    def test_ledger_query_by_artifact(self, isolated_ledger: Ledger):
        """按 artifact_ref 可查到生成调用（T-W4-010 归集依赖）."""
        generate_passage(
            _make_direction(),
            clients={"deepseek": _MockClient()},
            artifact_ref="item_revision:agg-001",
            bypass_pii_filter=True,
        )
        entries = isolated_ledger.query_by_artifact("item_revision:agg-001")
        assert len(entries) == 1
        assert entries[0].task_name == "draft_passage"

    def test_no_ledger_no_crash(self):
        """未注入台账（默认实例为 None 重置后）：不崩溃，call_id=None."""
        set_default_ledger(None)
        try:
            # 默认 get_default_ledger() 会懒建一个默认路径实例，
            # 但此处验证 ledger=None 参数显式传入时的行为：使用默认实例
            # 真正 None 行为由 set_default_ledger(None) + 不传 ledger 触发懒建
            draft = generate_passage(
                _make_direction(),
                clients={"deepseek": _MockClient()},
                bypass_pii_filter=True,
            )
            # 默认实例懒建后仍会记录，call_id 非空
            assert draft.generation_meta.call_id is not None
        finally:
            # 清理默认实例避免污染其他测试
            set_default_ledger(None)

    def test_explicit_ledger_instance(self, tmp_path: Path):
        """显式注入 ledger 实例（不依赖全局默认）."""
        explicit_ledger = Ledger(tmp_path / "explicit.jsonl")
        draft = generate_passage(
            _make_direction(),
            clients={"deepseek": _MockClient()},
            ledger=explicit_ledger,
            artifact_ref="item_revision:explicit-001",
            bypass_pii_filter=True,
        )
        assert draft.generation_meta.call_id is not None
        assert len(explicit_ledger.query_all()) == 1


# ── 验收 #5：不 import 学科包/学段包 ─────────────────────────────────

def test_no_subject_pack_import():
    """passage_generator 模块不 import 任何学科包/学段包（A5/X6）."""
    import inspect

    from src.core.content import passage_generator as mod

    source = inspect.getsource(mod)
    # 学科包/学段包命名约定：subject_*/grade_*/packs/*
    forbidden = ["subject_packs", "grade_band_packs", "from src.packs"]
    for token in forbidden:
        assert token not in source, f"passage_generator 不得 import {token}"
