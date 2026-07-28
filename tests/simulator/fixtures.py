"""T-W4-043 学生模拟器测试夹具.

提供：
- 模拟器客户端构造器（ASGI 模式，连进程内 FastAPI app；DB 依赖覆写为 mock）
- 样本 student_alias_id / paper_id / item_version_ids
- consumer-driven 契约导出辅助

宪法 A5/X6：夹具不 import 学科包；只通过 openapi-v1 端点交互.
"""
from __future__ import annotations

from typing import Any, AsyncIterator, Optional
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from src.api.deps import get_async_session
from src.api.main import create_app
from tests.simulator.client import DEFAULT_STUDENT_TOKEN, SimulatorClient

# ════════════════════════════════════════════════════════════════════
# 样本数据（mock；模拟器不连真实 DB，端点用 ASGI + 依赖覆写处理）
# ════════════════════════════════════════════════════════════════════

# 样本学生别名 ID（mock；生产由学生身份系统分配）
SAMPLE_STUDENT_ALIAS_ID: UUID = UUID("00000000-0000-0000-0000-000000000001")

# 样本 item_version_ids（mock；e2e 测试在 T-W4-044 接真实数据）
SAMPLE_ITEM_VERSION_IDS: list[str] = ["iv-sim-001", "iv-sim-002"]

# 样本 paper_id（mock）
SAMPLE_PAPER_ID: str = "paper-sim-001"

# 样本 mock token（与 SimulatorClient.DEFAULT_STUDENT_TOKEN 同源）
SAMPLE_STUDENT_TOKEN: str = DEFAULT_STUDENT_TOKEN


# ════════════════════════════════════════════════════════════════════
# ASGI app 构造（DB 依赖覆写为 mock，避免连真实库）
# ════════════════════════════════════════════════════════════════════


def make_simulator_app() -> Any:
    """构造 ASGI app 用于模拟器测试（覆写 DB 依赖为空）.

    为什么覆写 DB 依赖：模拟器测试聚焦「接口契约」（请求 shape / 响应
    schema），不连真实 DB；具体场景的 e2e（T-W4-044/045）用真实 DB fixture.
    """
    app = create_app()

    async def _override_session() -> AsyncIterator[None]:
        yield None

    app.dependency_overrides[get_async_session] = _override_session
    return app


# ════════════════════════════════════════════════════════════════════
# pytest fixtures
# ════════════════════════════════════════════════════════════════════


@pytest.fixture
def simulator_app() -> Any:
    """构造 ASGI app（DB 覆写为 mock）——同步 fixture."""
    return make_simulator_app()


@pytest.fixture
def simulator_client(simulator_app: Any) -> SimulatorClient:
    """ASGI 模式模拟器客户端（连进程内 app，无需起服务器）."""
    return SimulatorClient(asgi_app=simulator_app)


@pytest.fixture
def http_mode_client() -> SimulatorClient:
    """HTTP 模式客户端（base_url=localhost:8000；用于本地手动测试）.

    单测默认不启用（CI 无运行中的服务器）；通过 ``--run-http-sim`` 标记触发.
    """
    return SimulatorClient(base_url="http://localhost:8000")


# ════════════════════════════════════════════════════════════════════
# consumer-driven 契约导出辅助
# ════════════════════════════════════════════════════════════════════


def export_contract_cases(client: SimulatorClient) -> list[dict[str, Any]]:
    """导出客户端调用记录为 consumer-driven 契约用例列表."""
    return client.export_call_log()


def assert_contract_case(
    case: dict[str, Any],
    *,
    expected_method: Optional[str] = None,
    expected_path_pattern: Optional[str] = None,
    expected_status: Optional[int] = None,
) -> None:
    """断言单个契约用例的 method/path/status 符合预期（consumer-driven）."""
    if expected_method is not None:
        assert case["method"] == expected_method, (
            f"method 不符：期望 {expected_method}，实际 {case['method']}"
        )
    if expected_path_pattern is not None:
        assert expected_path_pattern in case["path"], (
            f"path 不含 {expected_path_pattern!r}：实际 {case['path']}"
        )
    if expected_status is not None:
        assert case["expected_status"] == expected_status, (
            f"status 不符：期望 {expected_status}，实际 {case['expected_status']}"
        )


__all__ = [
    "SAMPLE_STUDENT_ALIAS_ID",
    "SAMPLE_ITEM_VERSION_IDS",
    "SAMPLE_PAPER_ID",
    "SAMPLE_STUDENT_TOKEN",
    "make_simulator_app",
    "simulator_app",
    "simulator_client",
    "http_mode_client",
    "export_contract_cases",
    "assert_contract_case",
]
