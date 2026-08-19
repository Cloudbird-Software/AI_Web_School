"""T-W0-010 安全硬化单元测试（CodeQL 警报修复的回归防线）.

覆盖三组修复：
1. py/url-redirection：/login 的 next 参数白名单化（_safe_next）.
2. py/cookie-injection：会话 cookie 值由服务端 HMAC 派生，与用户输入解耦.
3. py/stack-trace-exposure：/health 响应不透出异常消息（_public_component）.

宪法 D1：本文件不写 DB（登录端点与纯函数路径均不触数据层）。
宪法 X1：不修改任何既有测试，仅新增断言。
"""
from __future__ import annotations

import re

import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from src.workbench.auth import (
    SESSION_COOKIE_NAME,
    get_workbench_token,
    session_cookie_value,
    verify_session_cookie,
)
from src.workbench.main import _safe_next, create_app
from src.core.monitoring.health_endpoints import _public_component


# ────────────────────────────────────────────────────────────────────
# _safe_next：开放重定向防护
# ────────────────────────────────────────────────────────────────────


def test_safe_next_accepts_site_relative() -> None:
    assert _safe_next("/items") == "/items"
    assert _safe_next("/templates/new?x=1") == "/templates/new?x=1"


def test_safe_next_rejects_external_and_protocol_relative() -> None:
    assert _safe_next("https://evil.example/phish") == "/items"
    assert _safe_next("//evil.example") == "/items"
    assert _safe_next("http://evil.example") == "/items"
    assert _safe_next("relative/path") == "/items"
    assert _safe_next("") == "/items"
    assert _safe_next("\\\\evil.example") == "/items"


# ────────────────────────────────────────────────────────────────────
# 会话 cookie：服务端派生，非用户输入
# ────────────────────────────────────────────────────────────────────


def test_session_cookie_value_derived_not_token() -> None:
    value = session_cookie_value()
    token = get_workbench_token()
    assert value != token, "cookie 值不得等于用户提交的 token"
    assert re.fullmatch(r"[0-9a-f]{64}", value), "HMAC-SHA256 十六进制输出"
    assert verify_session_cookie(value) is True
    assert verify_session_cookie(token) is False, "token 本身不是合法会话值"
    assert verify_session_cookie("garbage") is False


@pytest_asyncio.fixture
async def bare_client() -> AsyncClient:
    """无 DB 覆写的工作台 client（登录端点不触数据层）."""
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


async def test_login_sets_derived_cookie_and_internal_redirect(
    bare_client: AsyncClient,
) -> None:
    token = get_workbench_token()
    resp = await bare_client.post(
        "/login",
        data={"token": token, "next": "https://evil.example/phish"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/items", "外部 next 必须回落 /items"
    set_cookie = resp.headers["set-cookie"]
    assert SESSION_COOKIE_NAME in set_cookie
    # cookie 值是派生 HMAC，不是用户输入的 token（py/cookie-injection 回归）
    cookie_value = bare_client.cookies.get(SESSION_COOKIE_NAME)
    assert cookie_value is not None
    assert cookie_value != token
    assert verify_session_cookie(cookie_value) is True


async def test_login_page_external_next_logged_in_falls_back(
    bare_client: AsyncClient,
) -> None:
    bare_client.cookies.set(SESSION_COOKIE_NAME, session_cookie_value())
    resp = await bare_client.get(
        "/login",
        params={"next": "//evil.example"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/items"


async def test_login_page_next_echo_is_sanitized(bare_client: AsyncClient) -> None:
    """未登录时 next 回显到隐藏域，必须是白名单化后的值."""
    resp = await bare_client.get(
        "/login",
        params={"next": "https://evil.example"},
        follow_redirects=False,
    )
    assert resp.status_code == 200
    assert "evil.example" not in resp.text
    assert 'value="/items"' in resp.text


# ────────────────────────────────────────────────────────────────────
# /health 响应脱敏：异常消息不进 HTTP 响应
# ────────────────────────────────────────────────────────────────────


def test_public_component_strips_reason_and_keeps_error_class() -> None:
    probe = {
        "status": "unhealthy",
        "reason": "connection to server at \"127.0.0.1\", port 5432 failed",
        "error_class": "OSError",
    }
    public = _public_component(probe)
    assert public == {"status": "unhealthy", "error_class": "OSError"}
    assert "reason" not in public, "异常消息不得进入 HTTP 响应"


def test_public_component_ok_and_not_configured() -> None:
    assert _public_component({"status": "ok"}) == {"status": "ok"}
    assert _public_component(
        {"status": "not_configured", "reason": "REDIS_URL 未设置"}
    ) == {"status": "not_configured"}
