"""T-W4-044 学生模拟器练习链路 e2e 测试.

验收标准逐条覆盖：
1. practice_scenario.run() 完成完整练习链路，输出 session_id 与关键断言结果。
2. 作答数据一致性：提交的作答可在 response_event 中查到，评分结果与期望一致。
3. 弱项报告：至少返回错误类型归因（允许"证据不足"）。
5. 不 import 任何学科包/学段包.

e2e 链路（E2E-1 承载）：
- DB fixture：发布 3 道 single_choice 题（correct_answer=B，A 绑定错误类型）
- ASGI app：覆写 get_async_session 为测试的 async_session（事务隔离）
- SimulatorClient（ASGI 模式）→ FullPracticeScenario.run()
- 断言：response_event 计数 = 提交数；评分对错符合预期；弱项报告返回

宪法 A5/X6：本测试不 import 学科包；仅通过 openapi-v1 端点 + 标准 publish 路径.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import get_async_session
from src.api.main import create_app
from src.core.content.writer import publish_item_version
from tests.simulator.client import SimulatorClient
from tests.simulator.scenarios.practice_scenario import FullPracticeScenario

# ════════════════════════════════════════════════════════════════════
# 题目构造（single_choice，correct_answer=B，A 绑定错误类型）
# ════════════════════════════════════════════════════════════════════


def _version_data(stem: str, *, run_uid: str) -> dict[str, Any]:
    """单选题版本数据：correct_answer=B，A 绑定错误类型 et-001."""
    return {
        "pack_id": "platform",
        "tier": "C",
        "status": "published",
        "objective": {
            "kp_set": [{"dimension": "kp", "code": "sim.practice.e2e"}],
            "kp_set_mode": "single",
            "cognitive_level": "remember",
            "gradeband": "M",
            "graph_release": "2026.1",
        },
        "interaction_ref": {"interaction_id": "single_choice", "interaction_params": {}},
        "content": {"blocks": [{"kind": "stem", "template": stem, "rendered": f"{stem} {run_uid}"}]},
        "scoring_ref": {"scorer_id": "exact_match", "scorer_params": {"answer": "B"}},
        "error_bindings": [
            {
                "option_value": "A",
                "label": "常见错误",
                "error_type_id": "sim.practice.e2e.err-001",
                "collision": False,
                "corpus_ref": None,
            },
        ],
        "lineage": {
            "tier": "C",
            "pipeline": {"id": "sim-e2e", "version": "1.0"},
            "signed_by": "sim-e2e",
            "signed_at": "2026-07-28T00:00:00Z",
        },
    }


async def _publish_item(db: AsyncSession, stem: str, *, run_uid: str) -> str:
    """发布一道题，返回 item_version_id."""
    result = await publish_item_version(
        item_id=None,
        version_data=_version_data(stem, run_uid=run_uid),
        gate_certificate_id=f"cert-sim-{run_uid}",
        db=db,
    )
    return result["item_version_id"]


# ════════════════════════════════════════════════════════════════════
# ASGI app fixture（覆写 DB 依赖为测试的 async_session）
# ════════════════════════════════════════════════════════════════════


@pytest_asyncio.fixture
async def e2e_app(async_session: AsyncSession) -> AsyncIterator[Any]:
    """ASGI app 使用测试的 async_session（事务隔离；e2e 测试用）.

    为什么覆写 get_async_session：API 默认用 deps.py 的进程级 engine，
    与测试的 async_session 是不同连接/不同事务；覆写让 API 走测试事务，
    保证 setup 写入的已发布题目对 API 可见，且测试结束自动回滚.
    """
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
async def e2e_published_items(async_session: AsyncSession) -> list[str]:
    """发布 3 道题，返回 item_version_id 列表."""
    # 清理既有事件（避免前测残留干扰弱项报告）
    await async_session.execute(
        text("TRUNCATE TABLE response_event RESTART IDENTITY CASCADE")
    )
    await async_session.commit()

    run_uid = uuid.uuid4().hex[:8]
    v1 = await _publish_item(async_session, "练习题一", run_uid=run_uid)
    v2 = await _publish_item(async_session, "练习题二", run_uid=run_uid)
    v3 = await _publish_item(async_session, "练习题三", run_uid=run_uid)
    return [v1, v2, v3]


# ════════════════════════════════════════════════════════════════════
# 验收 #1：practice_scenario.run() 完成完整练习链路
# ════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_practice_scenario_completes_full_chain(
    e2e_client: SimulatorClient,
    e2e_published_items: list[str],
):
    """FullPracticeScenario.run() 完成 领卷→作答→完成→弱项报告 全链路（验收 #1）."""
    student_alias_id = str(uuid.uuid4())
    v1, v2, v3 = e2e_published_items

    scenario = FullPracticeScenario(
        client=e2e_client,
        student_alias_id=student_alias_id,
        item_version_ids=[v1, v2, v3],
        gradeband="M",
        answers={
            v1: {"selected": "B"},  # 正确
            v2: {"selected": "A"},  # 故意答错（A 绑定错误类型）
            v3: {"selected": "B"},  # 正确
        },
    )
    result = await asyncio.to_thread(scenario.run)

    # 验收 #1：完成全链路 + 输出 session_id
    assert result["all_ok"] is True, f"步骤失败: {result['steps']}"
    assert "session_id" in result["state"]
    assert result["state"]["session_id"] is not None
    # 全部 4 步执行成功
    assert len(result["steps"]) == 4
    for step in result["steps"]:
        assert step["ok"] is True, f"步骤 {step['step']} 失败: {step}"


@pytest.mark.asyncio
async def test_practice_scenario_outputs_key_assertions(
    e2e_client: SimulatorClient,
    e2e_published_items: list[str],
):
    """场景输出关键断言结果（feedbacks / done / weakness_report）."""
    student_alias_id = str(uuid.uuid4())
    v1, v2, v3 = e2e_published_items

    scenario = FullPracticeScenario(
        client=e2e_client,
        student_alias_id=student_alias_id,
        item_version_ids=[v1, v2, v3],
        gradeband="M",
        answers={v1: {"selected": "B"}, v2: {"selected": "A"}, v3: {"selected": "B"}},
    )
    result = await asyncio.to_thread(scenario.run)

    assert result["all_ok"] is True
    # 关键状态都在 state 里
    assert "feedbacks" in result["state"]
    assert "done" in result["state"]
    assert "weakness_report" in result["state"]
    # 3 道题 3 个反馈
    assert len(result["state"]["feedbacks"]) == 3


# ════════════════════════════════════════════════════════════════════
# 验收 #2：作答数据一致性（response_event + 评分）
# ════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_responses_recorded_in_response_event(
    e2e_client: SimulatorClient,
    e2e_published_items: list[str],
    async_session: AsyncSession,
):
    """提交的作答可在 response_event 中查到（验收 #2）."""
    student_alias_id = str(uuid.uuid4())
    v1, v2, v3 = e2e_published_items

    scenario = FullPracticeScenario(
        client=e2e_client,
        student_alias_id=student_alias_id,
        item_version_ids=[v1, v2, v3],
        gradeband="M",
        answers={v1: {"selected": "B"}, v2: {"selected": "A"}, v3: {"selected": "B"}},
    )
    await asyncio.to_thread(scenario.run)

    # 查 response_event 表：应有 3 条事件，对应 3 个 item_version_id
    rows = (
        await async_session.execute(
            text(
                "SELECT item_version_id, raw_payload FROM response_event "
                "WHERE student_alias_id = :sid ORDER BY created_at",
            ),
            {"sid": student_alias_id},
        )
    ).all()
    assert len(rows) == 3, f"response_event 应有 3 条，实际 {len(rows)}"
    recorded_ids = {r.item_version_id for r in rows}
    assert recorded_ids == {v1, v2, v3}, "提交的题目未全部入账"


@pytest.mark.asyncio
async def test_scoring_results_match_expected(
    e2e_client: SimulatorClient,
    e2e_published_items: list[str],
):
    """评分结果与期望一致（B=对，A=错）（验收 #2）."""
    student_alias_id = str(uuid.uuid4())
    v1, v2, v3 = e2e_published_items

    scenario = FullPracticeScenario(
        client=e2e_client,
        student_alias_id=student_alias_id,
        item_version_ids=[v1, v2, v3],
        gradeband="M",
        answers={v1: {"selected": "B"}, v2: {"selected": "A"}, v3: {"selected": "B"}},
    )
    result = await asyncio.to_thread(scenario.run)
    assert result["all_ok"] is True

    feedbacks = result["state"]["feedbacks"]
    # 按提交顺序断言对错：v1=对、v2=错、v3=对
    # Feedback 结构含 correct 字段（True/False）
    assert feedbacks[0]["correct"] is True, f"v1 应答对: {feedbacks[0]}"
    assert feedbacks[1]["correct"] is False, f"v2 应答错: {feedbacks[1]}"
    assert feedbacks[2]["correct"] is True, f"v3 应答对: {feedbacks[2]}"


# ════════════════════════════════════════════════════════════════════
# 验收 #3：弱项报告（错误类型归因或证据不足）
# ════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_weakness_report_returns_error_attribution(
    e2e_client: SimulatorClient,
    e2e_published_items: list[str],
):
    """弱项报告至少返回错误类型归因（允许「证据不足」）（验收 #3）.

    场景：1 次答错 → 1 条证据；min_evidence=3 默认阈值下应输出「证据不足」
    （未达阈值不给定论）—— 这是合规的弱项报告输出.
    """
    student_alias_id = str(uuid.uuid4())
    v1, v2, v3 = e2e_published_items

    scenario = FullPracticeScenario(
        client=e2e_client,
        student_alias_id=student_alias_id,
        item_version_ids=[v1, v2, v3],
        gradeband="M",
        answers={v1: {"selected": "B"}, v2: {"selected": "A"}, v3: {"selected": "B"}},
    )
    result = await asyncio.to_thread(scenario.run)
    assert result["all_ok"] is True

    report = result["state"]["weakness_report"]
    # 验收 #3：报告含 items 字段（错误类型归因列表，可能为空或「证据不足」）
    assert "items" in report
    assert isinstance(report["items"], list)
    # 报告含 student_alias_id（回指学生）
    assert "student_alias_id" in report
    # 若有 items，每项含 error_type_id + status（concluded/insufficient_evidence）
    for item in report["items"]:
        assert "error_type_id" in item
        assert "status" in item
        assert item["status"] in ("concluded", "insufficient_evidence")


# ════════════════════════════════════════════════════════════════════
# 验收 #3 补充：consumer-driven 契约用例导出
# ════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_practice_scenario_exports_contract_cases(
    e2e_client: SimulatorClient,
    e2e_published_items: list[str],
):
    """场景全部调用可导出为 consumer-driven 契约用例（验收 #3）."""
    student_alias_id = str(uuid.uuid4())
    v1, v2, v3 = e2e_published_items

    scenario = FullPracticeScenario(
        client=e2e_client,
        student_alias_id=student_alias_id,
        item_version_ids=[v1, v2, v3],
        gradeband="M",
        answers={v1: {"selected": "B"}, v2: {"selected": "A"}, v3: {"selected": "B"}},
    )
    result = await asyncio.to_thread(scenario.run)
    assert result["all_ok"] is True

    cases = result["calls"]
    # 至少含 POST /sessions、GET /next×N、POST /responses×N、GET /reports/weakness
    methods_paths = {(c["method"], c["path"].split("/")[1]) for c in cases}
    # 含 sessions 调用
    assert ("POST", "sessions") in methods_paths
    # 含 reports 调用
    assert ("GET", "reports") in methods_paths
    # 所有调用都有 status_code（成功调用）
    for case in cases:
        assert case["expected_status"] is not None


# ════════════════════════════════════════════════════════════════════
# 验收 #5：不 import 学科包/学段包
# ════════════════════════════════════════════════════════════════════


def test_practice_scenario_module_does_not_import_packs():
    """practice_scenario.py 不 import 学科包/学段包（A5 静态实证）."""
    src = (
        Path(__file__).resolve().parent.parent.parent
        / "tests"
        / "simulator"
        / "scenarios"
        / "practice_scenario.py"
    ).read_text(encoding="utf-8")
    forbidden = (
        "packs.",
        "gradeband_low",
        "subject-math",
        "subject-chinese",
        "subject-english",
    )
    for needle in forbidden:
        assert needle not in src, f"practice_scenario.py 含禁用 import: {needle!r}"
