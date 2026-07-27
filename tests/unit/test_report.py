"""W3 S5 弱项报告单元测试（聚合纯函数 + DB 服务 + API）.

覆盖（对应 BRIEF S5 验收点）：
- 报告归因正确性：按 error_type 聚合 + 贝叶斯后验数值校验
- 证据不足路径：阈值以下输出 insufficient_evidence，不给定论、不给推荐
- 推荐查表：已发布实例池中绑定同 error_type 的题组 5 题小卷；
  剔除来源题 / 排除未发布(draft) / 排除绑其他错误类型的题
- 场景口径：scene 过滤在取数层生效（D5 分场景不混估）
- API：GET /reports/weakness/{student_alias_id} 200 + 结构校验

发布路径遵守铁律 2：published 版本一律走 publish_item_version + 真实门编排
（always-pass 桩验证器，同 test_api_readonly 模式），不绕过校验门直写。
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import AsyncIterator, Optional
from uuid import uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import get_async_session
from src.api.main import create_app
from src.core.content.writer import publish_item_version
from src.core.events.writer import record_event
from src.core.gate.orchestrator import run_gate
from src.core.gate.policy.loader import load_default_policy
from src.core.gate.validator import (
    GateContext,
    Validator,
    ValidatorResult,
    register_validator,
    reset_registry,
)
from src.core.report.aggregator import (
    InferenceEventView,
    aggregate_inferences,
)
from src.core.report.service import build_weakness_report, recommend_practice

T0 = datetime.now(timezone.utc).replace(microsecond=0)
ET_WEAK = "math.decimal.digits_more_is_larger"
ET_OTHER = "math.et.other"


# ────────────────────────────────────────────────────────────────────
# 纯函数：aggregate_inferences
# ────────────────────────────────────────────────────────────────────


def _view(item: str, inferences: list[dict]) -> InferenceEventView:
    return InferenceEventView(item_version_id=item, error_inferences=tuple(inferences))


def _inf(error_type_id: str, confidence: float) -> dict:
    return {
        "error_type_id": error_type_id,
        "confidence": confidence,
        "rule_version": "1.2.0",
    }


def test_aggregate_counts_and_posterior() -> None:
    """贝叶斯累积：Beta(1,1) 先验 + 置信度加权证据 → 后验均值."""
    evidences = aggregate_inferences(
        [
            _view("iv-1", [_inf("et.a", 0.9)]),
            _view("iv-2", [_inf("et.a", 0.8)]),
            _view("iv-3", [_inf("et.a", 0.7)]),
        ]
    )
    ev = evidences["et.a"]
    assert ev.evidence_count == 3
    assert ev.alpha == pytest.approx(3.4)
    assert ev.beta == pytest.approx(1.6)
    assert ev.posterior == pytest.approx(0.68)
    assert ev.contributing_item_version_ids == {"iv-1", "iv-2", "iv-3"}


def test_aggregate_multiple_error_types_independent() -> None:
    """多错误类型独立累积；空推断事件不产生证据."""
    evidences = aggregate_inferences(
        [
            _view("iv-1", [_inf("et.a", 0.9), _inf("et.b", 0.4)]),
            _view("iv-2", []),
        ]
    )
    assert evidences["et.a"].evidence_count == 1
    assert evidences["et.b"].evidence_count == 1
    assert len(evidences) == 2


def test_aggregate_skips_dirty_inference() -> None:
    """缺 error_type_id 的脏推断跳过而非炸报告."""
    evidences = aggregate_inferences(
        [_view("iv-1", [{"confidence": 0.9}, _inf("et.a", 0.5)])]
    )
    assert evidences["et.a"].evidence_count == 1


def test_aggregate_confidence_out_of_range_rejected() -> None:
    """confidence 越界 [0,1] 显式失败（契约 §4 约束的应用层兜底）."""
    with pytest.raises(ValueError, match="confidence"):
        aggregate_inferences([_view("iv-1", [_inf("et.a", 1.5)])])


# ────────────────────────────────────────────────────────────────────
# DB：发布辅助（always-pass 桩门，同 test_api_readonly 模式）
# ────────────────────────────────────────────────────────────────────


def _make_always_pass_validator(vid: str) -> type[Validator]:
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
                evidence={"note": "test stub always pass"},
                confidence=Decimal("1.000"),
                cost_ms=0,
                cost_tokens=0,
            )

    _Stub.__name__ = f"_AlwaysPass_{vid}"
    return _Stub


def _install_pass_stubs() -> None:
    reset_registry()
    for vid in ("schema", "license", "duplicate_placeholder"):
        register_validator("platform", _make_always_pass_validator(vid))


def _version_data(error_type_id: str, content_tag: str, status: str) -> dict:
    return {
        "pack_id": "subject-math",
        "tier": "C",
        "status": status,
        "objective": {
            "kp_set": [{"dimension": "kp", "code": "math.nal.decimal.compare"}],
            "kp_set_mode": "single",
            "cognitive_level": "apply",
            "gradeband": "M",
            "graph_release": "graph-v1",
        },
        "interaction_ref": {
            "interaction_id": "single_choice",
            "interaction_params": {"option_count": 4},
        },
        "content": {
            "blocks": [
                {"kind": "stem", "template": content_tag, "rendered": content_tag}
            ]
        },
        "scoring_ref": {"scorer_id": "exact_match", "scorer_params": {"answer": "B"}},
        "error_bindings": [
            {
                "option_value": "A",
                "label": content_tag,
                "error_type_id": error_type_id,
                "collision": False,
                "corpus_ref": None,
            }
        ],
        "lineage": {
            "tier": "C",
            "pipeline": {"id": "test-pipeline", "version": "1.0"},
            "signed_by": "test-author",
            "signed_at": "2026-07-27T00:00:00Z",
        },
    }


async def _publish(
    session: AsyncSession, error_type_id: str, content_tag: str
) -> str:
    """走合法发布路径产出一个 published item_version，返回 item_version_id."""
    draft = await publish_item_version(
        item_id=None,
        version_data=_version_data(error_type_id, content_tag, "draft"),
        gate_certificate_id=None,
        db=session,
    )
    policy = load_default_policy()
    ctx = GateContext(
        artifact_type="item",
        pack_id="platform",
        artifact_payload={"item_version_id": draft["item_version_id"]},
    )
    outcome = await run_gate(
        artifact_ref=draft["item_version_id"],
        artifact_type="item",
        pack_id="platform",
        ctx=ctx,
        policy=policy,
        db=session,
        issued_by="test-issuer",
    )
    assert outcome.final_verdict == "pass"
    pub = await publish_item_version(
        item_id=draft["item_id"],
        version_data=_version_data(error_type_id, content_tag + "·发布", "published"),
        gate_certificate_id=outcome.cert_id,
        db=session,
    )
    return pub["item_version_id"]


async def _record_wrong(
    session: AsyncSession,
    student,
    item_version_id: str,
    error_type_id: str,
    confidence: float,
    scene: str = "practice",
) -> None:
    await record_event(
        session,
        event_id=uuid4(),
        student_alias_id=student,
        item_version_id=item_version_id,
        scene=scene,  # type: ignore[arg-type]
        raw_payload={"selected": "A"},
        scoring_trace={
            "scorer_id": "exact_match",
            "scorer_version": "1.0.0+sha256:test",
            "process": {"correct": False},
            "confidence": {"scoring": 1.0},
        },
        error_inferences=[_inf(error_type_id, confidence)],
        created_at=T0,
    )


@pytest_asyncio.fixture
async def report_session(async_session: AsyncSession) -> AsyncIterator[AsyncSession]:
    """清事件账 + 装桩门 + 建发布池（6 题绑 ET_WEAK 含 1 draft，1 题绑 ET_OTHER）."""
    await async_session.execute(
        text("TRUNCATE TABLE response_event RESTART IDENTITY CASCADE")
    )
    await async_session.commit()
    _install_pass_stubs()

    pool = {
        "weak_published": [
            await _publish(async_session, ET_WEAK, f"弱项池题{i}") for i in range(5)
        ],
    }
    # 第 6 题绑 ET_WEAK 但保持 draft（未发布，不得被推荐）
    draft_only = await publish_item_version(
        item_id=None,
        version_data=_version_data(ET_WEAK, "未发布草稿题", "draft"),
        gate_certificate_id=None,
        db=async_session,
    )
    pool["weak_draft"] = [draft_only["item_version_id"]]
    pool["other"] = [await _publish(async_session, ET_OTHER, "其他错误类型题")]
    async_session.info["pool"] = pool
    yield async_session


# ────────────────────────────────────────────────────────────────────
# DB：报告归因 / 证据不足 / 推荐查表 / 场景口径
# ────────────────────────────────────────────────────────────────────


async def test_report_attribution_and_recommendation(
    report_session: AsyncSession,
) -> None:
    """归因正确性 + 达阈值给结论 + 推荐查表（剔除来源题/草稿/他类型）."""
    pool = report_session.info["pool"]
    student = uuid4()
    sources = pool["weak_published"][:3]
    for iv, conf in zip(sources, (0.9, 0.8, 0.7)):
        await _record_wrong(report_session, student, iv, ET_WEAK, conf)

    report = await build_weakness_report(report_session, student_alias_id=student)

    assert len(report.items) == 1
    item = report.items[0]
    assert item.error_type_id == ET_WEAK
    assert item.status == "concluded"
    assert item.evidence_count == 3
    assert item.confidence == pytest.approx(0.68, abs=1e-4)
    # 推荐：5 题池 - 3 题来源 = 2；draft 与他类型题不得出现
    assert len(item.recommended_item_version_ids) == 2
    assert set(item.recommended_item_version_ids) == set(pool["weak_published"][3:])
    assert pool["weak_draft"][0] not in item.recommended_item_version_ids
    assert pool["other"][0] not in item.recommended_item_version_ids


async def test_report_insufficient_evidence(report_session: AsyncSession) -> None:
    """证据不足路径：阈值以下不给定论、不给推荐（§4.7 允许输出证据不足）."""
    pool = report_session.info["pool"]
    student = uuid4()
    for iv, conf in zip(pool["weak_published"][:2], (0.9, 0.9)):
        await _record_wrong(report_session, student, iv, ET_WEAK, conf)

    report = await build_weakness_report(report_session, student_alias_id=student)

    assert len(report.items) == 1
    item = report.items[0]
    assert item.status == "insufficient_evidence"
    assert item.evidence_count == 2
    assert item.recommended_item_version_ids == []
    # 后验仍如实返回（供参考而非结论）
    assert 0.0 < item.confidence < 1.0


async def test_report_scene_filter(report_session: AsyncSession) -> None:
    """D5 分场景口径：scene 过滤在取数层生效，不混估."""
    pool = report_session.info["pool"]
    student = uuid4()
    await _record_wrong(
        report_session, student, pool["weak_published"][0], ET_WEAK, 0.9,
        scene="practice",
    )
    for iv in pool["weak_published"][1:3]:
        await _record_wrong(
            report_session, student, iv, ET_WEAK, 0.9, scene="diagnosis"
        )

    practice = await build_weakness_report(
        report_session, student_alias_id=student, scene="practice"
    )
    assert practice.scene == "practice"
    assert practice.items[0].evidence_count == 1
    assert practice.items[0].status == "insufficient_evidence"

    diagnosis = await build_weakness_report(
        report_session, student_alias_id=student, scene="diagnosis"
    )
    assert diagnosis.items[0].evidence_count == 2

    all_scenes = await build_weakness_report(
        report_session, student_alias_id=student
    )
    assert all_scenes.scene is None
    assert all_scenes.items[0].evidence_count == 3
    assert all_scenes.items[0].status == "concluded"


async def test_report_empty_for_fresh_student(
    report_session: AsyncSession,
) -> None:
    """无事件学生：空报告（不伪造弱项）."""
    report = await build_weakness_report(
        report_session, student_alias_id=uuid4()
    )
    assert report.items == []


async def test_recommend_practice_caps_at_five(
    report_session: AsyncSession,
) -> None:
    """推荐小卷上限 5 题（池充足时恰 5；不足时如实更少）."""
    pool = report_session.info["pool"]
    rec = await recommend_practice(report_session, error_type_id=ET_WEAK)
    assert rec == sorted(pool["weak_published"])
    assert len(rec) == 5
    # 剔除 3 题来源 → 剩 2，不凑数
    rec2 = await recommend_practice(
        report_session,
        error_type_id=ET_WEAK,
        exclude_item_version_ids=pool["weak_published"][:3],
    )
    assert len(rec2) == 2
    # 无绑定的错误类型 → 空
    assert (
        await recommend_practice(report_session, error_type_id="et.nonexistent")
        == []
    )


# ────────────────────────────────────────────────────────────────────
# API
# ────────────────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def api_client(report_session: AsyncSession) -> AsyncIterator[AsyncClient]:
    app = create_app()

    async def _override_session() -> AsyncIterator[AsyncSession]:
        yield report_session

    app.dependency_overrides[get_async_session] = _override_session
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
    app.dependency_overrides.clear()


async def test_api_weakness_report_200(
    api_client: AsyncClient, report_session: AsyncSession
) -> None:
    """GET /reports/weakness/{student} → 200 + 报告结构."""
    pool = report_session.info["pool"]
    student = uuid4()
    for iv, conf in zip(pool["weak_published"][:3], (0.9, 0.8, 0.7)):
        await _record_wrong(report_session, student, iv, ET_WEAK, conf)

    resp = await api_client.get(f"/reports/weakness/{student}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["student_alias_id"] == str(student)
    assert body["min_evidence"] == 3
    assert len(body["items"]) == 1
    item = body["items"][0]
    assert item["error_type_id"] == ET_WEAK
    assert item["status"] == "concluded"
    assert item["evidence_count"] == 3
    assert len(item["recommended_item_version_ids"]) == 2


async def test_api_weakness_report_scene_query(
    api_client: AsyncClient, report_session: AsyncSession
) -> None:
    """GET /reports/weakness/{student}?scene=practice → 分场景口径."""
    pool = report_session.info["pool"]
    student = uuid4()
    await _record_wrong(
        report_session, student, pool["weak_published"][0], ET_WEAK, 0.9,
        scene="practice",
    )
    await _record_wrong(
        report_session, student, pool["weak_published"][1], ET_WEAK, 0.9,
        scene="diagnosis",
    )

    resp = await api_client.get(
        f"/reports/weakness/{student}", params={"scene": "practice"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["scene"] == "practice"
    assert body["items"][0]["evidence_count"] == 1
    assert body["items"][0]["status"] == "insufficient_evidence"


async def test_api_weakness_report_empty_200(api_client: AsyncClient) -> None:
    """无事件学生：200 + 空 items."""
    resp = await api_client.get(f"/reports/weakness/{uuid4()}")
    assert resp.status_code == 200
    assert resp.json()["items"] == []
