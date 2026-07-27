"""T-W2-043 签发闭环页：签发按钮 → 门编排 → 证书 → publication → published.

路由：
- GET /issue/{item_version_id}：展示待签发 item_version + 门状态摘要 + 签发按钮
- POST /issue/{item_version_id}：触发门编排（run_gate）→
    - 全部 pass：调用 issue_item_version 状态前移到 published + 写 publication
    - 任一 fail/review：渲染错误页，展示 fail 证据

W2 任务卡 non_goals：完整审批流、批量签发、AQL 抽检均不做。
W2 单用户静态 token：published_by = workbench token（即 session cookie 值）。

宪法 D1：内容六大块不改；仅状态机字段前移。
宪法 D2：published_at 非空必伴随 gate_certificate_id 非空（DB CHECK 强制）。
宪法 A5/X6：本包不 import 学科包；通过 src/core/gate 抽象接口调用平台通用验证器。
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.templating import Jinja2Templates

from src.api.deps import get_async_session
from src.core.content.publication import IssueError, issue_item_version
from src.core.gate.orchestrator.orchestrator import GateOutcome, run_gate
from src.core.gate.policy.loader import load_default_policy
from src.core.gate.validator import GateContext
from src.core.models.item import Item
from src.core.models.item_version import ItemVersion
from src.workbench.auth import require_session

router = APIRouter(prefix="", tags=["workbench-issue"])


def _get_templates(request: Request) -> Jinja2Templates:
    """从 app.state 取 templates（main.py 初始化时挂载）."""
    return request.app.state.templates


# ────────────────────────────────────────────────────────────────────
# 辅助：构造 artifact_payload（从 ItemVersion 六大块提取供验证器读）
# ────────────────────────────────────────────────────────────────────
# 为什么不直接传整个 ItemVersion ORM 对象：GateContext.artifact_payload 是 dict，
# 验证器按 dict key 访问（如 payload['objective']）；ORM 对象属性访问与 dict 不同。
# 提取六大块到 dict 与契约 §5.1 schema 校验对齐。


def _build_artifact_payload(version: ItemVersion) -> dict[str, Any]:
    """从 ItemVersion 提取六大块作为验证器载荷."""
    return {
        "objective": version.objective,
        "interaction_ref": version.interaction_ref,
        "content": version.content,
        "scoring_ref": version.scoring_ref,
        "error_bindings": version.error_bindings,
        "lineage": version.lineage,
        "item_version_id": version.item_version_id,
        "item_id": version.item_id,
        "status": version.status,
    }


# ────────────────────────────────────────────────────────────────────
# GET /issue/{item_version_id}：展示签发页
# ────────────────────────────────────────────────────────────────────


@router.get(
    "/issue/{item_version_id}",
    response_class=HTMLResponse,
    summary="签发页：展示待审 item_version + 签发按钮",
)
async def issue_page(
    request: Request,
    item_version_id: str,
    session: AsyncSession = Depends(get_async_session),
    _token: str = Depends(require_session),
) -> HTMLResponse:
    """展示待签发 item_version + 当前门状态摘要 + 签发按钮.

    验收 §1：签发页展示待审 item_version 与门状态摘要。
    门状态摘要 = item_version.gate_certificate_id（已签发证书则展示，否则提示未过门）。
    """
    version = await session.get(ItemVersion, item_version_id)
    tmpl = _get_templates(request)

    if version is None:
        return tmpl.TemplateResponse(
            request=request,
            name="error.html",
            context={
                "code": 404,
                "message": f"item_version_id={item_version_id} 不存在",
            },
            status_code=404,
        )

    item = await session.get(Item, version.item_id)

    # 状态机校验：已 published/retired 的版本不允许再签发
    can_issue = version.status in ("draft", "quarantined")

    return tmpl.TemplateResponse(
        request=request,
        name="issue.html",
        context={
            "version": version,
            "item": item,
            "can_issue": can_issue,
            "gate_certificate_id": version.gate_certificate_id,
        },
    )


# ────────────────────────────────────────────────────────────────────
# POST /issue/{item_version_id}：触发签发
# ────────────────────────────────────────────────────────────────────


@router.post(
    "/issue/{item_version_id}",
    response_class=HTMLResponse,
    summary="签发：触发门编排 + 状态前移",
)
async def issue_submit(
    request: Request,
    item_version_id: str,
    session: AsyncSession = Depends(get_async_session),
    token: str = Depends(require_session),
) -> HTMLResponse:
    """执行签发：run_gate → 若 pass 调 issue_item_version → 渲染结果页.

    验收 §2：点击签发调用门编排；全部通过后生成 GateCertificate 并更新
              item_version.status=published。
    验收 §3：失败时门状态停留在 quarantined，展示 fail 证据。

    失败处理：
    - run_gate final_verdict='fail'：渲染错误页展示 fail 验证器 + 证据。
    - run_gate final_verdict='review'：渲染错误页展示 review 验证器 + 证据
      （review 不签发证书，需人工复核）。
    - issue_item_version 抛 IssueError：渲染错误页（状态机非法）。
    """
    tmpl = _get_templates(request)
    version = await session.get(ItemVersion, item_version_id)
    if version is None:
        return tmpl.TemplateResponse(
            request=request,
            name="error.html",
            context={
                "code": 404,
                "message": f"item_version_id={item_version_id} 不存在",
            },
            status_code=404,
        )

    # 状态机校验
    if version.status not in ("draft", "quarantined"):
        return tmpl.TemplateResponse(
            request=request,
            name="error.html",
            context={
                "code": 409,
                "message": (
                    f"item_version 当前状态={version.status!r}，"
                    "仅 draft/quarantined 可签发（状态机无回边/无重签）"
                ),
            },
            status_code=409,
        )

    # 1. 构造 GateContext + 加载策略
    artifact_payload = _build_artifact_payload(version)
    pack_id = (await session.get(Item, version.item_id))
    pack_id_str = pack_id.pack_id if pack_id else "platform"

    ctx = GateContext(
        artifact_type="item",
        pack_id=pack_id_str,
        artifact_payload=artifact_payload,
        # 通用验证器需要：schema 校验必填键 / license 查 material_license 表 /
        # duplicate_placeholder 查 published 版本表——都需 db
        db=session,
        required_keys=[
            "objective",
            "interaction_ref",
            "content",
            "scoring_ref",
            "error_bindings",
            "lineage",
        ],
    )

    try:
        policy = load_default_policy()
    except Exception as e:
        return tmpl.TemplateResponse(
            request=request,
            name="error.html",
            context={
                "code": 500,
                "message": f"门策略加载失败：{e}",
            },
            status_code=500,
        )

    # 2. 运行门编排
    try:
        outcome: GateOutcome = await run_gate(
            artifact_ref=item_version_id,
            artifact_type="item",
            pack_id=pack_id_str,
            ctx=ctx,
            policy=policy,
            db=session,
            issued_by=token,  # W2 单用户：签发人 = workbench token
            cert_type="publish",
        )
    except Exception as e:
        return tmpl.TemplateResponse(
            request=request,
            name="error.html",
            context={
                "code": 500,
                "message": f"门编排执行失败：{e}",
            },
            status_code=500,
        )

    # 3. 失败/review：渲染错误页展示证据
    if outcome.final_verdict != "pass" or outcome.cert_id is None:
        return tmpl.TemplateResponse(
            request=request,
            name="issue_fail.html",
            context={
                "version": version,
                "outcome": outcome,
                "final_verdict": outcome.final_verdict,
                "runs": outcome.runs,
                "short_circuit_at": outcome.short_circuit_at,
            },
            status_code=200,  # 业务失败但 HTTP 200（页面可重试）
        )

    # 4. pass：调 issue_item_version 状态前移 + 写 publication
    try:
        result = await issue_item_version(
            item_version_id=item_version_id,
            gate_certificate_id=outcome.cert_id,
            published_by=token,
            db=session,
        )
    except IssueError as e:
        return tmpl.TemplateResponse(
            request=request,
            name="error.html",
            context={
                "code": 409,
                "message": f"签发失败：{e}",
            },
            status_code=409,
        )

    # 5. 渲染成功页
    # 重新查询 published 状态的 version（拿到 published_at / gate_certificate_id）
    published_version = await session.get(ItemVersion, item_version_id)
    return tmpl.TemplateResponse(
        request=request,
        name="issue_success.html",
        context={
            "version": published_version,
            "publication_id": result["publication_id"],
            "gate_certificate_id": outcome.cert_id,
            "outcome": outcome,
        },
    )


__all__ = ["router"]
