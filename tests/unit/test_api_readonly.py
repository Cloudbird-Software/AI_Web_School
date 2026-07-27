"""T-W2-039 只读 API v1 单元测试.

覆盖任务卡验收标准 §1-§5：
1. GET /items/{item_id} 返回 item + current_version（200/404）.
2. GET /item_versions/{item_version_id} 返回版本六大块 + 谱系（200/404）.
3. GET /gate_certificates/{cert_id} 返回证书 + runs（200/404）.
4. /docs 与 /openapi.json 可访问；openapi-draft.yaml 已产出.
5. 单元测试覆盖 200/404.

宪法 D1：测试通过 publish_item_version 写入数据（合法 INSERT 路径），
不绕过门强制；事务回滚隔离保证不污染其他测试。
"""
from __future__ import annotations

from pathlib import Path
from typing import AsyncIterator

import pytest
import pytest_asyncio
from decimal import Decimal
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import get_async_session
from src.api.main import create_app
from src.core.content.writer import publish_item_version
from src.core.gate.orchestrator import run_gate
from src.core.gate.policy.loader import load_default_policy
from src.core.gate.validator import (
    GateContext,
    Validator,
    ValidatorResult,
    register_validator,
    reset_registry,
)


# ────────────────────────────────────────────────────────────────────
# 测试数据：合法 item_version 数据（C 级，无 template）
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
# 测试用桩验证器：稳定 pass，让门编排可签发证书
# ────────────────────────────────────────────────────────────────────
# 为什么自造桩而非用真实验证器：真实验证器依赖 DB 状态（license 表/已发布版本）
# 难以精确控制；桩稳定 pass 让门编排可签发证书，便于 API 端点测试聚焦 200/404.


def _make_always_pass_validator(vid: str) -> type[Validator]:
    """工厂：构造一个永远 pass 的 Validator 子类（注册时覆盖真实验证器）."""

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
                evidence={"note": f"test stub for {vid} always pass"},
                confidence=Decimal("1.000"),
                cost_ms=0,
                cost_tokens=0,
            )

    _Stub.__name__ = f"_AlwaysPass_{vid}"
    return _Stub


def _install_pass_stubs() -> None:
    """重置注册表 + 安装三个 always-pass 桩（schema/license/duplicate_placeholder）."""
    reset_registry()
    for vid in ("schema", "license", "duplicate_placeholder"):
        register_validator("platform", _make_always_pass_validator(vid))


# ────────────────────────────────────────────────────────────────────
# Fixture：覆写 get_async_session，让 API 走测试的 async_session
# ────────────────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def api_client(async_session: AsyncSession) -> AsyncIterator[AsyncClient]:
    """构造 httpx AsyncClient + ASGI 传输，DB 走 async_session fixture.

    为什么用 AsyncClient + ASGITransport 而非 TestClient：async_session 是
    async fixture，TestClient（同步）无法直接复用同一事件循环；AsyncClient
    与 async 测试同事件循环，DB 写入对 API 调用可见。
    """
    app = create_app()

    async def _override_session() -> AsyncIterator[AsyncSession]:
        yield async_session

    app.dependency_overrides[get_async_session] = _override_session

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client

    app.dependency_overrides.clear()


# ────────────────────────────────────────────────────────────────────
# 辅助：写入一条 item_version 并返回 item_id / item_version_id
# ────────────────────────────────────────────────────────────────────


async def _publish_draft(async_session: AsyncSession) -> dict:
    """写入一条 draft item_version，返回 {item_id, item_version_id}."""
    result = await publish_item_version(
        item_id=None,
        version_data=_valid_version_data(),
        gate_certificate_id=None,
        db=async_session,
    )
    return result


async def _publish_with_cert(async_session: AsyncSession) -> dict:
    """写入一条 published item_version + 真实门证书，返回完整信息.

    步骤：
    1. 先写 draft item_version 拿到 item_version_id.
    2. 安装 always-pass 桩验证器 + 加载默认策略.
    3. run_gate 签发证书.
    4. 再写一条 published item_version 引用该证书.
    """
    # 1. 先写 draft 拿到 item_id
    draft = await publish_item_version(
        item_id=None,
        version_data=_valid_version_data(),
        gate_certificate_id=None,
        db=async_session,
    )

    # 2. 安装桩验证器（覆盖真实 schema/license/duplicate_placeholder）
    _install_pass_stubs()
    policy = load_default_policy()

    # 3. 跑门编排签发证书（artifact_ref 用 draft item_version_id）
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
        db=async_session,
        issued_by="test-issuer",
    )
    assert outcome.final_verdict == "pass", f"门编排未通过：{outcome}"
    assert outcome.cert_id is not None

    # 4. 写一条 published item_version 引用该证书
    pub_data = _valid_version_data()
    pub_data["status"] = "published"
    # published 需要新的 item_version_id（内容寻址），改动 content 让 id 不同
    pub_data["content"] = {
        "blocks": [
            {"kind": "stem", "template": "已发布版本：{a} vs {b}", "rendered": "已发布版本：0.3 vs 0.4"}
        ]
    }
    pub = await publish_item_version(
        item_id=draft["item_id"],
        version_data=pub_data,
        gate_certificate_id=outcome.cert_id,
        db=async_session,
    )

    return {
        "item_id": pub["item_id"],
        "item_version_id": pub["item_version_id"],
        "draft_item_version_id": draft["item_version_id"],
        "cert_id": outcome.cert_id,
    }


# ════════════════════════════════════════════════════════════════════
# 测试 §5：200/404 覆盖
# ════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_get_item_404(api_client: AsyncClient) -> None:
    """GET /items/{不存在的 id} → 404."""
    resp = await api_client.get("/items/nonexistent-item-id")
    assert resp.status_code == 404
    assert "不存在" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_get_item_200_draft(api_client: AsyncClient, async_session: AsyncSession) -> None:
    """GET /items/{item_id} → 200，current_version 为 None（draft 未发布）."""
    info = await _publish_draft(async_session)
    resp = await api_client.get(f"/items/{info['item_id']}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["item_id"] == info["item_id"]
    assert body["pack_id"] == "subject-math"
    assert body["tier"] == "C"
    # draft 状态：current_version_id 为 None（publish_item_version 不前移 current_version_id）
    # current_version 字段为 None
    assert body.get("current_version") is None


@pytest.mark.asyncio
async def test_get_item_version_404(api_client: AsyncClient) -> None:
    """GET /item_versions/{不存在} → 404."""
    resp = await api_client.get("/item_versions/nonexistent-version-id")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_get_item_version_200(api_client: AsyncClient, async_session: AsyncSession) -> None:
    """GET /item_versions/{id} → 200，返回六大块 + 谱系."""
    info = await _publish_draft(async_session)
    resp = await api_client.get(f"/item_versions/{info['item_version_id']}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["item_version_id"] == info["item_version_id"]
    assert body["item_id"] == info["item_id"]
    assert body["status"] == "draft"
    # 六大块存在
    for block in ["objective", "interaction_ref", "content", "scoring_ref", "error_bindings", "lineage"]:
        assert block in body, f"缺六大块: {block}"
    # 谱系字段
    assert body["lineage"]["tier"] == "C"
    assert body["lineage"]["pipeline"]["id"] == "test-pipeline"


@pytest.mark.asyncio
async def test_get_gate_certificate_404(api_client: AsyncClient) -> None:
    """GET /gate_certificates/{不存在} → 404."""
    resp = await api_client.get("/gate_certificates/cert_nonexistent")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_get_gate_certificate_200(
    api_client: AsyncClient, async_session: AsyncSession
) -> None:
    """GET /gate_certificates/{cert_id} → 200，返回证书 + runs."""
    info = await _publish_with_cert(async_session)
    resp = await api_client.get(f"/gate_certificates/{info['cert_id']}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["cert_id"] == info["cert_id"]
    assert body["artifact_ref"] == info["draft_item_version_id"]  # cert 的 artifact_ref 指向被检的 draft 版本
    assert body["cert_type"] == "publish"
    assert body["issued_by"] == "test-issuer"
    # 至少有一条 run
    assert len(body["runs"]) >= 1
    run = body["runs"][0]
    assert run["validator_id"] == "schema"
    assert run["verdict"] == "pass"
    # verdicts 字段存在（列表）
    assert "verdicts" in run


@pytest.mark.asyncio
async def test_get_template_404(api_client: AsyncClient) -> None:
    """GET /templates/{不存在} → 404."""
    resp = await api_client.get("/templates/nonexistent-template-id")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_get_template_200(
    api_client: AsyncClient, async_session: AsyncSession
) -> None:
    """GET /templates/{id} → 200，返回母题身份 + current_version=None."""
    # 直接写一条 item_template（无 current_version_id）
    from src.core.models.item_template import ItemTemplate

    template = ItemTemplate(
        template_id="tmpl_test_001",
        pack_id="subject-math",
        current_version_id=None,
    )
    async_session.add(template)
    await async_session.flush()

    resp = await api_client.get("/templates/tmpl_test_001")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["template_id"] == "tmpl_test_001"
    assert body["pack_id"] == "subject-math"
    assert body.get("current_version") is None


# ════════════════════════════════════════════════════════════════════
# 测试 §4：/docs 与 /openapi.json 可访问；openapi-draft.yaml 已产出
# ════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_openapi_json_accessible(api_client: AsyncClient) -> None:
    """GET /openapi.json → 200，含 4 个核心路径."""
    resp = await api_client.get("/openapi.json")
    assert resp.status_code == 200
    spec = resp.json()
    paths = spec["paths"]
    for p in ["/items/{item_id}", "/item_versions/{item_version_id}", "/templates/{template_id}", "/gate_certificates/{cert_id}"]:
        assert p in paths, f"OpenAPI 缺路径 {p}"
    # /health 也应存在（meta 端点）
    assert "/health" in paths


@pytest.mark.asyncio
async def test_docs_accessible(api_client: AsyncClient) -> None:
    """GET /docs → 200（Swagger UI）."""
    resp = await api_client.get("/docs")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_health_endpoint(api_client: AsyncClient) -> None:
    """GET /health → 200 {status: ok}."""
    resp = await api_client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_openapi_draft_yaml_exists() -> None:
    """src/api/openapi-draft.yaml 已产出（T-W2-040 反向定稿契约的输入）."""
    draft_path = Path(__file__).resolve().parents[2] / "src" / "api" / "openapi-draft.yaml"
    assert draft_path.is_file(), f"openapi-draft.yaml 不存在：{draft_path}"
    # 内容可被 yaml 解析
    import yaml

    spec = yaml.safe_load(draft_path.read_text(encoding="utf-8"))
    assert spec["openapi"].startswith("3."), f"OpenAPI 版本不是 3.x：{spec['openapi']}"
    assert "paths" in spec
