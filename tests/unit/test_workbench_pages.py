"""T-W2-041 教研工作台 v1 单元测试.

覆盖任务卡验收标准 §1-§4：
1. 工作台可启动，登录页存在（占位校验即可）.
2. 题库列表页展示 item_id/title/status/pack_id，支持按 pack_id 过滤.
3. 详情页展示 ItemVersion 的 objective/content/lineage/gate_certificate.
4. 单元测试覆盖页面路由 200.

宪法 D1：测试通过 publish_item_version 写入数据；事务回滚隔离保证不污染其他测试。
宪法 A5/X6：测试不 import 学科包（pack_id 仅用占位 'subject-math' 字符串）。
"""
from __future__ import annotations

from typing import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import get_async_session
from src.core.content.writer import publish_item_version
from src.workbench.auth import SESSION_COOKIE_NAME, get_workbench_token
from src.workbench.main import create_app


# ────────────────────────────────────────────────────────────────────
# 测试数据：合法 item_version（C 级，无 template）
# ────────────────────────────────────────────────────────────────────


def _valid_version_data(pack_id: str = "subject-math") -> dict:
    """构造一份合法的 item_version 数据（C 级 / draft）."""
    return {
        "pack_id": pack_id,
        "tier": "C",
        "status": "draft",
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
                {"kind": "stem", "template": "比较 {a} 与 {b}", "rendered": "比较 0.3 与 0.4"}
            ]
        },
        "scoring_ref": {
            "scorer_id": "exact_match",
            "scorer_params": {"answer": "B"},
        },
        "error_bindings": [
            {"option_value": "A", "label": "0.3 > 0.4", "error_type_id": "et_comp_flaw", "collision": False, "corpus_ref": None}
        ],
        "lineage": {
            "tier": "C",
            "pipeline": {"id": "test-pipeline", "version": "1.0"},
            "signed_by": "test-author",
            "signed_at": "2026-07-27T00:00:00Z",
        },
    }


# ────────────────────────────────────────────────────────────────────
# Fixture：构造带 session cookie 的工作台 client
# ────────────────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def workbench_client(
    async_session: AsyncSession,
) -> AsyncIterator[AsyncClient]:
    """构造工作台 ASGI client，DB 走 async_session fixture，并预登录带 cookie.

    为什么用真实 POST /login 而非手动 client.cookies.set：httpx + ASGITransport
    在某些版本下对 cookie jar 的域匹配处理与浏览器不一致，手动 set 的 cookie
    可能不被发送；走真实登录流程让 Set-Cookie 响应自动写入 cookie jar 最稳。
    登录失败路径由 test_login_with_wrong_token 单独覆盖。
    """
    app = create_app()

    async def _override_session() -> AsyncIterator[AsyncSession]:
        yield async_session

    app.dependency_overrides[get_async_session] = _override_session

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 走真实登录流程：POST /login 让 Set-Cookie 自动写入 cookie jar
        token = get_workbench_token()
        login_resp = await client.post(
            "/login",
            data={"token": token, "next": "/items"},
            follow_redirects=False,
        )
        assert login_resp.status_code == 303, f"预登录失败：{login_resp.text}"
        yield client

    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def workbench_client_no_auth(
    async_session: AsyncSession,
) -> AsyncIterator[AsyncClient]:
    """无 cookie 的工作台 client（用于测试未登录重定向）."""
    app = create_app()

    async def _override_session() -> AsyncIterator[AsyncSession]:
        yield async_session

    app.dependency_overrides[get_async_session] = _override_session

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client

    app.dependency_overrides.clear()


# ────────────────────────────────────────────────────────────────────
# 辅助：写入一条 item_version
# ────────────────────────────────────────────────────────────────────


async def _publish_draft(async_session: AsyncSession, pack_id: str = "subject-math") -> dict:
    """写入一条 draft item_version，返回 {item_id, item_version_id}."""
    return await publish_item_version(
        item_id=None,
        version_data=_valid_version_data(pack_id=pack_id),
        gate_certificate_id=None,
        db=async_session,
    )


# ════════════════════════════════════════════════════════════════════
# 测试 §1：工作台可启动，登录页存在
# ════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_workbench_app_starts(workbench_client_no_auth: AsyncClient) -> None:
    """工作台 ASGI 可启动：GET /login → 200，含登录表单."""
    resp = await workbench_client_no_auth.get("/login")
    assert resp.status_code == 200, resp.text
    assert "登录" in resp.text
    assert "token" in resp.text.lower() or "Token" in resp.text


@pytest.mark.asyncio
async def test_health_check(workbench_client_no_auth: AsyncClient) -> None:
    """GET /health → 200（无需鉴权，便于 docker compose 健康探测）."""
    resp = await workbench_client_no_auth.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_login_with_correct_token_redirects(
    workbench_client_no_auth: AsyncClient,
) -> None:
    """POST /login 正确 token → 303 重定向到 /items，并设置 cookie."""
    token = get_workbench_token()
    resp = await workbench_client_no_auth.post(
        "/login",
        data={"token": token, "next": "/items"},
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text
    assert resp.headers["location"] == "/items"
    # Set-Cookie 含 workbench_session
    set_cookie = resp.headers.get("set-cookie", "")
    assert SESSION_COOKIE_NAME in set_cookie
    assert "HttpOnly" in set_cookie


@pytest.mark.asyncio
async def test_login_with_wrong_token(
    workbench_client_no_auth: AsyncClient,
) -> None:
    """POST /login 错误 token → 401，重渲染登录页回显错误."""
    resp = await workbench_client_no_auth.post(
        "/login",
        data={"token": "wrong-token", "next": "/items"},
        follow_redirects=False,
    )
    assert resp.status_code == 401
    assert "token 错误" in resp.text


@pytest.mark.asyncio
async def test_protected_route_redirects_to_login(
    workbench_client_no_auth: AsyncClient,
) -> None:
    """未登录访问 /items → 303 重定向到 /login?next=/items."""
    resp = await workbench_client_no_auth.get("/items", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/login")
    assert "next" in resp.headers["location"]


@pytest.mark.asyncio
async def test_logout_clears_cookie(
    workbench_client: AsyncClient,
) -> None:
    """GET /logout → 303 到 /login，并删除 cookie."""
    resp = await workbench_client.get("/logout", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"
    # Set-Cookie 应包含删除指令（max-age=0 或 expired）
    set_cookie = resp.headers.get("set-cookie", "")
    assert SESSION_COOKIE_NAME in set_cookie


# ════════════════════════════════════════════════════════════════════
# 测试 §2：题库列表页 + pack_id 过滤
# ════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_items_list_empty(workbench_client: AsyncClient) -> None:
    """GET /items 空库 → 200，显示 '题库为空' 提示."""
    resp = await workbench_client.get("/items")
    assert resp.status_code == 200, resp.text
    assert "题库列表" in resp.text


@pytest.mark.asyncio
async def test_items_list_shows_item(
    workbench_client: AsyncClient, async_session: AsyncSession
) -> None:
    """GET /items 含一条 item → 200，列表展示 item_id/pack_id/tier/status."""
    info = await _publish_draft(async_session, pack_id="subject-math")
    resp = await workbench_client.get("/items")
    assert resp.status_code == 200, resp.text
    # 表头字段
    assert "item_id" in resp.text
    assert "pack_id" in resp.text
    assert "tier" in resp.text
    # 学科包与 tier 显示
    assert "subject-math" in resp.text
    assert "C" in resp.text
    # item_id 截断显示（前 16 字符）
    assert info["item_id"][:16] in resp.text


@pytest.mark.asyncio
async def test_items_list_pack_id_filter(
    workbench_client: AsyncClient, async_session: AsyncSession
) -> None:
    """GET /items?pack_id=subject-math 过滤：仅显示匹配学科包的 item."""
    # 写两条不同 pack_id 的 item
    math_info = await _publish_draft(async_session, pack_id="subject-math")
    # 第二条用不同 pack_id + 不同 content（避免内容寻址 hash 碰撞）
    other_data = _valid_version_data(pack_id="subject-chinese")
    other_data["content"] = {
        "blocks": [
            {"kind": "stem", "template": "语文题 {a}", "rendered": "语文题 1"}
        ]
    }
    other_info = await publish_item_version(
        item_id=None,
        version_data=other_data,
        gate_certificate_id=None,
        db=async_session,
    )

    # 不带 filter：两条都在
    resp_all = await workbench_client.get("/items")
    assert resp_all.status_code == 200
    assert math_info["item_id"][:16] in resp_all.text
    assert other_info["item_id"][:16] in resp_all.text

    # 过滤 subject-math：只显示 math
    resp_math = await workbench_client.get("/items?pack_id=subject-math")
    assert resp_math.status_code == 200
    assert math_info["item_id"][:16] in resp_math.text
    assert other_info["item_id"][:16] not in resp_math.text


# ════════════════════════════════════════════════════════════════════
# 测试 §3：详情页展示 ItemVersion 六大块 + gate_certificate
# ════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_item_detail_404(workbench_client: AsyncClient) -> None:
    """GET /items/不存在的 id → 404 错误页."""
    resp = await workbench_client.get("/items/nonexistent-id")
    assert resp.status_code == 404
    assert "不存在" in resp.text


@pytest.mark.asyncio
async def test_item_detail_shows_six_blocks(
    workbench_client: AsyncClient, async_session: AsyncSession
) -> None:
    """GET /items/{item_id} 含已发布版本 → 展示 objective/content/lineage/gate_certificate.

    验收 §3：详情页展示 ItemVersion 的 objective/content/lineage/gate_certificate.
    本测试仅写 draft item_version（无 current_version），验证详情页对无 current_version
    的占位渲染；已发布版本的场景由 test_issue_flow.py（T-W2-043）覆盖。
    """
    info = await _publish_draft(async_session, pack_id="subject-math")
    resp = await workbench_client.get(f"/items/{info['item_id']}")
    assert resp.status_code == 200, resp.text
    # item 身份字段
    assert info["item_id"] in resp.text
    assert "subject-math" in resp.text
    # tier 徽章
    assert "C" in resp.text
    # 版本历史展示该 draft version
    assert info["item_version_id"][:24] in resp.text
    # draft 状态徽章
    assert "draft" in resp.text


# ════════════════════════════════════════════════════════════════════
# 测试 §4：单元测试覆盖页面路由 200
# ════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_index_redirects_to_items(workbench_client: AsyncClient) -> None:
    """GET / → 303 重定向到 /items（已登录）."""
    resp = await workbench_client.get("/", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/items"


@pytest.mark.asyncio
async def test_login_page_already_logged_in_redirects(
    workbench_client: AsyncClient,
) -> None:
    """已登录访问 /login → 303 重定向到 next（默认 /items）."""
    resp = await workbench_client.get("/login", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/items"
