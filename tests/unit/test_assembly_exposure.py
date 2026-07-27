"""T-W3-assembly S1 曝光账本 + serving 候选加载 DB 测试.

对照任务卡验收：
1. 曝光账本双轨（paper 队列/学生）记录与查询
2. DB 层兜底：周队列级/学生级 UNIQUE 防重复曝光；append-only 触发器
3. 候选加载：load_candidates 只读 serving 视图（published×学段×学科），
   派生字段（kp/先验/用途许可）正确解析

数据准备：直接 ORM 插入 item/item_version（published 行带门证书 id +
published_at，满足 ck_iv_published_requires_gate_cert）；
事务回滚隔离由 conftest async_session fixture 保证。
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.assembly import (
    CandidateItem,
    candidate_from_serving_row,
    load_candidates,
    queue_exposed_item_version_ids,
    queue_exposed_template_version_ids,
    record_paper_exposures,
    record_student_exposures,
    student_exposed_item_version_ids,
    student_exposed_template_version_ids,
)
from src.core.models import Item, ItemVersion, PaperExposure


# ────────────────────────────────────────────────────────────────────
# 数据准备
# ────────────────────────────────────────────────────────────────────

def _objective(kp_code: str, gradeband: str = "M") -> dict:
    return {
        "kp_set": [{"dimension": "kp", "code": kp_code}],
        "kp_set_mode": "single",
        "cognitive_level": "understand",
        "gradeband": gradeband,
        "graph_release": "rel-test",
    }


async def _insert_published(
    session: AsyncSession,
    *,
    item_id: str,
    item_version_id: str,
    pack_id: str = "subject-math",
    kp_code: str = "math.a",
    gradeband: str = "M",
    template_version_id: str | None = None,
    p_correct_prior: float | None = 0.6,
    allowed_purposes: list[str] | None = None,
) -> None:
    """插入一条 published item_version（含 item 身份行）."""
    params: dict = {}
    if p_correct_prior is not None:
        params["p_correct_prior"] = p_correct_prior
    if allowed_purposes is not None:
        params["allowed_purposes"] = allowed_purposes
    if template_version_id is not None:
        # item.template_version_id 有 FK：需真实母题行（minimal 骨架即可）
        from src.core.models import ItemTemplate, ItemTemplateVersion

        template_id = f"tpl-{template_version_id}"
        session.add(ItemTemplate(template_id=template_id, pack_id=pack_id))
        session.add(
            ItemTemplateVersion(
                template_version_id=template_version_id,
                template_id=template_id,
                dsl_version="1.0.0",
                spec={"schema_version": "1.0.0"},
                status="published",
            )
        )
        # 母题两表存在循环外键，拓扑序不稳定：先 flush 确保 item 的 FK 可见
        await session.flush()
    session.add(
        Item(
            item_id=item_id,
            pack_id=pack_id,
            tier="B",
            template_version_id=template_version_id,
        )
    )
    session.add(
        ItemVersion(
            item_version_id=item_version_id,
            item_id=item_id,
            status="published",
            objective=_objective(kp_code, gradeband),
            interaction_ref={"interaction_id": "single_choice", "interaction_params": {}},
            content={"blocks": [{"kind": "stem", "rendered": "题面"}]},
            scoring_ref={"scorer_id": "exact_match", "scorer_params": {"answer": "A"}},
            error_bindings=[],
            lineage={
                "tier": "B",
                "pipeline": {"id": "test-pipe", "version": "1.0.0"},
                "template_version_id": template_version_id,
                "params": params,
                "signed_by": "tester",
                "signed_at": "2026-07-27T00:00:00Z",
            },
            gate_certificate_id="cert-test",
            published_at=datetime(2026, 7, 27, tzinfo=timezone.utc),
            rendered_snapshot={"html": "<div>题面</div>"},
        )
    )
    await session.flush()


def _candidate(vid: str, tpl: str | None = None) -> CandidateItem:
    return CandidateItem(
        item_version_id=vid,
        item_id=f"item-{vid}",
        template_version_id=tpl,
        kp_codes=["math.a"],
        kp_set_mode="single",
        gradeband="M",
        interaction_id="single_choice",
    )


# ────────────────────────────────────────────────────────────────────
# 双轨记录与查询
# ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_paper_exposure_record_and_query(async_session: AsyncSession) -> None:
    """静态轨：记录 渠道×学科×周队列 曝光，查询返回题目集与母题集."""
    await _insert_published(
        async_session, item_id="i1", item_version_id="iv1",
        template_version_id="tv1",
    )
    await _insert_published(
        async_session, item_id="i2", item_version_id="iv2",
        template_version_id="tv2",
    )
    n = await record_paper_exposures(
        async_session,
        channel="weekly_pdf",
        subject_pack_id="subject-math",
        gradeband="M",
        week_label="2026-W30",
        items=[_candidate("iv1", "tv1"), _candidate("iv2", "tv2")],
    )
    assert n == 2

    items = await queue_exposed_item_version_ids(
        async_session, channel="weekly_pdf",
        subject_pack_id="subject-math", week_label="2026-W30",
    )
    assert items == frozenset({"iv1", "iv2"})
    tpls = await queue_exposed_template_version_ids(
        async_session, channel="weekly_pdf",
        subject_pack_id="subject-math", week_label="2026-W30",
    )
    assert tpls == frozenset({"tv1", "tv2"})

    # 不同周队列互不影响
    other_week = await queue_exposed_item_version_ids(
        async_session, channel="weekly_pdf",
        subject_pack_id="subject-math", week_label="2026-W31",
    )
    assert other_week == frozenset()


@pytest.mark.asyncio
async def test_student_exposure_record_and_query(async_session: AsyncSession) -> None:
    """在线轨：按学生匿名 id 记录与查询；学生间互不影响."""
    await _insert_published(
        async_session, item_id="i1", item_version_id="iv1", template_version_id="tv1",
    )
    n = await record_student_exposures(
        async_session,
        student_alias_id="stu-anon-001",
        purpose="practice",
        items=[_candidate("iv1", "tv1")],
    )
    assert n == 1

    assert await student_exposed_item_version_ids(
        async_session, student_alias_id="stu-anon-001"
    ) == frozenset({"iv1"})
    assert await student_exposed_template_version_ids(
        async_session, student_alias_id="stu-anon-001"
    ) == frozenset({"tv1"})
    assert await student_exposed_item_version_ids(
        async_session, student_alias_id="stu-anon-002"
    ) == frozenset()


# ────────────────────────────────────────────────────────────────────
# DB 层兜底
# ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_queue_unique_constraint_blocks_duplicate(async_session: AsyncSession) -> None:
    """同周队列同渠道重复曝光同题 → UNIQUE 冲突（并发组卷的 DB 兜底）."""
    await _insert_published(async_session, item_id="i1", item_version_id="iv1")
    kwargs = dict(
        channel="weekly_pdf",
        subject_pack_id="subject-math",
        gradeband="M",
        week_label="2026-W30",
        items=[_candidate("iv1")],
    )
    await record_paper_exposures(async_session, **kwargs)
    with pytest.raises(IntegrityError):
        await record_paper_exposures(async_session, **kwargs)


@pytest.mark.asyncio
async def test_student_unique_constraint_blocks_duplicate(async_session: AsyncSession) -> None:
    """同一学生重复曝光同题 → UNIQUE 冲突（跨期不重复的 DB 兜底）."""
    await _insert_published(async_session, item_id="i1", item_version_id="iv1")
    kwargs = dict(
        student_alias_id="stu-anon-001",
        purpose="practice",
        items=[_candidate("iv1")],
    )
    await record_student_exposures(async_session, **kwargs)
    with pytest.raises(IntegrityError):
        await record_student_exposures(async_session, **kwargs)


@pytest.mark.asyncio
async def test_exposure_tables_append_only(async_session: AsyncSession) -> None:
    """曝光账本只增不改：UPDATE 被触发器拒绝（D1 风格）."""
    await _insert_published(async_session, item_id="i1", item_version_id="iv1")
    await record_paper_exposures(
        async_session,
        channel="weekly_pdf",
        subject_pack_id="subject-math",
        gradeband="M",
        week_label="2026-W30",
        items=[_candidate("iv1")],
    )
    row = (await async_session.execute(
        PaperExposure.__table__.select().limit(1)
    )).first()
    assert row is not None
    from sqlalchemy import update
    from sqlalchemy.exc import DBAPIError

    # 触发器 raise_append_only_error 抛 plpgsql RAISE（DBAPIError 包装）
    with pytest.raises(DBAPIError, match="append-only"):
        await async_session.execute(
            update(PaperExposure).values(week_label="2026-W99")
        )


# ────────────────────────────────────────────────────────────────────
# serving 候选加载
# ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_load_candidates_filters_pack_and_gradeband(
    async_session: AsyncSession,
) -> None:
    """load_candidates：只装 published × 学科 × 学段；draft/其他学段不出现."""
    await _insert_published(
        async_session, item_id="i1", item_version_id="iv-math-m",
        kp_code="math.a", gradeband="M", template_version_id="tv1",
        p_correct_prior=0.55, allowed_purposes=["practice", "diagnosis"],
    )
    await _insert_published(
        async_session, item_id="i2", item_version_id="iv-math-h",
        kp_code="math.b", gradeband="H",
    )
    # draft 状态的题（不应出现在 serving 视图）
    async_session.add(Item(item_id="i3", pack_id="subject-math", tier="B"))
    async_session.add(
        ItemVersion(
            item_version_id="iv-draft",
            item_id="i3",
            status="draft",
            objective=_objective("math.a"),
            interaction_ref={"interaction_id": "single_choice", "interaction_params": {}},
            content={"blocks": []},
            scoring_ref={"scorer_id": "exact_match", "scorer_params": {}},
            error_bindings=[],
            lineage={
                "tier": "B",
                "pipeline": {"id": "test-pipe", "version": "1.0.0"},
                "signed_by": "tester",
                "signed_at": "2026-07-27T00:00:00Z",
            },
        )
    )
    await async_session.flush()

    pool = await load_candidates(
        async_session, subject_pack_id="subject-math", gradeband="M"
    )
    ids = {c.item_version_id for c in pool}
    assert "iv-math-m" in ids
    assert "iv-math-h" not in ids, "学段过滤失效"
    assert "iv-draft" not in ids, "draft 进入 serving 池"

    cand = next(c for c in pool if c.item_version_id == "iv-math-m")
    assert cand.kp_codes == ["math.a"]
    assert cand.kp_set_mode == "single"
    assert cand.is_isolated
    assert cand.p_correct_prior == 0.55
    assert cand.allowed_purposes == ["practice", "diagnosis"]
    assert cand.template_version_id == "tv1"
    assert cand.interaction_id == "single_choice"


# ────────────────────────────────────────────────────────────────────
# candidate_from_serving_row 输入校验
# ────────────────────────────────────────────────────────────────────

def test_candidate_row_requires_kp_set() -> None:
    """kp_set 为空 → 明确报错（无法组卷的题不静默通过）."""
    row = {
        "item_version_id": "iv-x",
        "item_id": "i-x",
        "template_version_id": None,
        "objective": {**_objective("math.a"), "kp_set": []},
        "interaction_ref": {"interaction_id": "single_choice"},
        "lineage": {"params": {}},
    }
    with pytest.raises(ValueError, match="kp_set"):
        candidate_from_serving_row(row)


def test_candidate_row_rejects_unknown_purpose() -> None:
    """allowed_purposes 含未知场景 → 明确报错（数据质量问题早发现）."""
    row = {
        "item_version_id": "iv-x",
        "item_id": "i-x",
        "template_version_id": None,
        "objective": _objective("math.a"),
        "interaction_ref": {"interaction_id": "single_choice"},
        "lineage": {"params": {"allowed_purposes": ["practice", "exam"]}},
    }
    with pytest.raises(ValueError, match="未知场景"):
        candidate_from_serving_row(row)
