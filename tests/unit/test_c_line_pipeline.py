"""T-W4-016 C 线端到端流水线单元测试（E2E-2 承载卡）.

验收对照：
  #1 run_c_pipeline 返回入库后的题组 id 与门证书 id。
  #2 任一中途校验失败返回失败原因与证据链，不残留脏数据（事务回滚）。
  #3 入库产物含完整谱系：语篇来源 → AI 生成记录 → 教研定稿标记 → 门证书 → 签发人。
  #4 make accept 全绿；E2E-2 承载卡。
  #5 不 import 任何学科包/学段包。

测试策略（与 test_api_readonly / test_report 一致）：
- Happy path：always-pass 桩验证器（聚焦 pipeline 流程，验证器本身在 test_passage_gate 已测）。
- Failure path：always-fail 桩 → pipeline 返回失败 + 证据链，passage/items/group 不入库。
- Real validator path：真实 passage_fact_check + 暴力词语篇 → fail，pipeline 拒绝入库。
"""
from __future__ import annotations

import inspect
from decimal import Decimal
from typing import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.content.c_line_pipeline import (
    CPipelineFailure,
    CPipelineSuccess,
    run_c_pipeline,
)
from src.core.content.passage_generator import GenerationMeta, PassageDraft
from src.core.content.passage_schema import DifficultyTarget, PromptDirection
from src.core.content.testlet_blueprint import ItemSpec
from src.core.gate.policy.loader import load_default_policy
from src.core.gate.validator import (
    GateContext,
    Validator,
    ValidatorResult,
    register_validator,
    reset_registry,
)
from src.core.models.item_group import ItemGroup
from src.core.models.item_version import ItemVersion
from src.core.models.passage import Passage
from src.core.models.material_license import MaterialLicense


# ════════════════════════════════════════════════════════════════════
# 辅助构造
# ════════════════════════════════════════════════════════════════════


def _direction(
    *,
    genre: str = "narrative",
    grade_band: str = "M",
    subject: str = "subject-chinese",
    body: str = "春天来了，花儿开了。小鸟在唱歌。",
) -> PromptDirection:
    """构造合法命题方向（中段记叙文）."""
    return PromptDirection(
        kp_refs=[{"dimension": "kp", "code": "read.main_idea"}],
        genre=genre,
        difficulty_target=DifficultyTarget(min=0.05, max=0.3),
        grade_band=grade_band,
        subject=subject,
        word_count_target=(50, 200),
    )


def _draft(
    body: str = "春天来了，花儿开了。小鸟在唱歌。",
    direction: PromptDirection | None = None,
) -> PassageDraft:
    """构造 PassageDraft（不调 AI 总线，直接构造）."""
    return PassageDraft(
        body=body,
        prompt="体裁：narrative；学段：M",
        generation_meta=GenerationMeta(
            model="test-model",
            prompt_hash="abc123def4567890",
            prompt_version="v1",
            token_in=100,
            token_out=50,
            duration_ms=200.0,
            fallback=False,
            call_id="call_test_001",
            task_level="L2",
        ),
        direction=direction or _direction(),
    )


def _three_specs() -> list[ItemSpec]:
    """E2E-2 要求 3 道子题."""
    return [
        ItemSpec(
            spec_id="q1",
            kp_codes=["read.main_idea"],
            interaction_type="single_choice",
            scoring_method="exact_match",
            stem_hint="本文的主旨是什么？",
        ),
        ItemSpec(
            spec_id="q2",
            kp_codes=["read.detail"],
            interaction_type="single_choice",
            scoring_method="exact_match",
            stem_hint="花儿在什么时候开的？",
        ),
        ItemSpec(
            spec_id="q3",
            kp_codes=["read.inference"],
            interaction_type="short_answer",
            scoring_method="exact_match",
            stem_hint="小鸟的心情是怎样的？",
        ),
    ]


# ── 桩验证器（与 test_api_readonly / test_report 同模式） ─────────────


def _make_pass_validator(vid: str) -> type[Validator]:
    class _Stub(Validator):
        validator_id = vid  # type: ignore[assignment]
        version = "test-stub-0.0.1"
        cost_tier = "cheap"
        blocking = True

        async def validate(self, artifact_ref: str, ctx: GateContext) -> ValidatorResult:  # type: ignore[override]
            return ValidatorResult(
                validator_id=vid,
                version=self.version,
                verdict="pass",
                evidence={"note": f"test stub {vid} always pass"},
                confidence=Decimal("1.000"),
                cost_ms=0,
                cost_tokens=0,
            )

    _Stub.__name__ = f"_Pass_{vid}"
    return _Stub


def _make_fail_validator(vid: str) -> type[Validator]:
    class _Stub(Validator):
        validator_id = vid  # type: ignore[assignment]
        version = "test-stub-fail-0.0.1"
        cost_tier = "cheap"
        blocking = True

        async def validate(self, artifact_ref: str, ctx: GateContext) -> ValidatorResult:  # type: ignore[override]
            return ValidatorResult(
                validator_id=vid,
                version=self.version,
                verdict="fail",
                evidence={"reason": f"test stub {vid} always fail"},
                confidence=Decimal("1.000"),
                cost_ms=0,
                cost_tokens=0,
            )

    _Stub.__name__ = f"_Fail_{vid}"
    return _Stub


_PASSAGE_CHAIN_VIDS = (
    "schema",
    "license",
    "passage_fact_check",
    "passage_age_appropriate",
    "passage_difficulty_gate",
    "duplicate_placeholder",
)


def _install_all_pass_stubs() -> None:
    """全 pass 桩（happy path）."""
    reset_registry()
    for vid in _PASSAGE_CHAIN_VIDS:
        register_validator("platform", _make_pass_validator(vid))


def _install_fact_check_fail_stubs() -> None:
    """fact_check fail + 其余 pass（failure path）."""
    reset_registry()
    for vid in _PASSAGE_CHAIN_VIDS:
        if vid == "passage_fact_check":
            register_validator("platform", _make_fail_validator(vid))
        else:
            register_validator("platform", _make_pass_validator(vid))


def _install_real_passage_validators() -> None:
    """真实语篇验证器 + 通用桩（schema/license/duplicate 用 pass 桩聚焦语篇门）."""
    reset_registry()
    # 通用桩
    for vid in ("schema", "license", "duplicate_placeholder"):
        register_validator("platform", _make_pass_validator(vid))
    # 真实语篇验证器
    from src.core.gate.validators import (
        PassageAgeAppropriateValidator,
        PassageDifficultyGateValidator,
        PassageFactCheckValidator,
    )
    register_validator("platform", PassageFactCheckValidator)
    register_validator("platform", PassageAgeAppropriateValidator)
    register_validator("platform", PassageDifficultyGateValidator)


# ════════════════════════════════════════════════════════════════════
# 验收 #1 + #3：Happy path — pipeline 成功 + 完整谱系
# ════════════════════════════════════════════════════════════════════


class TestCPipelineHappyPath:
    """端到端 happy path：草稿 → 分析 → 编排 → 过门 → 入库."""

    @pytest.mark.asyncio
    async def test_pipeline_returns_success(self, async_session: AsyncSession):
        """验收 #1：返回 passage_id + item_group_id + cert_id."""
        _install_all_pass_stubs()
        result = await run_c_pipeline(
            passage_draft=_draft(),
            item_specs=_three_specs(),
            db=async_session,
            issued_by="test-issuer",
        )
        assert isinstance(result, CPipelineSuccess)
        assert result.passage_id
        assert result.item_group_id
        assert result.cert_id

    @pytest.mark.asyncio
    async def test_published_passage_in_db(self, async_session: AsyncSession):
        """入库后 passage 行 status=published + 持 cert_id（D2 门强制）."""
        _install_all_pass_stubs()
        result = await run_c_pipeline(
            passage_draft=_draft(),
            item_specs=_three_specs(),
            db=async_session,
        )
        assert isinstance(result, CPipelineSuccess)
        row = (
            await async_session.execute(
                select(Passage).where(Passage.passage_id == result.passage_id)
            )
        ).one_or_none()
        assert row is not None
        passage = row[0]
        assert passage.status == "published"
        assert passage.gate_certificate_id == result.cert_id
        assert passage.genre == "narrative"
        assert passage.grade_band == "M"

    @pytest.mark.asyncio
    async def test_item_versions_in_db(self, async_session: AsyncSession):
        """3 道子题 item_version 入库（draft 状态）."""
        _install_all_pass_stubs()
        result = await run_c_pipeline(
            passage_draft=_draft(),
            item_specs=_three_specs(),
            db=async_session,
        )
        assert isinstance(result, CPipelineSuccess)
        for iv_id in result.lineage.item_version_ids:
            row = (
                await async_session.execute(
                    select(ItemVersion).where(
                        ItemVersion.item_version_id == iv_id
                    )
                )
            ).one_or_none()
            assert row is not None
            assert row[0].status == "draft"
            # tier 在 lineage JSONB 中（ItemVersion 无 tier 列）
            assert row[0].lineage["tier"] == "C"

    @pytest.mark.asyncio
    async def test_item_group_in_db(self, async_session: AsyncSession):
        """item_group 入库：testlet=true + item_version_ids 顺序正确."""
        _install_all_pass_stubs()
        result = await run_c_pipeline(
            passage_draft=_draft(),
            item_specs=_three_specs(),
            db=async_session,
        )
        assert isinstance(result, CPipelineSuccess)
        row = (
            await async_session.execute(
                select(ItemGroup).where(
                    ItemGroup.item_group_id == result.item_group_id
                )
            )
        ).one_or_none()
        assert row is not None
        group = row[0]
        assert group.testlet is True
        assert group.ordered is True
        assert list(group.item_version_ids) == result.lineage.item_version_ids
        assert len(group.item_version_ids) == 3

    @pytest.mark.asyncio
    async def test_license_registered(self, async_session: AsyncSession):
        """许可登记入库（material_license approved）."""
        _install_all_pass_stubs()
        result = await run_c_pipeline(
            passage_draft=_draft(),
            item_specs=_three_specs(),
            db=async_session,
        )
        assert isinstance(result, CPipelineSuccess)
        passage = (
            await async_session.execute(
                select(Passage).where(Passage.passage_id == result.passage_id)
            )
        ).one()[0]
        assert passage.license_id is not None
        lic = (
            await async_session.execute(
                select(MaterialLicense).where(
                    MaterialLicense.license_id == passage.license_id
                )
            )
        ).one_or_none()
        assert lic is not None
        assert lic[0].decision == "approved"

    @pytest.mark.asyncio
    async def test_teacher_finalized_flag(self, async_session: AsyncSession):
        """教研改写定稿标记：传 finalized_body 时 teacher_finalized=True."""
        _install_all_pass_stubs()
        result = await run_c_pipeline(
            passage_draft=_draft(),
            item_specs=_three_specs(),
            db=async_session,
            finalized_body="教研改写后的语篇正文。春天来了。",
        )
        assert isinstance(result, CPipelineSuccess)
        assert result.lineage.teacher_finalized is True

    @pytest.mark.asyncio
    async def test_teacher_finalized_false_when_no_finalize(
        self, async_session: AsyncSession
    ):
        """未传 finalized_body 时 teacher_finalized=False."""
        _install_all_pass_stubs()
        result = await run_c_pipeline(
            passage_draft=_draft(),
            item_specs=_three_specs(),
            db=async_session,
        )
        assert isinstance(result, CPipelineSuccess)
        assert result.lineage.teacher_finalized is False


# ════════════════════════════════════════════════════════════════════
# 验收 #3：完整谱系
# ════════════════════════════════════════════════════════════════════


class TestCPipelineLineage:
    """入库产物含完整谱系：语篇来源 → AI 生成记录 → 教研定稿 → 门证书 → 签发人."""

    @pytest.mark.asyncio
    async def test_lineage_complete_chain(self, async_session: AsyncSession):
        """谱系链条完整：source → ai_generation → teacher_finalized → cert → issued_by."""
        _install_all_pass_stubs()
        result = await run_c_pipeline(
            passage_draft=_draft(),
            item_specs=_three_specs(),
            db=async_session,
            finalized_body="教研定稿正文。",
            issued_by="teacher-zhang",
        )
        assert isinstance(result, CPipelineSuccess)
        lin = result.lineage

        # 语篇来源
        assert lin.source == "ai_draft"
        # AI 生成记录
        assert lin.ai_generation["model"] == "test-model"
        assert lin.ai_generation["prompt_hash"] == "abc123def4567890"
        assert lin.ai_generation["call_id"] == "call_test_001"
        assert lin.ai_generation["task_level"] == "L2"
        # 教研定稿标记
        assert lin.teacher_finalized is True
        # 门证书
        assert lin.gate_certificate_id == result.cert_id
        assert lin.gate_certificate_id is not None
        # 签发人
        assert lin.issued_by == "teacher-zhang"
        # 子题版本 id 列表
        assert len(lin.item_version_ids) == 3
        assert lin.item_group_id == result.item_group_id

    @pytest.mark.asyncio
    async def test_lineage_item_version_ids_match_group(
        self, async_session: AsyncSession
    ):
        """谱系 item_version_ids 与 item_group.item_version_ids 一致."""
        _install_all_pass_stubs()
        result = await run_c_pipeline(
            passage_draft=_draft(),
            item_specs=_three_specs(),
            db=async_session,
        )
        assert isinstance(result, CPipelineSuccess)
        group = (
            await async_session.execute(
                select(ItemGroup).where(
                    ItemGroup.item_group_id == result.item_group_id
                )
            )
        ).one()[0]
        assert list(group.item_version_ids) == result.lineage.item_version_ids


# ════════════════════════════════════════════════════════════════════
# 验收 #2：中途校验失败 → 失败原因 + 证据链 + 不残留脏数据
# ════════════════════════════════════════════════════════════════════


class TestCPipelineFailureRollback:
    """门失败时：返回失败原因 + 证据链，passage/items/group 不入库."""

    @pytest.mark.asyncio
    async def test_gate_fail_returns_failure(self, async_session: AsyncSession):
        """门 fail → CPipelineFailure + 证据链含验证器结果."""
        _install_fact_check_fail_stubs()
        result = await run_c_pipeline(
            passage_draft=_draft(),
            item_specs=_three_specs(),
            db=async_session,
        )
        assert isinstance(result, CPipelineFailure)
        assert result.reason == "gate_fail"
        assert result.step == "gate"
        assert len(result.evidence_chain) > 0
        # 证据链含 passage_fact_check 的 fail
        fact_check_evidence = [
            e for e in result.evidence_chain
            if e["validator_id"] == "passage_fact_check"
        ]
        assert len(fact_check_evidence) == 1
        assert fact_check_evidence[0]["verdict"] == "fail"

    @pytest.mark.asyncio
    async def test_gate_fail_no_passage_in_db(self, async_session: AsyncSession):
        """门 fail → passage 不入库（不残留脏数据）."""
        _install_fact_check_fail_stubs()
        result = await run_c_pipeline(
            passage_draft=_draft(),
            item_specs=_three_specs(),
            db=async_session,
        )
        assert isinstance(result, CPipelineFailure)
        # 查 passage 表——pipeline 生成的 passage_id 不应存在
        count = (
            await async_session.execute(
                text("SELECT COUNT(*) FROM passage WHERE passage_id LIKE 'pass_%'")
            )
        ).scalar()
        assert count == 0

    @pytest.mark.asyncio
    async def test_gate_fail_no_item_versions(self, async_session: AsyncSession):
        """门 fail → item_versions 不入库."""
        _install_fact_check_fail_stubs()
        result = await run_c_pipeline(
            passage_draft=_draft(),
            item_specs=_three_specs(),
            db=async_session,
        )
        assert isinstance(result, CPipelineFailure)
        # pipeline 生成的 item_versions 的 lineage.pipeline.id='c-line'
        count = (
            await async_session.execute(
                text(
                    "SELECT COUNT(*) FROM item_version"
                    " WHERE lineage->'pipeline'->>'id' = 'c-line'"
                )
            )
        ).scalar()
        assert count == 0

    @pytest.mark.asyncio
    async def test_gate_fail_no_item_group(self, async_session: AsyncSession):
        """门 fail → item_group 不入库."""
        _install_fact_check_fail_stubs()
        result = await run_c_pipeline(
            passage_draft=_draft(),
            item_specs=_three_specs(),
            db=async_session,
        )
        assert isinstance(result, CPipelineFailure)
        count = (
            await async_session.execute(
                text("SELECT COUNT(*) FROM item_group WHERE item_group_id LIKE 'ig_%'")
            )
        ).scalar()
        assert count == 0

    @pytest.mark.asyncio
    async def test_blueprint_error_returns_failure(self, async_session: AsyncSession):
        """蓝图编排错误（子题数 <2）→ CPipelineFailure."""
        _install_all_pass_stubs()
        result = await run_c_pipeline(
            passage_draft=_draft(),
            item_specs=[ItemSpec(  # 只有 1 道子题
                spec_id="q1",
                kp_codes=["read.main_idea"],
                interaction_type="single_choice",
                scoring_method="exact_match",
            )],
            db=async_session,
        )
        assert isinstance(result, CPipelineFailure)
        assert result.reason == "blueprint_error"
        assert result.step == "blueprint"


# ════════════════════════════════════════════════════════════════════
# 真实验证器集成：暴力词语篇 → fact_check fail → pipeline 拒绝
# ════════════════════════════════════════════════════════════════════


class TestCPipelineRealValidatorIntegration:
    """真实 passage_fact_check 验证器集成（E2E-2「语篇未过事实门不得入库」）."""

    @pytest.mark.asyncio
    async def test_violence_passage_rejected(self, async_session: AsyncSession):
        """含暴力词的语篇 → fact_check fail → pipeline 拒绝入库."""
        _install_real_passage_validators()
        bad_draft = _draft(
            body="故事里有杀人和血腥场面。这不是一篇适龄的语篇。",
        )
        result = await run_c_pipeline(
            passage_draft=bad_draft,
            item_specs=_three_specs(),
            db=async_session,
        )
        assert isinstance(result, CPipelineFailure)
        assert result.reason == "gate_fail"
        # 证据链中 fact_check fail
        fact_check = [
            e for e in result.evidence_chain
            if e["validator_id"] == "passage_fact_check"
        ]
        assert len(fact_check) == 1
        assert fact_check[0]["verdict"] == "fail"
        assert "暴力词" in fact_check[0]["evidence"]["reason"]

    @pytest.mark.asyncio
    async def test_violence_passage_no_db_residue(
        self, async_session: AsyncSession
    ):
        """含暴力词语篇被拒后 passage 表无残留."""
        _install_real_passage_validators()
        bad_draft = _draft(body="故事里有杀人和血腥场面。")
        result = await run_c_pipeline(
            passage_draft=bad_draft,
            item_specs=_three_specs(),
            db=async_session,
        )
        assert isinstance(result, CPipelineFailure)
        count = (
            await async_session.execute(
                text("SELECT COUNT(*) FROM passage WHERE passage_id LIKE 'pass_%'")
            )
        ).scalar()
        assert count == 0


# ════════════════════════════════════════════════════════════════════
# 验收 #5：不 import 学科包/学段包
# ════════════════════════════════════════════════════════════════════


class TestNoSubjectPackImport:
    """c_line_pipeline 不 import 学科包/学段包（A5/X6）."""

    def test_no_subject_pack_import(self):
        """pipeline 模块源码不含学科包/学段包 import."""
        from src.core.content import c_line_pipeline

        forbidden = [
            "subject_packs",
            "grade_band_packs",
            "from src.packs",
            "import src.packs",
        ]
        source = inspect.getsource(c_line_pipeline)
        for token in forbidden:
            assert token not in source, (
                f"c_line_pipeline 不得 import {token}"
            )
