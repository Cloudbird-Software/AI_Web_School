"""T-W4-045 学生模拟器诊断+复习链路 e2e 测试.

验收标准逐条覆盖：
1. diagnosis_scenario.run() 完成诊断链路，断言诊断卷每知识点≥3 孤立题。
2. review_scenario.run() 完成复习链路：错题入队列 → 到期提醒 → 取到复习题 → 作答。
3. 诊断报告含错误类型归因与置信度；复习队列按固定间隔（1/3/7/21 天）触发。
5. 不 import 任何学科包/学段包.

e2e 链路（E2E-1 承载）：
- DB fixture：发布 6 道诊断题（2 KP × 3 孤立题）+ 同步复习策略种子
- ASGI app：覆写 get_async_session 为测试的 async_session（事务隔离）
- DiagnosisScenario / ReviewScenario（ASGI 模式）→ run()
- 断言：诊断约束满足 / 诊断报告返回 / 复习队列到期触发 / 复习作答完成

宪法 A5/X6：本测试不 import 学科包；仅通过 openapi-v1 端点 + 标准 publish 路径.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, AsyncIterator
from uuid import UUID

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import get_async_session
from src.api.main import create_app
from src.core.content.writer import publish_item_version
from src.core.review.service import sync_review_queue
from tests.simulator.client import SimulatorClient
from tests.simulator.scenarios.diagnosis_scenario import DiagnosisScenario
from tests.simulator.scenarios.review_scenario import ReviewScenario

# 诊断场景基准时刻：作答事件 created_at = datetime.now(UTC)（/responses API
# 不接受 now 注入），故 T0 取模块导入时刻近似实际作答时刻——
# due_at = created_at + 1 天，到期判定用 T0 + 1 天 + 1 小时确保 due_at <= due_now。
# 不能用固定时刻：实际作答时刻随墙钟走，固定 due_now 会在一天内部分时段
# 出现 due_at > due_now 而空返回（flaky）；动态 T0 让 due_now 始终滞后作答
# 足够时长，复习排程的 1 天间隔语义稳定可验。
_DIAG_T0 = datetime.now(timezone.utc)
_DIAG_DUE_NOW = (_DIAG_T0 + timedelta(days=1, hours=1)).isoformat().replace("+00:00", "Z")


# ════════════════════════════════════════════════════════════════════
# 诊断题构造：6 道题，2 KP × 3 孤立题；A 绑定错误类型，B 正确
# ════════════════════════════════════════════════════════════════════


def _diagnosis_version_data(stem: str, *, kp_code: str, run_uid: str) -> dict[str, Any]:
    """单选题版本数据：correct_answer=B，A 绑定错误类型，KP 由参数指定."""
    return {
        "pack_id": "platform",
        "tier": "C",
        "status": "published",
        "objective": {
            "kp_set": [{"dimension": "kp", "code": kp_code}],
            "kp_set_mode": "single",  # 孤立题：单 KP
            "cognitive_level": "apply",
            "gradeband": "M",
            "graph_release": "2026.1",
        },
        "interaction_ref": {"interaction_id": "single_choice", "interaction_params": {}},
        "content": {"blocks": [{"kind": "stem", "template": stem, "rendered": f"{stem} {run_uid}"}]},
        "scoring_ref": {"scorer_id": "exact_match", "scorer_params": {"answer": "B"}},
        "error_bindings": [
            {
                "option_value": "A",
                "label": "诊断常见错误",
                "error_type_id": f"sim.diag.e2e.{kp_code}",
                "collision": False,
                "corpus_ref": None,
            },
        ],
        "lineage": {
            "tier": "C",
            "pipeline": {"id": "sim-diag-e2e", "version": "1.0"},
            "signed_by": "sim-diag-e2e",
            "signed_at": "2026-07-28T00:00:00Z",
        },
    }


async def _publish_diagnosis_item(
    db: AsyncSession, stem: str, *, kp_code: str, run_uid: str
) -> str:
    """发布一道诊断题，返回 item_version_id."""
    result = await publish_item_version(
        item_id=None,
        version_data=_diagnosis_version_data(stem, kp_code=kp_code, run_uid=run_uid),
        gate_certificate_id=f"cert-diag-{run_uid}-{kp_code}",
        db=db,
    )
    return result["item_version_id"]


# ════════════════════════════════════════════════════════════════════
# ASGI app + 客户端 fixture（复用 practice_e2e 模式）
# ════════════════════════════════════════════════════════════════════


@pytest_asyncio.fixture
async def e2e_app(async_session: AsyncSession) -> AsyncIterator[Any]:
    """ASGI app 使用测试的 async_session（事务隔离；e2e 测试用）."""
    app = create_app()

    async def _override_session() -> AsyncIterator[AsyncSession]:
        yield async_session

    app.dependency_overrides[get_async_session] = _override_session
    yield app
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def e2e_client(e2e_app: Any) -> SimulatorClient:
    """ASGI 模式模拟器客户端（连 e2e_app，使用测试事务）."""
    return SimulatorClient(asgi_app=e2e_app)


@pytest_asyncio.fixture
async def e2e_diagnosis_items(async_session: AsyncSession) -> dict[str, Any]:
    """发布 6 道诊断题（2 KP × 3 孤立题），返回题目与 KP 映射.

    返回 dict：
    - item_version_ids: 6 道 item_version_id 列表（kp_a × 3 + kp_b × 3）
    - item_kp_map: item_version_id → kp_code
    - kp_a_ids / kp_b_ids: 按 KP 分组的 id 列表
    """
    # 清理既有事件与复习队列（避免前测残留干扰）
    await async_session.execute(
        text("TRUNCATE TABLE response_event RESTART IDENTITY CASCADE")
    )
    await async_session.execute(
        text("TRUNCATE TABLE review_queue_entry RESTART IDENTITY CASCADE")
    )
    await async_session.commit()

    run_uid = uuid.uuid4().hex[:8]
    kp_a_ids = [
        await _publish_diagnosis_item(async_session, f"诊断A-{i}", kp_code="sim.diag.kp.a", run_uid=run_uid)
        for i in range(1, 4)
    ]
    kp_b_ids = [
        await _publish_diagnosis_item(async_session, f"诊断B-{i}", kp_code="sim.diag.kp.b", run_uid=run_uid)
        for i in range(1, 4)
    ]
    item_version_ids = kp_a_ids + kp_b_ids
    item_kp_map = {**{iv: "sim.diag.kp.a" for iv in kp_a_ids},
                   **{iv: "sim.diag.kp.b" for iv in kp_b_ids}}
    return {
        "item_version_ids": item_version_ids,
        "item_kp_map": item_kp_map,
        "kp_a_ids": kp_a_ids,
        "kp_b_ids": kp_b_ids,
    }


# ════════════════════════════════════════════════════════════════════
# sync_review_queue 回调工厂（review 场景用）
# ════════════════════════════════════════════════════════════════════


def _make_sync_fn(async_session: AsyncSession) -> Any:
    """构造 sync_review_queue 回调闭包（review 场景用）.

    封装 db_session + sync_review_queue 调用，让 ReviewScenario 不直连 DB.
    通过 asyncio.run_coroutine_threadsafe 把协程调度到测试 loop（场景从
    worker thread 调用 sync_fn）.
    """
    loop = asyncio.get_running_loop()

    def _sync(student_alias_id: str) -> int:
        async def _call() -> int:
            return await sync_review_queue(
                async_session, student_alias_id=UUID(student_alias_id)
            )
        future = asyncio.run_coroutine_threadsafe(_call(), loop)
        return future.result()

    return _sync


# ════════════════════════════════════════════════════════════════════
# 验收 #1：diagnosis_scenario.run() 完成诊断链路 + 孤立题约束
# ════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_diagnosis_scenario_completes_full_chain(
    e2e_client: SimulatorClient,
    e2e_diagnosis_items: dict[str, Any],
):
    """DiagnosisScenario.run() 完成 诊断约束→开会话→作答→完成→报告 全链路."""
    student_alias_id = str(uuid.uuid4())
    items = e2e_diagnosis_items["item_version_ids"]
    kp_map = e2e_diagnosis_items["item_kp_map"]

    scenario = DiagnosisScenario(
        client=e2e_client,
        student_alias_id=student_alias_id,
        item_version_ids=items,
        gradeband="M",
        item_kp_map=kp_map,
        answers={iv: {"selected": "A"} for iv in items},  # 全错（产生错误推断）
    )
    result = await asyncio.to_thread(scenario.run)

    assert result["all_ok"] is True, f"步骤失败: {result['steps']}"
    assert "diagnosis_report" in result["state"]
    # 全部 5 步执行成功
    assert len(result["steps"]) == 5
    for step in result["steps"]:
        assert step["ok"] is True, f"步骤 {step['step']} 失败: {step}"


@pytest.mark.asyncio
async def test_diagnosis_scenario_asserts_isolated_items_per_kp(
    e2e_client: SimulatorClient,
    e2e_diagnosis_items: dict[str, Any],
):
    """验收 #1：诊断卷每知识点≥3 孤立题断言."""
    student_alias_id = str(uuid.uuid4())
    items = e2e_diagnosis_items["item_version_ids"]
    kp_map = e2e_diagnosis_items["item_kp_map"]

    scenario = DiagnosisScenario(
        client=e2e_client,
        student_alias_id=student_alias_id,
        item_version_ids=items,
        gradeband="M",
        item_kp_map=kp_map,
        answers={iv: {"selected": "A"} for iv in items},
    )
    result = await asyncio.to_thread(scenario.run)

    assert result["all_ok"] is True
    # 验收 #1：每 KP ≥3 孤立题
    kp_counts = result["state"]["kp_isolated_counts"]
    assert all(c >= 3 for c in kp_counts.values()), f"KP 孤立题数: {kp_counts}"
    # 2 个 KP
    assert len(kp_counts) == 2
    # 每 KP 各 3 题
    assert set(kp_counts.values()) == {3}


@pytest.mark.asyncio
async def test_diagnosis_scenario_rejects_insufficient_isolated_items(
    e2e_client: SimulatorClient,
    e2e_diagnosis_items: dict[str, Any],
):
    """验收 #1：孤立题 <3 时 assert_diagnosis_constraints 步骤失败."""
    student_alias_id = str(uuid.uuid4())
    # 只取 2 道 KP-a 题（< 3）构造违规诊断卷
    kp_a_ids = e2e_diagnosis_items["kp_a_ids"][:2]
    kp_b_ids = e2e_diagnosis_items["kp_b_ids"][:3]
    bad_items = kp_a_ids + kp_b_ids
    bad_kp_map = {
        **{iv: "sim.diag.kp.a" for iv in kp_a_ids},
        **{iv: "sim.diag.kp.b" for iv in kp_b_ids},
    }

    scenario = DiagnosisScenario(
        client=e2e_client,
        student_alias_id=student_alias_id,
        item_version_ids=bad_items,
        gradeband="M",
        item_kp_map=bad_kp_map,
        answers={iv: {"selected": "A"} for iv in bad_items},
    )
    result = await asyncio.to_thread(scenario.run)

    # 第一步断言应失败，后续步骤中止
    assert result["all_ok"] is False
    assert result["steps"][0]["ok"] is False
    assert "sim.diag.kp.a" in result["steps"][0]["error"]
    assert "3" in result["steps"][0]["error"]


# ════════════════════════════════════════════════════════════════════
# 验收 #3：诊断报告含错误类型归因与置信度
# ════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_diagnosis_report_contains_error_attribution(
    e2e_client: SimulatorClient,
    e2e_diagnosis_items: dict[str, Any],
):
    """诊断报告含错误类型归因与置信度（验收 #3）.

    场景：6 题全错（3 题绑 et-a + 3 题绑 et-b）→ 每 KP 错误类型 3 条证据
    → min_evidence=3 默认阈值下达 concluded，含 confidence.
    """
    student_alias_id = str(uuid.uuid4())
    items = e2e_diagnosis_items["item_version_ids"]
    kp_map = e2e_diagnosis_items["item_kp_map"]

    scenario = DiagnosisScenario(
        client=e2e_client,
        student_alias_id=student_alias_id,
        item_version_ids=items,
        gradeband="M",
        item_kp_map=kp_map,
        answers={iv: {"selected": "A"} for iv in items},  # 全错
    )
    result = await asyncio.to_thread(scenario.run)
    assert result["all_ok"] is True

    report = result["state"]["diagnosis_report"]
    assert report["student_alias_id"] == student_alias_id
    assert report["scene"] == "diagnosis"
    assert "items" in report
    # 6 题全错，2 个错误类型各 3 条证据 → 都达阈值 concluded
    concluded = [it for it in report["items"] if it["status"] == "concluded"]
    assert len(concluded) == 2, f"应有 2 个达阈值错误类型，实际 {len(concluded)}"
    for item in concluded:
        assert "error_type_id" in item
        assert "confidence" in item
        assert 0.0 <= item["confidence"] <= 1.0
        assert item["evidence_count"] >= 3


# ════════════════════════════════════════════════════════════════════
# 验收 #2：review_scenario.run() 完成复习链路
# ════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_review_scenario_completes_full_chain(
    e2e_client: SimulatorClient,
    e2e_diagnosis_items: dict[str, Any],
    async_session: AsyncSession,
):
    """ReviewScenario.run() 完成 错题入队→到期提醒→取复习题→作答 全链路."""
    student_alias_id = str(uuid.uuid4())
    # 用 2 道题做练习（故意答错 → 入队）
    practice_items = e2e_diagnosis_items["kp_a_ids"][:2]

    scenario = ReviewScenario(
        client=e2e_client,
        student_alias_id=student_alias_id,
        practice_item_version_ids=practice_items,
        gradeband="M",
        practice_answers={iv: {"selected": "A"} for iv in practice_items},  # 全错
        sync_fn=_make_sync_fn(async_session),
        due_now=_DIAG_DUE_NOW,  # 推进到 +1 天 +1 小时（错题入队后 +1 天到期）
    )
    result = await asyncio.to_thread(scenario.run)

    assert result["all_ok"] is True, f"步骤失败: {result['steps']}"
    # 全部 5 步执行成功
    assert len(result["steps"]) == 5
    for step in result["steps"]:
        assert step["ok"] is True, f"步骤 {step['step']} 失败: {step}"
    # 复习作答完成
    assert "review_feedbacks" in result["state"]
    assert len(result["state"]["review_feedbacks"]) == len(practice_items)


@pytest.mark.asyncio
async def test_review_scenario_wrong_answers_enqueued(
    e2e_client: SimulatorClient,
    e2e_diagnosis_items: dict[str, Any],
    async_session: AsyncSession,
):
    """验收 #2：错题答错后入复习队列（queue_count > 0）."""
    student_alias_id = str(uuid.uuid4())
    practice_items = e2e_diagnosis_items["kp_a_ids"][:2]

    scenario = ReviewScenario(
        client=e2e_client,
        student_alias_id=student_alias_id,
        practice_item_version_ids=practice_items,
        gradeband="M",
        practice_answers={iv: {"selected": "A"} for iv in practice_items},
        sync_fn=_make_sync_fn(async_session),
        due_now=_DIAG_DUE_NOW,
    )
    result = await asyncio.to_thread(scenario.run)
    assert result["all_ok"] is True

    # 错题入队
    wrong_ids = result["state"]["wrong_item_version_ids"]
    assert len(wrong_ids) == 2, f"应有 2 道错题，实际 {len(wrong_ids)}"
    assert set(wrong_ids) == set(practice_items)
    # sync_queue 入队数 = 2
    assert result["state"]["queue_count"] == 2


@pytest.mark.asyncio
async def test_review_scenario_due_reviews_match_wrong_items(
    e2e_client: SimulatorClient,
    e2e_diagnosis_items: dict[str, Any],
    async_session: AsyncSession,
):
    """验收 #2/#3：到期复习题 = 错题集（固定间隔 1 天触发）."""
    student_alias_id = str(uuid.uuid4())
    practice_items = e2e_diagnosis_items["kp_a_ids"][:2]

    scenario = ReviewScenario(
        client=e2e_client,
        student_alias_id=student_alias_id,
        practice_item_version_ids=practice_items,
        gradeband="M",
        practice_answers={iv: {"selected": "A"} for iv in practice_items},
        sync_fn=_make_sync_fn(async_session),
        due_now=_DIAG_DUE_NOW,
    )
    result = await asyncio.to_thread(scenario.run)
    assert result["all_ok"] is True

    due_ids = result["state"]["due_item_version_ids"]
    assert set(due_ids) == set(practice_items), (
        f"到期复习题应 = 错题集，实际 due={due_ids} wrong={practice_items}"
    )


@pytest.mark.asyncio
async def test_review_scenario_no_due_before_interval(
    e2e_client: SimulatorClient,
    e2e_diagnosis_items: dict[str, Any],
    async_session: AsyncSession,
):
    """验收 #3：未到期（+12h < +1d）不返回复习题."""
    student_alias_id = str(uuid.uuid4())
    practice_items = e2e_diagnosis_items["kp_a_ids"][:2]
    # due_now 推进到 +12 小时（未到 1 天间隔）
    not_yet_due = (_DIAG_T0 + timedelta(hours=12)).isoformat().replace("+00:00", "Z")

    scenario = ReviewScenario(
        client=e2e_client,
        student_alias_id=student_alias_id,
        practice_item_version_ids=practice_items,
        gradeband="M",
        practice_answers={iv: {"selected": "A"} for iv in practice_items},
        sync_fn=_make_sync_fn(async_session),
        due_now=not_yet_due,
    )
    result = await asyncio.to_thread(scenario.run)

    # fetch_due_reviews 步骤会因 due 为空 → start_review_session 失败
    assert result["all_ok"] is False
    # 找到失败的步骤
    failed = [s for s in result["steps"] if not s["ok"]]
    assert len(failed) >= 1
    # fetch_due_reviews 成功但返回空；start_review_session 因无到期题失败
    step_names = [s["step"] for s in result["steps"]]
    assert "fetch_due_reviews" in step_names
    assert "start_review_session" in step_names


# ════════════════════════════════════════════════════════════════════
# 验收 #5：不 import 学科包/学段包
# ════════════════════════════════════════════════════════════════════


def test_diagnosis_scenario_module_does_not_import_packs():
    """diagnosis_scenario.py 不 import 学科包/学段包（A5 静态实证）."""
    src = (
        Path(__file__).resolve().parent.parent
        / "simulator"
        / "scenarios"
        / "diagnosis_scenario.py"
    ).read_text(encoding="utf-8")
    forbidden = (
        "packs.",
        "gradeband_low",
        "subject-math",
        "subject-chinese",
        "subject-english",
    )
    for needle in forbidden:
        assert needle not in src, f"diagnosis_scenario.py 含禁用 import: {needle!r}"


def test_review_scenario_module_does_not_import_packs():
    """review_scenario.py 不 import 学科包/学段包（A5 静态实证）."""
    src = (
        Path(__file__).resolve().parent.parent
        / "simulator"
        / "scenarios"
        / "review_scenario.py"
    ).read_text(encoding="utf-8")
    forbidden = (
        "packs.",
        "gradeband_low",
        "subject-math",
        "subject-chinese",
        "subject-english",
    )
    for needle in forbidden:
        assert needle not in src, f"review_scenario.py 含禁用 import: {needle!r}"
