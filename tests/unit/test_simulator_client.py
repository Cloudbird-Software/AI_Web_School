"""T-W4-043 学生模拟器核心框架单元测试.

验收标准逐条覆盖：
1. SimulatorClient 实现 openapi-v1 全部 C 端接口的同步调用封装，含自动鉴权（mock token）。
2. Scenario 基类支持步骤编排与状态传递；至少提供 BaseScenario 与 PracticeScenario。
3. 每次调用记录请求/响应，可导出为 consumer-driven 契约测试用例。
5. 不 import 任何学科包/学段包.
"""
from __future__ import annotations

from pathlib import Path
from uuid import UUID

import pytest

from src.api.main import create_app
from tests.simulator.client import (
    DEFAULT_STUDENT_TOKEN,
    CallRecord,
    SimulatorClient,
)
from tests.simulator.fixtures import (
    SAMPLE_ITEM_VERSION_IDS,
    SAMPLE_PAPER_ID,
    SAMPLE_STUDENT_ALIAS_ID,
    assert_contract_case,
    export_contract_cases,
    make_simulator_app,
)
from tests.simulator.scenarios import BaseScenario, PracticeScenario

# ════════════════════════════════════════════════════════════════════
# 验收 #1：SimulatorClient 同步封装 + 自动鉴权
# ════════════════════════════════════════════════════════════════════


def test_simulator_client_http_mode_constructable():
    """HTTP 模式客户端可构造（base_url）."""
    client = SimulatorClient(base_url="http://localhost:8000")
    assert client.base_url == "http://localhost:8000"
    assert client.asgi_app is None


def test_simulator_client_asgi_mode_constructable():
    """ASGI 模式客户端可构造（asgi_app）."""
    app = create_app()
    client = SimulatorClient(asgi_app=app)
    assert client.asgi_app is app
    assert client.base_url is None


def test_simulator_client_rejects_both_modes():
    """base_url 与 asgi_app 互斥（二选一）."""
    app = create_app()
    with pytest.raises(ValueError, match="互斥"):
        SimulatorClient(base_url="http://localhost:8000", asgi_app=app)


def test_simulator_client_rejects_neither_mode():
    """必须提供 base_url 或 asgi_app 之一."""
    with pytest.raises(ValueError, match="至少提供其一"):
        SimulatorClient()


def test_simulator_client_default_mock_token():
    """客户端默认带 mock 学生 token（dev-only 鉴权）."""
    client = SimulatorClient(base_url="http://localhost:8000")
    assert client.student_token == DEFAULT_STUDENT_TOKEN
    headers = client._headers()
    assert headers["Authorization"] == f"Bearer {DEFAULT_STUDENT_TOKEN}"


def test_simulator_client_custom_token():
    """客户端支持自定义 token."""
    client = SimulatorClient(base_url="http://x", student_token="custom-token")
    assert client.student_token == "custom-token"
    assert client._headers()["Authorization"] == "Bearer custom-token"


def test_simulator_client_headers_mark_simulator():
    """请求头含 X-Simulator: true 标识（dev 旁路用）."""
    client = SimulatorClient(base_url="http://x")
    assert client._headers()["X-Simulator"] == "true"
    assert client._headers()["Content-Type"] == "application/json"


def test_simulator_client_implements_all_c_side_endpoints():
    """SimulatorClient 实现全部 C 端接口同步封装（验收 #1）.

    覆盖 openapi-v1 的 13 端点：sessions×6 + reports + review + items +
    item_versions + templates + gate_certificates + health.
    """
    client = SimulatorClient(base_url="http://x")
    # sessions 链路（6 个）
    assert callable(client.start_session)
    assert callable(client.get_session)
    assert callable(client.get_next_item)
    assert callable(client.submit_response)
    assert callable(client.resume_session)
    assert callable(client.abandon_session)
    # 报告与复习
    assert callable(client.get_weakness_report)
    assert callable(client.get_review_due)
    # 教研端只读（参考客户端亦覆盖）
    assert callable(client.get_item)
    assert callable(client.get_item_version)
    assert callable(client.get_template)
    assert callable(client.get_gate_certificate)
    # 元信息
    assert callable(client.health)


# ════════════════════════════════════════════════════════════════════
# 验收 #3：调用记录 + consumer-driven 契约导出
# ════════════════════════════════════════════════════════════════════


def test_simulator_client_call_log_starts_empty():
    """客户端初始 call_log 为空."""
    client = SimulatorClient(base_url="http://x")
    assert client.call_log == []


def test_call_record_to_contract_case_shape():
    """CallRecord.to_contract_case 输出 consumer-driven 契约用例 dict 形状."""
    record = CallRecord(
        method="GET",
        path="/health",
        status_code=200,
        response_body={"status": "ok"},
    )
    case = record.to_contract_case()
    assert case["method"] == "GET"
    assert case["path"] == "/health"
    assert case["expected_status"] == 200
    assert case["expected_response_schema_keys"] == ["status"]


def test_call_record_to_contract_case_handles_non_dict_response():
    """响应非 dict（如 list）时 expected_response_schema_keys 为 None."""
    record = CallRecord(
        method="GET",
        path="/review/due/x",
        status_code=200,
        response_body=[{"item": "iv-1"}],
    )
    case = record.to_contract_case()
    assert case["expected_response_schema_keys"] is None


def test_simulator_client_export_call_log():
    """export_call_log 导出全部调用记录为契约用例列表."""
    client = SimulatorClient(base_url="http://x")
    # 手动追加两条记录
    client.call_log.append(
        CallRecord(method="GET", path="/health", status_code=200, response_body={"status": "ok"})
    )
    client.call_log.append(
        CallRecord(method="GET", path="/items/x", status_code=200, response_body={"item_id": "x"})
    )
    cases = export_contract_cases(client)
    assert len(cases) == 2
    assert cases[0]["path"] == "/health"
    assert cases[1]["path"] == "/items/x"


def test_simulator_client_clear_call_log():
    """clear_call_log 清空调用记录."""
    client = SimulatorClient(base_url="http://x")
    client.call_log.append(CallRecord(method="GET", path="/x"))
    assert len(client.call_log) == 1
    client.clear_call_log()
    assert client.call_log == []


def test_assert_contract_case_validates_method_path_status():
    """assert_contract_case 校验 method/path/status."""
    case = {
        "method": "POST",
        "path": "/sessions",
        "expected_status": 201,
    }
    assert_contract_case(case, expected_method="POST", expected_status=201)
    assert_contract_case(case, expected_path_pattern="/sessions")
    with pytest.raises(AssertionError, match="method"):
        assert_contract_case(case, expected_method="GET")
    with pytest.raises(AssertionError, match="path"):
        assert_contract_case(case, expected_path_pattern="/reports")
    with pytest.raises(AssertionError, match="status"):
        assert_contract_case(case, expected_status=200)


# ════════════════════════════════════════════════════════════════════
# 验收 #1 实证：ASGI 模式调用真实端点（健康检查 + openapi）
# ════════════════════════════════════════════════════════════════════


def test_simulator_client_health_endpoint_asgi_mode():
    """ASGI 模式调用 GET /health 成功（验证同步封装 + ASGI transport）."""
    app = make_simulator_app()
    client = SimulatorClient(asgi_app=app)
    result = client.health()
    assert result == {"status": "ok"}
    # 调用记录被追加
    assert len(client.call_log) == 1
    record = client.call_log[0]
    assert record.method == "GET"
    assert record.path == "/health"
    assert record.status_code == 200


def test_simulator_client_records_request_body_for_post():
    """POST 请求的 request_body 被记入 call_log（验收 #3）."""
    app = make_simulator_app()
    client = SimulatorClient(asgi_app=app)
    # POST /sessions 会因 DB mock 返回 None 而失败，但请求被记录
    with pytest.raises(Exception):
        client.start_session(
            student_alias_id=SAMPLE_STUDENT_ALIAS_ID,
            item_version_ids=SAMPLE_ITEM_VERSION_IDS,
        )
    # 找到 POST /sessions 记录
    post_records = [r for r in client.call_log if r.method == "POST" and r.path == "/sessions"]
    assert len(post_records) == 1
    record = post_records[0]
    assert record.request_body is not None
    assert record.request_body["student_alias_id"] == str(SAMPLE_STUDENT_ALIAS_ID)
    assert record.request_body["item_version_ids"] == SAMPLE_ITEM_VERSION_IDS
    assert record.request_body["scene"] == "practice"


def test_simulator_client_ensures_str_id_for_uuid():
    """UUID 类型 student_alias_id 被转为 str（路径/body 参数序列化）."""
    client = SimulatorClient(base_url="http://x")
    sid = UUID("12345678-1234-1234-1234-123456789012")
    assert client._ensure_str_id(sid) == "12345678-1234-1234-1234-123456789012"
    assert client._ensure_str_id("plain-str") == "plain-str"


# ════════════════════════════════════════════════════════════════════
# 验收 #2：Scenario 基类步骤编排 + 状态传递
# ════════════════════════════════════════════════════════════════════


def test_base_scenario_run_executes_steps_in_order():
    """BaseScenario.run() 按 steps 列表顺序执行方法."""
    from dataclasses import dataclass, field

    executed: list[str] = []

    @dataclass
    class _MyScenario(BaseScenario):
        steps: list[str] = field(default_factory=lambda: ["step_a", "step_b"])

        def step_a(self):
            executed.append("a")
            self.state["from_a"] = "hello"
            return "a-result"

        def step_b(self):
            executed.append("b")
            # 验证状态传递：step_b 能读到 step_a 写入的 state
            assert self.state["from_a"] == "hello"
            return "b-result"

    client = SimulatorClient(base_url="http://x")
    scenario = _MyScenario(client=client)
    result = scenario.run()

    assert executed == ["a", "b"]
    assert result["scenario"] == "_MyScenario"
    assert result["all_ok"] is True
    assert len(result["steps"]) == 2
    assert result["steps"][0] == {"step": "step_a", "ok": True, "result": "a-result"}
    assert result["steps"][1] == {"step": "step_b", "ok": True, "result": "b-result"}


def test_base_scenario_aborts_on_step_failure():
    """步骤抛异常时中止后续步骤（保留已完成步骤结果）."""
    from dataclasses import dataclass, field

    @dataclass
    class _FailingScenario(BaseScenario):
        steps: list[str] = field(
            default_factory=lambda: ["ok_step", "fail_step", "never_step"]
        )

        def ok_step(self):
            return "ok"

        def fail_step(self):
            raise ValueError("boom")

        def never_step(self):
            return "should-not-reach"

    client = SimulatorClient(base_url="http://x")
    scenario = _FailingScenario(client=client)
    result = scenario.run()

    assert result["all_ok"] is False
    assert len(result["steps"]) == 2  # 第 3 步未执行
    assert result["steps"][0]["ok"] is True
    assert result["steps"][1]["ok"] is False
    assert "boom" in result["steps"][1]["error"]


def test_base_scenario_summary_includes_call_log_and_state():
    """summary 含调用记录与最终 state（审计/契约导出用）."""
    from dataclasses import dataclass, field

    @dataclass
    class _SimpleScenario(BaseScenario):
        steps: list[str] = field(default_factory=lambda: ["step"])

        def step(self):
            self.state["key"] = "value"
            # 手动追加调用记录（模拟 client 调用）
            self.client.call_log.append(
                CallRecord(method="GET", path="/health", status_code=200, response_body={"status": "ok"})
            )
            return "done"

    client = SimulatorClient(base_url="http://x")
    scenario = _SimpleScenario(client=client)
    result = scenario.run()

    assert len(result["calls"]) == 1
    assert result["calls"][0]["path"] == "/health"
    assert result["state"] == {"key": "value"}


def test_practice_scenario_subclass_of_base():
    """PracticeScenario 是 BaseScenario 子类（验收 #2）."""
    client = SimulatorClient(base_url="http://x")
    scenario = PracticeScenario(client=client)
    assert isinstance(scenario, BaseScenario)


def test_practice_scenario_has_three_steps():
    """PracticeScenario 含 start_session / answer_all_items / verify_session_done 三步."""
    client = SimulatorClient(base_url="http://x")
    scenario = PracticeScenario(client=client)
    assert scenario.steps == ["start_session", "answer_all_items", "verify_session_done"]
    assert callable(scenario.start_session)
    assert callable(scenario.answer_all_items)
    assert callable(scenario.verify_session_done)


def test_practice_scenario_state_passes_session_id_to_answer_step():
    """PracticeScenario 步骤间状态传递：session_id 从 start 流向 answer.

    用 mock client 验证编排正确性（不连真实 API）.
    """
    class _MockClient:
        def __init__(self):
            self.call_log = []
            self.start_calls = 0
            self.next_calls = 0
            self.submit_calls = 0

        def start_session(self, **kwargs):
            self.start_calls += 1
            return {"session_id": "sess-1", "total": 2}

        def get_next_item(self, session_id):
            self.next_calls += 1
            # 第 1 次：返回题 1；第 2 次：返回题 2；第 3 次：done
            if self.next_calls == 1:
                return {"item_version_id": "iv-1"}
            if self.next_calls == 2:
                return {"item_version_id": "iv-2"}
            return {"done": True}

        def submit_response(self, session_id, *, item_version_id, response):
            self.submit_calls += 1
            return {"item_version_id": item_version_id, "correct": True}

        def export_call_log(self):
            return self.call_log

    mock_client = _MockClient()
    scenario = PracticeScenario(
        client=mock_client,  # type: ignore[arg-type]
        student_alias_id=str(SAMPLE_STUDENT_ALIAS_ID),
        item_version_ids=SAMPLE_ITEM_VERSION_IDS,
    )
    result = scenario.run()

    assert result["all_ok"] is True
    assert mock_client.start_calls == 1
    assert mock_client.submit_calls == 2  # 两题各提交一次
    # session_id 流转到了 state
    assert scenario.state["session_id"] == "sess-1"
    assert scenario.state["done"] is True


# ════════════════════════════════════════════════════════════════════
# 验收 #5：不 import 学科包/学段包
# ════════════════════════════════════════════════════════════════════


def test_simulator_client_module_does_not_import_packs():
    """client.py 不 import 学科包/学段包（A5 静态实证）."""
    src = (
        Path(__file__).resolve().parents[2]
        / "tests"
        / "simulator"
        / "client.py"
    ).read_text(encoding="utf-8")
    forbidden = (
        "packs.",
        "gradeband_low",
        "subject-math",
        "subject-chinese",
        "subject-english",
    )
    for needle in forbidden:
        assert needle not in src, f"client.py 含禁用 import: {needle!r}"


def test_simulator_scenarios_module_does_not_import_packs():
    """scenarios/__init__.py 不 import 学科包/学段包（A5 静态实证）."""
    src = (
        Path(__file__).resolve().parents[2]
        / "tests"
        / "simulator"
        / "scenarios"
        / "__init__.py"
    ).read_text(encoding="utf-8")
    forbidden = (
        "packs.",
        "gradeband_low",
        "subject-math",
        "subject-chinese",
        "subject-english",
    )
    for needle in forbidden:
        assert needle not in src, f"scenarios/__init__.py 含禁用 import: {needle!r}"


def test_simulator_fixtures_module_does_not_import_packs():
    """fixtures.py 不 import 学科包/学段包（A5 静态实证）."""
    src = (
        Path(__file__).resolve().parents[2]
        / "tests"
        / "simulator"
        / "fixtures.py"
    ).read_text(encoding="utf-8")
    forbidden = (
        "packs.",
        "gradeband_low",
        "subject-math",
        "subject-chinese",
        "subject-english",
    )
    for needle in forbidden:
        assert needle not in src, f"fixtures.py 含禁用 import: {needle!r}"
