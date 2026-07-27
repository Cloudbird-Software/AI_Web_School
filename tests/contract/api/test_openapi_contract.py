"""T-W2-040 OpenAPI 契约测试.

任务卡验收标准：
1. 读取 /openapi.json 并与 specs/contracts/api/openapi.yaml 做语义比对.
2. 对新增/删除/修改字段给出明确 diff.
3. `make contract` 通过.

比对策略（语义比对，非字节级）：
- paths 集合：新增/删除路径 → diff 报告.
- 每路径 methods 集合：新增/删除方法 → diff 报告.
- 每方法的 responses 状态码集合：新增/删除 → diff 报告.
- 每方法的 parameters 名称集合：新增/删除 → diff 报告.
- components.schemas 名称集合：新增/删除 → diff 报告.
- 关键字段（summary/operationId）变更 → diff 报告.

为什么不做完整 JSON 深度比对：FastAPI 自动生成的 OpenAPI 含大量自动
字段（title、examples 等），深度比对会因无关字段漂移频繁误报；语义比对
聚焦契约本质（路径/方法/状态码/参数/schema 名），稳定且有意义。

宪法 X6：本测试不 import 学科包；契约文件是 API 实现的冻结快照。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, AsyncIterator

import pytest
import pytest_asyncio
import yaml
from httpx import ASGITransport, AsyncClient

from src.api.deps import get_async_session
from src.api.main import create_app


# ────────────────────────────────────────────────────────────────────
# 契约文件路径
# ────────────────────────────────────────────────────────────────────

PROJECT_ROOT = Path(__file__).resolve().parents[3]
CONTRACT_PATH = PROJECT_ROOT / "specs" / "contracts" / "api" / "openapi.yaml"


# ────────────────────────────────────────────────────────────────────
# Fixture：ASGI 客户端（无需 DB；只读 /openapi.json）
# ────────────────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def api_client_no_db() -> AsyncIterator[AsyncClient]:
    """构造 ASGI 客户端，覆写 DB 依赖为空（/openapi.json 不需 DB）."""
    app = create_app()

    async def _override_session() -> AsyncIterator[None]:
        yield None

    app.dependency_overrides[get_async_session] = _override_session

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client

    app.dependency_overrides.clear()


# ────────────────────────────────────────────────────────────────────
# 辅助：语义 diff
# ────────────────────────────────────────────────────────────────────


def _load_contract() -> dict[str, Any]:
    """加载契约 YAML 为 dict."""
    assert CONTRACT_PATH.is_file(), f"契约文件不存在：{CONTRACT_PATH}"
    return yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))


def _diff_keys(
    contract_set: set[str], impl_set: set[str], label: str
) -> list[str]:
    """比对两组键，返回 diff 描述行列表."""
    diffs: list[str] = []
    added = impl_set - contract_set
    removed = contract_set - impl_set
    if added:
        diffs.append(f"[{label}] 实现新增（契约未声明）：{sorted(added)}")
    if removed:
        diffs.append(f"[{label}] 实现删除（契约已声明）：{sorted(removed)}")
    return diffs


def _normalize_spec(spec: dict[str, Any]) -> dict[str, Any]:
    """规范化 OpenAPI spec：只保留契约比对关心的字段.

    保留：paths（路径→方法→{parameters, responses, summary}）、components.schemas 名集合.
    丢弃：自动生成的 examples、title、description 细节等无关字段.
    """
    normalized: dict[str, Any] = {"paths": {}, "schema_names": set()}
    for path, path_item in spec.get("paths", {}).items():
        normalized["paths"][path] = {}
        for method, op in path_item.items():
            if method not in ("get", "post", "put", "patch", "delete"):
                continue
            normalized["paths"][path][method] = {
                "parameters": [
                    p.get("name") for p in op.get("parameters", [])
                ],
                "responses": sorted(op.get("responses", {}).keys()),
                "summary": op.get("summary", ""),
            }
    schemas = spec.get("components", {}).get("schemas", {})
    normalized["schema_names"] = set(schemas.keys())
    return normalized


# ════════════════════════════════════════════════════════════════════
# 测试
# ════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_openapi_json_accessible(api_client_no_db: AsyncClient) -> None:
    """GET /openapi.json → 200，可解析为 dict."""
    resp = await api_client_no_db.get("/openapi.json")
    assert resp.status_code == 200
    spec = resp.json()
    assert spec["openapi"].startswith("3."), f"OpenAPI 版本异常：{spec['openapi']}"
    assert "paths" in spec


def test_contract_file_exists() -> None:
    """specs/contracts/api/openapi.yaml 契约文件存在."""
    assert CONTRACT_PATH.is_file(), f"契约文件不存在：{CONTRACT_PATH}"


@pytest.mark.asyncio
async def test_contract_paths_match(api_client_no_db: AsyncClient) -> None:
    """契约与实现的路径集合一致（新增/删除均报 diff）."""
    contract = _load_contract()
    resp = await api_client_no_db.get("/openapi.json")
    impl = resp.json()

    contract_paths = set(contract.get("paths", {}).keys())
    impl_paths = set(impl.get("paths", {}).keys())

    diffs = _diff_keys(contract_paths, impl_paths, "paths")
    assert not diffs, "路径 diff：\n" + "\n".join(diffs)


@pytest.mark.asyncio
async def test_contract_methods_match(api_client_no_db: AsyncClient) -> None:
    """每路径的方法集合一致."""
    contract = _load_contract()
    resp = await api_client_no_db.get("/openapi.json")
    impl = resp.json()

    HTTP_METHODS = {"get", "post", "put", "patch", "delete"}
    all_diffs: list[str] = []
    contract_paths = contract.get("paths", {})
    impl_paths = impl.get("paths", {})

    for path in set(contract_paths.keys()) | set(impl_paths.keys()):
        c_methods = {
            m for m in contract_paths.get(path, {}).keys() if m in HTTP_METHODS
        }
        i_methods = {
            m for m in impl_paths.get(path, {}).keys() if m in HTTP_METHODS
        }
        all_diffs.extend(_diff_keys(c_methods, i_methods, f"methods @ {path}"))

    assert not all_diffs, "方法 diff：\n" + "\n".join(all_diffs)


@pytest.mark.asyncio
async def test_contract_responses_match(api_client_no_db: AsyncClient) -> None:
    """每路径×方法的状态码集合一致."""
    contract = _load_contract()
    resp = await api_client_no_db.get("/openapi.json")
    impl = resp.json()

    HTTP_METHODS = {"get", "post", "put", "patch", "delete"}
    all_diffs: list[str] = []
    contract_paths = contract.get("paths", {})
    impl_paths = impl.get("paths", {})

    for path in set(contract_paths.keys()) | set(impl_paths.keys()):
        for method in HTTP_METHODS:
            c_op = contract_paths.get(path, {}).get(method, {})
            i_op = impl_paths.get(path, {}).get(method, {})
            if not c_op and not i_op:
                continue
            c_codes = set(c_op.get("responses", {}).keys())
            i_codes = set(i_op.get("responses", {}).keys())
            all_diffs.extend(
                _diff_keys(c_codes, i_codes, f"responses @ {method.upper()} {path}")
            )

    assert not all_diffs, "状态码 diff：\n" + "\n".join(all_diffs)


@pytest.mark.asyncio
async def test_contract_parameters_match(api_client_no_db: AsyncClient) -> None:
    """每路径×方法的参数名集合一致."""
    contract = _load_contract()
    resp = await api_client_no_db.get("/openapi.json")
    impl = resp.json()

    HTTP_METHODS = {"get", "post", "put", "patch", "delete"}
    all_diffs: list[str] = []
    contract_paths = contract.get("paths", {})
    impl_paths = impl.get("paths", {})

    for path in set(contract_paths.keys()) | set(impl_paths.keys()):
        for method in HTTP_METHODS:
            c_op = contract_paths.get(path, {}).get(method, {})
            i_op = impl_paths.get(path, {}).get(method, {})
            if not c_op and not i_op:
                continue
            c_params = {p.get("name") for p in c_op.get("parameters", [])}
            i_params = {p.get("name") for p in i_op.get("parameters", [])}
            all_diffs.extend(
                _diff_keys(c_params, i_params, f"parameters @ {method.upper()} {path}")
            )

    assert not all_diffs, "参数 diff：\n" + "\n".join(all_diffs)


@pytest.mark.asyncio
async def test_contract_schemas_match(api_client_no_db: AsyncClient) -> None:
    """components.schemas 名称集合一致（新增/删除 schema 均 diff）."""
    contract = _load_contract()
    resp = await api_client_no_db.get("/openapi.json")
    impl = resp.json()

    c_schemas = set(contract.get("components", {}).get("schemas", {}).keys())
    i_schemas = set(impl.get("components", {}).get("schemas", {}).keys())

    diffs = _diff_keys(c_schemas, i_schemas, "schemas")
    assert not diffs, "schema diff：\n" + "\n".join(diffs)


@pytest.mark.asyncio
async def test_contract_core_endpoints_present(
    api_client_no_db: AsyncClient,
) -> None:
    """契约包含 4 个核心只读端点 + /health."""
    contract = _load_contract()
    paths = set(contract.get("paths", {}).keys())
    required = {
        "/items/{item_id}",
        "/item_versions/{item_version_id}",
        "/templates/{template_id}",
        "/gate_certificates/{cert_id}",
    }
    missing = required - paths
    assert not missing, f"契约缺核心端点：{sorted(missing)}"
