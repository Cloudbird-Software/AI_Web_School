"""T-W2-042 母题表单 + 按轴抽样预览.

路由：
- GET /templates/new：渲染母题表单（YAML textarea + 元信息字段 + 预览参数）
- POST /templates/save：解析 YAML → Linter 校验 → 写 item_template +
  item_template_version（status=draft）；Linter 失败回显字段错误
- POST /templates/preview：解析 YAML → 调用 generate_variants(n=20) →
  渲染预览网格（题面 + 答案 + 选项）

W2 任务卡 non_goals：完整 DSL 编辑器、AI 辅助出题、版本 diff 不做。
表单采用 YAML textarea（教研可读，非程序员填字段→YAML 转换由前端模板预填示例引导）；
"完整 DSL 编辑器"（结构化字段表单 + 实时 Linter 提示）留后续波次。

宪法 D1：item_template_version 只增不改，status=draft 草稿多版本不删除；
宪法 A5/X6：本包不 import 学科包；调用 src/core/instantiation 纯计算接口。
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Optional

import yaml
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.templating import Jinja2Templates

from src.api.deps import get_async_session
from src.core.instantiation.dsl.linter import LintResult, lint
from src.core.instantiation.variation.engine import generate_variants
from src.core.models.item_template import ItemTemplate
from src.core.models.item_template_version import ItemTemplateVersion
from src.workbench.auth import require_session
from src.workbench.components.preview_grid import (
    certificate_summary,
    render_variant_cards,
)

router = APIRouter(prefix="", tags=["workbench-template-form"])


def _get_templates(request: Request) -> Jinja2Templates:
    """从 app.state 取 templates（main.py 初始化时挂载）."""
    return request.app.state.templates


# ────────────────────────────────────────────────────────────────────
# 预填示例（教研打开表单即见可运行样例）
# ────────────────────────────────────────────────────────────────────
# 为什么预填可运行样例：教研不写 YAML 原文，但需看到"填好的样子"模仿修改；
# 样例取自 tests/golden/instantiation/sample_single_choice.yaml 的整数加法母题。

_SAMPLE_SPEC_YAML: str = """\
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

_SAMPLE_BASE_PARAMS_YAML: str = """\
a: 3
b: 4
"""

# W2 占位 pack_digest：真实 pack_digest 应由 SubjectPack 注册表提供（D4）。
# 此处用 sha256(pack_id) 占位，让公式一可计算；后续波次接入 SubjectPack 注册表后替换。


def _compute_pack_digest(pack_id: str) -> str:
    """W2 占位：用 sha256(pack_id) 作为 pack_digest.

    真实 pack_digest 应来自 SubjectPack 注册表（包含包代码摘要 + 版本）；
    W2 未接入注册表，用 pack_id 哈希占位让公式一可运行。
    """
    return "sha256:" + hashlib.sha256(pack_id.encode("utf-8")).hexdigest()


def _compute_template_version_id(spec: dict) -> str:
    """计算 template_version_id = sha256(canonical_json(spec)).

    契约 §2.3：template_version_id 是 spec 的内容寻址哈希（D3 可复现）。
    """
    canonical = json.dumps(
        spec, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ────────────────────────────────────────────────────────────────────
# GET /templates/new：渲染表单
# ────────────────────────────────────────────────────────────────────


@router.get("/templates/new", response_class=HTMLResponse, summary="母题表单页")
async def template_form_new(
    request: Request,
    _token: str = Depends(require_session),
) -> HTMLResponse:
    """渲染母题表单，预填可运行样例."""
    tmpl = _get_templates(request)
    return tmpl.TemplateResponse(
        request=request,
        name="template_form.html",
        context={
            "spec_yaml": _SAMPLE_SPEC_YAML,
            "base_params_yaml": _SAMPLE_BASE_PARAMS_YAML,
            "template_id": "",
            "pack_id": "subject-math",
            "dsl_version": "1",
            "axis_id": "vary_operands",
            "interaction_id": "single_choice",
            "scorer_id": "exact_match",
            "preview_count": 20,
            "errors": None,
            "saved": None,
            "preview_cards": None,
            "cert_summary": None,
        },
    )


# ────────────────────────────────────────────────────────────────────
# POST /templates/save：保存草稿到 item_template_version
# ────────────────────────────────────────────────────────────────────


@router.post("/templates/save", summary="保存母题草稿")
async def template_save(
    request: Request,
    template_id: str = Form(...),
    pack_id: str = Form(...),
    dsl_version: str = Form("1"),
    spec_yaml: str = Form(...),
    session: AsyncSession = Depends(get_async_session),
    _token: str = Depends(require_session),
) -> HTMLResponse:
    """解析 spec YAML → Linter 校验 → 写 item_template + item_template_version(draft).

    验收 §1：表单页可保存母题草稿到 item_template_version（status=draft）.
    验收 §2：保存时调用 Linter，错误回显到字段.
    """
    # 解析 YAML
    parse_error: Optional[str] = None
    spec: Optional[dict] = None
    try:
        spec = yaml.safe_load(spec_yaml)
    except yaml.YAMLError as e:
        parse_error = f"YAML 解析失败：{e}"

    # Linter 校验
    lint_result: Optional[LintResult] = None
    if spec is not None:
        lint_result = lint(spec)

    errors: list[dict[str, str]] = []
    if parse_error:
        errors.append({"code": "yaml_parse_error", "path": "spec", "message": parse_error})
    if lint_result is not None and not lint_result.valid:
        for err in lint_result.errors:
            errors.append({"code": err.code, "path": err.path, "message": err.message})

    tmpl = _get_templates(request)
    if errors:
        # 校验失败：回显字段错误，不写库
        return tmpl.TemplateResponse(
            request=request,
            name="template_form.html",
            context={
                "spec_yaml": spec_yaml,
                "base_params_yaml": _SAMPLE_BASE_PARAMS_YAML,
                "template_id": template_id,
                "pack_id": pack_id,
                "dsl_version": dsl_version,
                "axis_id": "vary_operands",
                "interaction_id": "single_choice",
                "scorer_id": "exact_match",
                "preview_count": 20,
                "errors": errors,
                "saved": None,
                "preview_cards": None,
                "cert_summary": None,
            },
        )

    # 校验通过：写库
    assert spec is not None  # 上面 if spec is not None 保证
    if not template_id:
        # 未提供 template_id：用 pack_id + 短哈希生成
        template_id = f"tpl-{pack_id}-{spec.get('objective', {}).get('kp_set', [{}])[0].get('code', 'unknown')[:12]}"

    # 计算 template_version_id（内容寻址）
    template_version_id = _compute_template_version_id(spec)

    # 检查 template 是否已存在（已存在则复用，不重复 INSERT）
    existing_tmpl = await session.get(ItemTemplate, template_id)
    if existing_tmpl is None:
        session.add(
            ItemTemplate(
                template_id=template_id,
                pack_id=pack_id,
                current_version_id=None,  # draft 不前移 current_version_id
            )
        )
        await session.flush()  # 让 item_template 先落库满足 FK

    # 检查 template_version 是否已存在（同 spec 重复保存幂等）
    existing_ver = await session.get(ItemTemplateVersion, template_version_id)
    if existing_ver is None:
        session.add(
            ItemTemplateVersion(
                template_version_id=template_version_id,
                template_id=template_id,
                dsl_version=dsl_version,
                spec=spec,
                status="draft",
            )
        )
    await session.commit()

    return tmpl.TemplateResponse(
        request=request,
        name="template_form.html",
        context={
            "spec_yaml": spec_yaml,
            "base_params_yaml": _SAMPLE_BASE_PARAMS_YAML,
            "template_id": template_id,
            "pack_id": pack_id,
            "dsl_version": dsl_version,
            "axis_id": "vary_operands",
            "interaction_id": "single_choice",
            "scorer_id": "exact_match",
            "preview_count": 20,
            "errors": None,
            "saved": {
                "template_id": template_id,
                "template_version_id": template_version_id,
            },
            "preview_cards": None,
            "cert_summary": None,
        },
    )


# ────────────────────────────────────────────────────────────────────
# POST /templates/preview：按轴抽样 20 例
# ────────────────────────────────────────────────────────────────────


@router.post("/templates/preview", summary="按轴抽样预览 20 例")
async def template_preview(
    request: Request,
    spec_yaml: str = Form(...),
    base_params_yaml: str = Form(...),
    axis_id: str = Form(...),
    pack_id: str = Form("subject-math"),
    interaction_id: str = Form("single_choice"),
    scorer_id: str = Form("exact_match"),
    preview_count: int = Form(20),
    _token: str = Depends(require_session),
) -> HTMLResponse:
    """解析 spec YAML → 调用 generate_variants(n=preview_count) → 渲染预览网格.

    验收 §3：预览按钮按所选轴生成 20 个实例，网格展示题面与答案.
    """
    # 解析 YAML
    errors: list[dict[str, str]] = []
    spec: Optional[dict] = None
    try:
        spec = yaml.safe_load(spec_yaml)
    except yaml.YAMLError as e:
        errors.append({"code": "yaml_parse_error", "path": "spec", "message": f"YAML 解析失败：{e}"})

    base_params: Optional[dict] = None
    try:
        base_params = yaml.safe_load(base_params_yaml)
    except yaml.YAMLError as e:
        errors.append({"code": "yaml_parse_error", "path": "base_params", "message": f"YAML 解析失败：{e}"})

    # Linter 校验
    if spec is not None:
        lint_result = lint(spec)
        if not lint_result.valid:
            for err in lint_result.errors:
                errors.append({"code": err.code, "path": err.path, "message": err.message})

    tmpl = _get_templates(request)
    if errors or spec is None or base_params is None:
        return tmpl.TemplateResponse(
            request=request,
            name="template_form.html",
            context={
                "spec_yaml": spec_yaml,
                "base_params_yaml": base_params_yaml,
                "template_id": "",
                "pack_id": pack_id,
                "dsl_version": "1",
                "axis_id": axis_id,
                "interaction_id": interaction_id,
                "scorer_id": scorer_id,
                "preview_count": preview_count,
                "errors": errors or [{"code": "unknown", "path": "", "message": "解析失败"}],
                "saved": None,
                "preview_cards": None,
                "cert_summary": None,
            },
        )

    # 构造 template_version dict（instantiate 不要求已落库，dict 即可）
    pack_digest = _compute_pack_digest(pack_id)
    template_version_dict = {
        "template_version_id": _compute_template_version_id(spec),
        "template_id": "preview-only",  # 预览不落库，template_id 仅占位
        "dsl_version": "1",
        "spec": spec,
    }

    # 调用变式引擎
    preview_cards: list[dict[str, Any]] = []
    cert_summary: Optional[dict[str, Any]] = None
    try:
        # scorer_params 从 base_params 推导：默认 answer = 表达式求值结果（变式引擎内部已计算）
        # 此处传空 dict，scorer_params.answer 由调用方在真实签发时填入；
        # 预览只展示题面，answer 字段从 variant.scoring_ref.scorer_params 取（instantiate 已填）
        variants, cert = generate_variants(
            template_version=template_version_dict,
            axis_id=axis_id,
            n=preview_count,
            base_params=base_params,
            pack_digest=pack_digest,
            interaction_id=interaction_id,
            scorer_id=scorer_id,
            scorer_params={},
        )
        preview_cards = render_variant_cards(variants)
        cert_summary = certificate_summary(cert)
    except Exception as e:
        errors.append(
            {"code": "instantiation_error", "path": "", "message": f"实例化失败：{e}"}
        )

    return tmpl.TemplateResponse(
        request=request,
        name="template_form.html",
        context={
            "spec_yaml": spec_yaml,
            "base_params_yaml": base_params_yaml,
            "template_id": "",
            "pack_id": pack_id,
            "dsl_version": "1",
            "axis_id": axis_id,
            "interaction_id": interaction_id,
            "scorer_id": scorer_id,
            "preview_count": preview_count,
            "errors": errors or None,
            "saved": None,
            "preview_cards": preview_cards,
            "cert_summary": cert_summary,
        },
    )


__all__ = ["router"]
