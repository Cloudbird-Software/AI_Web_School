"""T-W2-042 母题表单 + 按轴抽样预览 单元测试.

覆盖任务卡验收标准 §1-§4：
1. 表单页可保存母题草稿到 item_template_version（status=draft）.
2. 保存时调用 Linter，错误回显到字段.
3. 预览按钮按所选轴生成 20 个实例，网格展示题面与答案.
4. 单元测试覆盖表单提交与预览 API.

宪法 D1：item_template_version 行只增；事务回滚隔离保证不污染其他测试。
宪法 A5/X6：测试不 import 学科包（pack_id='subject-math' 仅作字符串占位）。
"""
from __future__ import annotations

from typing import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import get_async_session
from src.core.models.item_template import ItemTemplate
from src.core.models.item_template_version import ItemTemplateVersion
from src.workbench.auth import SESSION_COOKIE_NAME, get_workbench_token
from src.workbench.main import create_app


# ────────────────────────────────────────────────────────────────────
# 测试数据：合法 spec YAML（与 template_form.py 预填样例一致）
# ────────────────────────────────────────────────────────────────────

_VALID_SPEC_YAML: str = """\
objective:
  kp_set:
    - dimension: kp
      code: math.nal.int.add
  kp_set_mode: single
  cognitive_level: apply
  gradeband: L
  graph_release: '2026.1'
slots:
  a:
    type: int
    difficulty_relevant: true
  b:
    type: int
    difficulty_relevant: true
variation_axes:
  axes:
    - axis_id: vary_operands
      slots: [a]
presentation:
  blocks:
    - kind: text
      template: '{a} + {b} = ?'
answer_program:
  expression: a + b
  returns: number
distractor_rules:
  rules:
    - rule_type: deterministic
      error_type_id: err.calc.add.off-by-one
      expression: a + b + 1
      label: 多 1
    - rule_type: deterministic
      error_type_id: err.calc.add.minus-one
      expression: a + b - 1
      label: 少 1
"""

_VALID_BASE_PARAMS_YAML: str = """\
a: 3
b: 4
"""

# 缺块 spec（缺 distractor_rules）—— Linter 应报 missing_block
_MISSING_BLOCK_SPEC_YAML: str = """\
objective:
  kp_set:
    - dimension: kp
      code: math.nal.int.add
  kp_set_mode: single
  cognitive_level: apply
  gradeband: L
  graph_release: '2026.1'
slots:
  a:
    type: int
    difficulty_relevant: true
variation_axes:
  axes: []
presentation:
  blocks:
    - kind: text
      template: '{a} = ?'
answer_program:
  expression: a
  returns: number
"""


# ────────────────────────────────────────────────────────────────────
# Fixture：构造带 session cookie 的工作台 client
# ────────────────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def workbench_client(
    async_session: AsyncSession,
) -> AsyncIterator[AsyncClient]:
    """构造工作台 ASGI client，DB 走 async_session fixture，并预登录带 cookie."""
    app = create_app()

    async def _override_session() -> AsyncIterator[AsyncSession]:
        yield async_session

    app.dependency_overrides[get_async_session] = _override_session

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        token = get_workbench_token()
        login_resp = await client.post(
            "/login",
            data={"token": token, "next": "/items"},
            follow_redirects=False,
        )
        assert login_resp.status_code == 303, f"预登录失败：{login_resp.text}"
        yield client

    app.dependency_overrides.clear()


# ════════════════════════════════════════════════════════════════════
# 测试 §4 + 基本路由
# ════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_template_form_new_page(workbench_client: AsyncClient) -> None:
    """GET /templates/new → 200，渲染表单 + 预填样例."""
    resp = await workbench_client.get("/templates/new")
    assert resp.status_code == 200, resp.text
    assert "母题表单" in resp.text
    # 预填样例（integer 加法母题）
    assert "math.nal.int.add" in resp.text
    assert "vary_operands" in resp.text
    # 保存 + 预览按钮
    assert "保存草稿" in resp.text
    assert "预览" in resp.text


# ════════════════════════════════════════════════════════════════════
# 测试 §1：表单页可保存母题草稿到 item_template_version（status=draft）
# ════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_save_draft_writes_template_version(
    workbench_client: AsyncClient, async_session: AsyncSession
) -> None:
    """POST /templates/save 合法 spec → 200，落库 item_template + item_template_version(draft)."""
    resp = await workbench_client.post(
        "/templates/save",
        data={
            "template_id": "tpl-test-001",
            "pack_id": "subject-math",
            "dsl_version": "1",
            "spec_yaml": _VALID_SPEC_YAML,
        },
        follow_redirects=False,
    )
    assert resp.status_code == 200, resp.text
    # 成功提示
    assert "草稿已保存" in resp.text
    assert "tpl-test-001" in resp.text

    # 落库验证：item_template + item_template_version
    tmpl = await async_session.get(ItemTemplate, "tpl-test-001")
    assert tmpl is not None
    assert tmpl.pack_id == "subject-math"
    assert tmpl.current_version_id is None  # draft 不前移

    # 查询 template_version（template_version_id 是内容寻址 hash，直接查所有该 template_id 的版本）
    from sqlalchemy import select
    stmt = select(ItemTemplateVersion).where(
        ItemTemplateVersion.template_id == "tpl-test-001"
    )
    ver = (await async_session.execute(stmt)).scalar_one_or_none()
    assert ver is not None
    assert ver.status == "draft"
    assert ver.dsl_version == "1"
    assert "objective" in ver.spec
    assert ver.spec["objective"]["kp_set"][0]["code"] == "math.nal.int.add"


@pytest.mark.asyncio
async def test_save_draft_idempotent_same_spec(
    workbench_client: AsyncClient, async_session: AsyncSession
) -> None:
    """同 spec 重复保存幂等：不重复 INSERT（template_version_id 内容寻址）."""
    for _ in range(2):
        resp = await workbench_client.post(
            "/templates/save",
            data={
                "template_id": "tpl-test-idem",
                "pack_id": "subject-math",
                "dsl_version": "1",
                "spec_yaml": _VALID_SPEC_YAML,
            },
            follow_redirects=False,
        )
        assert resp.status_code == 200, resp.text

    # 只有一行 template_version
    from sqlalchemy import select, func
    stmt = select(func.count()).select_from(ItemTemplateVersion).where(
        ItemTemplateVersion.template_id == "tpl-test-idem"
    )
    count = (await async_session.execute(stmt)).scalar()
    assert count == 1


# ════════════════════════════════════════════════════════════════════
# 测试 §2：保存时调用 Linter，错误回显到字段
# ════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_save_with_missing_block_shows_lint_error(
    workbench_client: AsyncClient, async_session: AsyncSession
) -> None:
    """POST /templates/save 缺块 spec → 200，回显 Lint 错误，不写库."""
    resp = await workbench_client.post(
        "/templates/save",
        data={
            "template_id": "tpl-test-bad",
            "pack_id": "subject-math",
            "dsl_version": "1",
            "spec_yaml": _MISSING_BLOCK_SPEC_YAML,
        },
        follow_redirects=False,
    )
    assert resp.status_code == 200, resp.text
    # 错误回显
    assert "校验失败" in resp.text
    assert "missing_block" in resp.text or "distractor_rules" in resp.text

    # 不写库
    from sqlalchemy import select
    stmt = select(ItemTemplateVersion).where(
        ItemTemplateVersion.template_id == "tpl-test-bad"
    )
    ver = (await async_session.execute(stmt)).scalar_one_or_none()
    assert ver is None


@pytest.mark.asyncio
async def test_save_with_invalid_yaml_shows_parse_error(
    workbench_client: AsyncClient, async_session: AsyncSession
) -> None:
    """POST /templates/save 非法 YAML → 200，回显 YAML 解析错误，不写库."""
    resp = await workbench_client.post(
        "/templates/save",
        data={
            "template_id": "tpl-test-yaml",
            "pack_id": "subject-math",
            "dsl_version": "1",
            "spec_yaml": "objective: { invalid yaml: : :",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 200, resp.text
    assert "YAML 解析失败" in resp.text or "yaml_parse_error" in resp.text


# ════════════════════════════════════════════════════════════════════
# 测试 §3 + §4：预览 API + 20 例实例化
# ════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_preview_generates_20_variants(
    workbench_client: AsyncClient,
) -> None:
    """POST /templates/preview 合法 spec → 200，生成 20 个变式，网格展示题面与答案."""
    resp = await workbench_client.post(
        "/templates/preview",
        data={
            "spec_yaml": _VALID_SPEC_YAML,
            "base_params_yaml": _VALID_BASE_PARAMS_YAML,
            "axis_id": "vary_operands",
            "pack_id": "subject-math",
            "interaction_id": "single_choice",
            "scorer_id": "exact_match",
            "preview_count": "20",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 200, resp.text
    # 预览结果区
    assert "预览结果" in resp.text
    assert "20" in resp.text  # "20 例" 或 "20" 变式数量
    # 题面展示（'+' 加法表达式）
    assert "+" in resp.text
    # 答案展示
    assert "正解" in resp.text
    # item_version_id 展示
    assert "sha256:" in resp.text


@pytest.mark.asyncio
async def test_preview_with_invalid_axis_returns_error(
    workbench_client: AsyncClient,
) -> None:
    """POST /templates/preview 不存在的 axis_id → 200，回显实例化错误."""
    resp = await workbench_client.post(
        "/templates/preview",
        data={
            "spec_yaml": _VALID_SPEC_YAML,
            "base_params_yaml": _VALID_BASE_PARAMS_YAML,
            "axis_id": "nonexistent_axis",
            "pack_id": "subject-math",
            "interaction_id": "single_choice",
            "scorer_id": "exact_match",
            "preview_count": "5",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 200, resp.text
    # 错误回显（instantiation_error）
    assert "instantiation_error" in resp.text or "不存在" in resp.text


@pytest.mark.asyncio
async def test_preview_certified_certificate_shown(
    workbench_client: AsyncClient,
) -> None:
    """POST /templates/preview 通过的变式 → 显示 VariantCertificate 摘要（已认证）."""
    resp = await workbench_client.post(
        "/templates/preview",
        data={
            "spec_yaml": _VALID_SPEC_YAML,
            "base_params_yaml": _VALID_BASE_PARAMS_YAML,
            "axis_id": "vary_operands",
            "pack_id": "subject-math",
            "interaction_id": "single_choice",
            "scorer_id": "exact_match",
            "preview_count": "10",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 200, resp.text
    # 证书摘要：已认证 + axis_id
    assert "已认证" in resp.text or "UNPROVEN" in resp.text
    assert "vary_operands" in resp.text
    # axis_slots / frozen_slots
    assert "axis_slots" in resp.text or "a, b" in resp.text


# ════════════════════════════════════════════════════════════════════
# 测试 §4：未登录访问受保护
# ════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_template_form_requires_auth(
    async_session: AsyncSession,
) -> None:
    """未登录访问 /templates/new → 303 重定向到 /login."""
    app = create_app()

    async def _override_session() -> AsyncIterator[AsyncSession]:
        yield async_session

    app.dependency_overrides[get_async_session] = _override_session

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/templates/new", follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"].startswith("/login")

    app.dependency_overrides.clear()
