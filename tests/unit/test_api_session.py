"""W3-S3 会话 API 路由单元测试.

覆盖：POST /sessions（201/422）→ GET next → POST responses（反馈）→
GET state → 404/409 错误映射；时长保护的 409 rest_required 结构
（时长保护逻辑本身在 test_session_service.py 以注入时钟覆盖，
API 层用 monkeypatch 模拟 RestRequiredError 验证映射）。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import AsyncIterator
from uuid import uuid4

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import get_async_session
from src.api.main import create_app
from src.api.routers import session as session_router
from src.core.content.writer import publish_item_version
from src.core.session.service import RestRequiredError

# ────────────────────────────────────────────────────────────────────
# 数学包评分器注册（API 进程不 import 学科包，测试进程代为加载）
# ────────────────────────────────────────────────────────────────────

_MATH_SCORERS_PATH = (
    Path(__file__).resolve().parents[2]
    / "src" / "packs" / "subject-math" / "scorers" / "__init__.py"
)


def _register_math_scorers() -> None:
    spec = importlib.util.spec_from_file_location(
        "subject_math_scorers_pkg", _MATH_SCORERS_PATH
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["subject_math_scorers_pkg"] = mod
    spec.loader.exec_module(mod)
    mod.register_math_scorers()


_register_math_scorers()


# ────────────────────────────────────────────────────────────────────
# Fixture：API 客户端走 async_session（同 test_api_readonly 模式）
# ────────────────────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def api_client(async_session: AsyncSession) -> AsyncIterator[AsyncClient]:
    app = create_app()

    async def _override_session() -> AsyncIterator[AsyncSession]:
        yield async_session

    app.dependency_overrides[get_async_session] = _override_session
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
    app.dependency_overrides.clear()


async def _publish_choice(db: AsyncSession, stem: str) -> str:
    result = await publish_item_version(
        item_id=None,
        version_data={
            "pack_id": "subject-math",
            "tier": "A",
            "status": "published",
            "objective": {
                "kp_set": [{"dimension": "kp", "code": "math.nal.decimal.compare"}],
                "kp_set_mode": "single",
                "cognitive_level": "apply",
                "gradeband": "M",
                "graph_release": "graph-v1",
            },
            "interaction_ref": {
                "interaction_id": "single_choice", "interaction_params": {},
            },
            "content": {"blocks": [{"kind": "stem", "rendered": stem}]},
            "scoring_ref": {"scorer_id": "exact_match", "scorer_params": {"answer": "B"}},
            "error_bindings": [
                {"option_value": "A", "label": "位数多的小数更大",
                 "error_type_id": "math.decimal.digits_more_is_larger"},
            ],
            "lineage": {
                "tier": "A",
                "pipeline": {"id": "test-pipeline", "version": "1.0"},
                "signed_by": "test-author",
                "signed_at": "2026-07-27T00:00:00Z",
            },
        },
        gate_certificate_id="cert-test-w3",
        db=db,
    )
    return result["item_version_id"]


# ════════════════════════════════════════════════════════════════════
# 端点全流程
# ════════════════════════════════════════════════════════════════════

async def test_session_api_full_flow(
    api_client: AsyncClient, async_session: AsyncSession
):
    """POST→GET next→POST responses×2→GET state：全流程 200/201."""
    v1 = await _publish_choice(async_session, "题一")
    v2 = await _publish_choice(async_session, "题二")

    # 开始练习
    resp = await api_client.post("/sessions", json={
        "student_alias_id": str(uuid4()),
        "gradeband": "M",
        "item_version_ids": [v1, v2],
    })
    assert resp.status_code == 201, resp.text
    body = resp.json()
    session_id = body["session_id"]
    assert body["total"] == 2
    assert body["time_limit_sec"] == 3600

    # 取下一题
    resp = await api_client.get(f"/sessions/{session_id}/next")
    assert resp.status_code == 200
    nxt = resp.json()
    assert nxt["item_version_id"] == v1
    assert nxt["interaction_id"] == "single_choice"
    assert nxt["round"] == "main"

    # 提交作答（答错 → 反馈含错误类型）
    resp = await api_client.post(
        f"/sessions/{session_id}/responses",
        json={"item_version_id": v1, "response": {"selected": "A"},
              "duration_ms": 3000},
    )
    assert resp.status_code == 200, resp.text
    fb = resp.json()
    assert fb["correct"] is False
    assert fb["error_feedback"][0]["error_type_id"] == (
        "math.decimal.digits_more_is_larger"
    )
    assert fb["event_id"]

    # 第二题答对 → 会话完成
    resp = await api_client.get(f"/sessions/{session_id}/next")
    assert resp.json()["item_version_id"] == v2
    resp = await api_client.post(
        f"/sessions/{session_id}/responses",
        json={"item_version_id": v2, "response": {"selected": "B"}},
    )
    assert resp.json()["correct"] is True

    # 再取题 → done
    resp = await api_client.get(f"/sessions/{session_id}/next")
    assert resp.json()["done"] is True

    # 会话状态
    resp = await api_client.get(f"/sessions/{session_id}")
    assert resp.status_code == 200
    state = resp.json()
    assert state["status"] == "completed"
    assert state["answered_count"] == 2
    assert state["correct_count"] == 1
    assert state["wrong_count"] == 1


async def test_session_api_404(api_client: AsyncClient):
    resp = await api_client.get(f"/sessions/{uuid4()}")
    assert resp.status_code == 404


async def test_session_api_422_unpublished(
    api_client: AsyncClient, async_session: AsyncSession
):
    """draft 题目开会话 → 422（门纪律）."""
    draft = await publish_item_version(
        item_id=None,
        version_data={
            "pack_id": "subject-math",
            "tier": "A",
            "status": "draft",
            "objective": {
                "kp_set": [{"dimension": "kp", "code": "math.nal.int.add"}],
                "kp_set_mode": "single",
                "cognitive_level": "apply",
                "gradeband": "M",
                "graph_release": "graph-v1",
            },
            "interaction_ref": {
                "interaction_id": "single_choice", "interaction_params": {},
            },
            "content": {"blocks": [{"kind": "stem", "rendered": "草稿"}]},
            "scoring_ref": {"scorer_id": "exact_match", "scorer_params": {"answer": "B"}},
            "error_bindings": [],
            "lineage": {
                "tier": "A",
                "pipeline": {"id": "test-pipeline", "version": "1.0"},
                "signed_by": "test-author",
                "signed_at": "2026-07-27T00:00:00Z",
            },
        },
        gate_certificate_id=None,
        db=async_session,
    )
    resp = await api_client.post("/sessions", json={
        "student_alias_id": str(uuid4()),
        "gradeband": "M",
        "item_version_ids": [draft["item_version_id"]],
    })
    assert resp.status_code == 422


async def test_session_api_409_out_of_sequence(
    api_client: AsyncClient, async_session: AsyncSession
):
    """跳答 → 409."""
    v1 = await _publish_choice(async_session, "题一")
    v2 = await _publish_choice(async_session, "题二")
    resp = await api_client.post("/sessions", json={
        "student_alias_id": str(uuid4()),
        "gradeband": "M",
        "item_version_ids": [v1, v2],
    })
    session_id = resp.json()["session_id"]
    resp = await api_client.post(
        f"/sessions/{session_id}/responses",
        json={"item_version_id": v2, "response": {"selected": "B"}},
    )
    assert resp.status_code == 409


async def test_session_api_rest_required_mapping(
    api_client: AsyncClient, async_session: AsyncSession, monkeypatch
):
    """时长保护触发 → 409 rest_required 结构（休息提示文案透出）."""
    v = await _publish_choice(async_session, "题")
    resp = await api_client.post("/sessions", json={
        "student_alias_id": str(uuid4()),
        "gradeband": "L",
        "item_version_ids": [v],
    })
    session_id = resp.json()["session_id"]

    async def _raise_rest(*args, **kwargs):
        raise RestRequiredError(
            "已连续作答超过 15 分钟，该休息了——站起来活动一下、看看远处，休息好后回来继续。",
            elapsed_sec=960,
            time_limit_sec=900,
        )

    monkeypatch.setattr(session_router, "get_next_item", _raise_rest)
    resp = await api_client.get(f"/sessions/{session_id}/next")
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["error"] == "rest_required"
    assert "休息" in detail["message"]
    assert detail["time_limit_sec"] == 900


async def test_session_api_resume_and_abandon(
    api_client: AsyncClient, async_session: AsyncSession
):
    """resume/abandon 端点."""
    v = await _publish_choice(async_session, "题")
    resp = await api_client.post("/sessions", json={
        "student_alias_id": str(uuid4()),
        "gradeband": "M",
        "item_version_ids": [v],
    })
    session_id = resp.json()["session_id"]

    resp = await api_client.post(f"/sessions/{session_id}/resume")
    assert resp.status_code == 200
    assert resp.json()["status"] == "active"

    resp = await api_client.post(f"/sessions/{session_id}/abandon")
    assert resp.status_code == 200
    assert resp.json()["status"] == "abandoned"
