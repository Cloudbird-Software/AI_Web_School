"""T-W4-042 冻结契约 v1 守卫测试.

任务卡验收标准：
1. openapi-v1.yaml 覆盖全部 C 端与教研端接口，通过 OpenAPI 语法校验。
2. check_openapi_diff.py 对比 PR 前后的 openapi-v1.yaml，发现 diff 即退出码非零。
3. 契约测试遍历所有端点，断言响应 schema 与 openapi-v1 一致。
4. make accept TASK=T-W4-042 全绿；E2E-10 承载卡。
5. 不 import 任何学科包/学段包.

本测试与 tests/contract/api/test_openapi_contract.py 互补：
- test_openapi_contract.py 比对实现 /openapi.json 与 openapi.yaml（live 草稿）
- 本测试比对实现 /openapi.json 与 openapi-v1.yaml（冻结 v1）——
  确保实现不偏离冻结契约，consumer（学生模拟器/小程序团队）可凭 v1 开工.

宪法 X6：本测试不 import 学科包；契约文件是 API 实现的冻结快照。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any, AsyncIterator

import pytest
import pytest_asyncio
import yaml
from httpx import ASGITransport, AsyncClient

from src.api.deps import get_async_session
from src.api.main import create_app

# ────────────────────────────────────────────────────────────────────
# 冻结契约文件路径
# ────────────────────────────────────────────────────────────────────

PROJECT_ROOT = Path(__file__).resolve().parents[2]
FROZEN_CONTRACT_PATH = PROJECT_ROOT / "specs" / "contracts" / "api" / "openapi-v1.yaml"
FROZEN_TXT_PATH = PROJECT_ROOT / "specs" / "contracts" / "FROZEN.txt"
DIFF_SCRIPT = PROJECT_ROOT / "scripts" / "ci" / "check_openapi_diff.py"


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
# 辅助
# ────────────────────────────────────────────────────────────────────


def _load_frozen_contract() -> dict[str, Any]:
    """加载冻结契约 YAML 为 dict."""
    assert FROZEN_CONTRACT_PATH.is_file(), f"冻结契约不存在：{FROZEN_CONTRACT_PATH}"
    return yaml.safe_load(FROZEN_CONTRACT_PATH.read_text(encoding="utf-8"))


def _diff_keys(contract_set: set[str], impl_set: set[str], label: str) -> list[str]:
    """比对两组键，返回 diff 描述行列表."""
    diffs: list[str] = []
    added = impl_set - contract_set
    removed = contract_set - impl_set
    if added:
        diffs.append(f"[{label}] 实现新增（契约未声明）：{sorted(added)}")
    if removed:
        diffs.append(f"[{label}] 实现删除（契约已声明）：{sorted(removed)}")
    return diffs


# ════════════════════════════════════════════════════════════════════
# 验收 #1：openapi-v1.yaml 存在 + 语法校验 + 覆盖核心端点
# ════════════════════════════════════════════════════════════════════


def test_frozen_contract_file_exists() -> None:
    """specs/contracts/api/openapi-v1.yaml 冻结契约文件存在（验收 #1）."""
    assert FROZEN_CONTRACT_PATH.is_file()


def test_frozen_contract_is_valid_yaml() -> None:
    """openapi-v1.yaml 通过 YAML 语法校验（验收 #1）."""
    contract = _load_frozen_contract()
    assert isinstance(contract, dict)
    assert contract.get("openapi", "").startswith("3."), "OpenAPI 版本应 3.x"


def test_frozen_contract_marked_frozen() -> None:
    """openapi-v1.yaml 含 x-frozen: true 标记（冻结语义）."""
    contract = _load_frozen_contract()
    info = contract.get("info", {})
    assert info.get("x-frozen") is True, "info.x-frozen 应为 true"
    assert "frozen" in info.get("version", ""), "info.version 应含 frozen 标记"


def test_frozen_contract_listed_in_frozen_txt() -> None:
    """openapi-v1.yaml 列入 FROZEN.txt（人类维护的冻结清单）."""
    assert FROZEN_TXT_PATH.is_file()
    content = FROZEN_TXT_PATH.read_text(encoding="utf-8")
    assert "specs/contracts/api/openapi-v1.yaml" in content, (
        "FROZEN.txt 应含 openapi-v1.yaml 路径"
    )


def test_frozen_contract_covers_student_and_teacher_endpoints() -> None:
    """openapi-v1.yaml 覆盖 C 端（学生）与教研端核心端点（验收 #1）.

    C 端：sessions（领卷/作答/休息/放弃）+ reports（弱项）+ review（复习）
    教研端：items / item_versions / templates / gate_certificates（只读查询）
    """
    contract = _load_frozen_contract()
    paths = set(contract.get("paths", {}).keys())
    required = {
        # 教研端只读
        "/items/{item_id}",
        "/item_versions/{item_version_id}",
        "/templates/{template_id}",
        "/gate_certificates/{cert_id}",
        # C 端学生侧
        "/sessions",
        "/sessions/{session_id}",
        "/sessions/{session_id}/next",
        "/sessions/{session_id}/responses",
        "/sessions/{session_id}/resume",
        "/sessions/{session_id}/abandon",
        # 报告与复习
        "/reports/weakness/{student_alias_id}",
        "/review/due/{student_alias_id}",
        # 元信息
        "/health",
    }
    missing = required - paths
    assert not missing, f"冻结契约缺端点：{sorted(missing)}"


# ════════════════════════════════════════════════════════════════════
# 验收 #3：契约测试遍历所有端点，断言响应 schema 与 openapi-v1 一致
# ════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_impl_paths_match_frozen_v1(api_client_no_db: AsyncClient) -> None:
    """实现的路径集合与冻结 v1 一致（验收 #3）."""
    contract = _load_frozen_contract()
    resp = await api_client_no_db.get("/openapi.json")
    impl = resp.json()

    contract_paths = set(contract.get("paths", {}).keys())
    impl_paths = set(impl.get("paths", {}).keys())
    diffs = _diff_keys(contract_paths, impl_paths, "paths")
    assert not diffs, "路径 diff（实现 vs 冻结 v1）：\n" + "\n".join(diffs)


@pytest.mark.asyncio
async def test_impl_methods_match_frozen_v1(api_client_no_db: AsyncClient) -> None:
    """每路径的方法集合与冻结 v1 一致（验收 #3）."""
    contract = _load_frozen_contract()
    impl = (await api_client_no_db.get("/openapi.json")).json()
    HTTP_METHODS = {"get", "post", "put", "patch", "delete"}

    all_diffs: list[str] = []
    for path in set(contract.get("paths", {}).keys()) | set(impl.get("paths", {}).keys()):
        c_methods = {m for m in contract.get("paths", {}).get(path, {}).keys() if m in HTTP_METHODS}
        i_methods = {m for m in impl.get("paths", {}).get(path, {}).keys() if m in HTTP_METHODS}
        all_diffs.extend(_diff_keys(c_methods, i_methods, f"methods @ {path}"))
    assert not all_diffs, "方法 diff：\n" + "\n".join(all_diffs)


@pytest.mark.asyncio
async def test_impl_responses_match_frozen_v1(api_client_no_db: AsyncClient) -> None:
    """每路径×方法的状态码集合与冻结 v1 一致（验收 #3）."""
    contract = _load_frozen_contract()
    impl = (await api_client_no_db.get("/openapi.json")).json()
    HTTP_METHODS = {"get", "post", "put", "patch", "delete"}

    all_diffs: list[str] = []
    for path in set(contract.get("paths", {}).keys()) | set(impl.get("paths", {}).keys()):
        for method in HTTP_METHODS:
            c_op = contract.get("paths", {}).get(path, {}).get(method, {})
            i_op = impl.get("paths", {}).get(path, {}).get(method, {})
            if not c_op and not i_op:
                continue
            c_codes = set(c_op.get("responses", {}).keys())
            i_codes = set(i_op.get("responses", {}).keys())
            all_diffs.extend(_diff_keys(c_codes, i_codes, f"responses @ {method.upper()} {path}"))
    assert not all_diffs, "状态码 diff：\n" + "\n".join(all_diffs)


@pytest.mark.asyncio
async def test_impl_schemas_match_frozen_v1(api_client_no_db: AsyncClient) -> None:
    """components.schemas 名称集合与冻结 v1 一致（验收 #3 schema 一致性）."""
    contract = _load_frozen_contract()
    impl = (await api_client_no_db.get("/openapi.json")).json()

    c_schemas = set(contract.get("components", {}).get("schemas", {}).keys())
    i_schemas = set(impl.get("components", {}).get("schemas", {}).keys())
    diffs = _diff_keys(c_schemas, i_schemas, "schemas")
    assert not diffs, "schema diff：\n" + "\n".join(diffs)


@pytest.mark.asyncio
async def test_impl_parameters_match_frozen_v1(api_client_no_db: AsyncClient) -> None:
    """每路径×方法的参数名集合与冻结 v1 一致（验收 #3）."""
    contract = _load_frozen_contract()
    impl = (await api_client_no_db.get("/openapi.json")).json()
    HTTP_METHODS = {"get", "post", "put", "patch", "delete"}

    all_diffs: list[str] = []
    for path in set(contract.get("paths", {}).keys()) | set(impl.get("paths", {}).keys()):
        for method in HTTP_METHODS:
            c_op = contract.get("paths", {}).get(path, {}).get(method, {})
            i_op = impl.get("paths", {}).get(path, {}).get(method, {})
            if not c_op and not i_op:
                continue
            c_params = {p.get("name") for p in c_op.get("parameters", [])}
            i_params = {p.get("name") for p in i_op.get("parameters", [])}
            all_diffs.extend(_diff_keys(c_params, i_params, f"parameters @ {method.upper()} {path}"))
    assert not all_diffs, "参数 diff：\n" + "\n".join(all_diffs)


# ════════════════════════════════════════════════════════════════════
# 验收 #2：check_openapi_diff.py 脚本存在 + 可执行
# ════════════════════════════════════════════════════════════════════


def test_check_openapi_diff_script_exists() -> None:
    """scripts/ci/check_openapi_diff.py 存在（验收 #2）."""
    assert DIFF_SCRIPT.is_file(), f"diff 检测脚本不存在：{DIFF_SCRIPT}"


def test_check_openapi_diff_script_no_diff_on_clean_main() -> None:
    """check_openapi_diff.py 在 HEAD 未改 openapi-v1.yaml 时退出码 0（验收 #2）.

    场景：本分支相对 main 新增了 openapi-v1.yaml（首次创建），脚本应能识别
    这是「新增文件」而非「修改既有冻结契约」——即对尚未在 base 中存在的
    文件不做阻断（否则首次创建就无法合入）。

    实测策略：直接调用脚本，base=HEAD（自身对比自身）→ 必无 diff。
    """
    result = subprocess.run(
        [sys.executable, str(DIFF_SCRIPT), "--base", "HEAD", "--head", "HEAD"],
        capture_output=True,
        text=True,
        cwd=str(PROJECT_ROOT),
    )
    assert result.returncode == 0, (
        f"自身对比应无 diff，但退出码 {result.returncode}\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )


def test_check_openapi_diff_script_importable_as_library() -> None:
    """check_openapi_diff.py 可作为库导入（has_frozen_changes / collect_diff）."""
    # 通过 subprocess 导入避免污染当前 pytest 进程
    result = subprocess.run(
        [
            sys.executable, "-c",
            "from scripts.ci.check_openapi_diff import "
            "collect_diff, has_frozen_changes, DiffSummary, EXEMPTION_MARKER; "
            "print('OK')",
        ],
        capture_output=True,
        text=True,
        cwd=str(PROJECT_ROOT),
    )
    assert result.returncode == 0, (
        f"库导入失败：{result.stderr}"
    )
    assert "OK" in result.stdout


# ════════════════════════════════════════════════════════════════════
# 验收 #5：不 import 学科包/学段包
# ════════════════════════════════════════════════════════════════════


def test_check_openapi_diff_script_does_not_import_packs() -> None:
    """check_openapi_diff.py 不 import 学科包/学段包（A5 静态实证）."""
    src = DIFF_SCRIPT.read_text(encoding="utf-8")
    forbidden = (
        "packs.",
        "gradeband_low",
        "subject-math",
        "subject-chinese",
        "subject-english",
    )
    for needle in forbidden:
        assert needle not in src, f"check_openapi_diff.py 含禁用 import: {needle!r}"
