"""T-W2-041 题库只读列表/详情页.

对接 src/api/deps.get_async_session 复用同一 DB session 工厂；
不直接读库以外的服务，仅做 SELECT 渲染。

路由：
- GET /items：题库列表（支持 pack_id 过滤）
- GET /items/{item_id}：题库详情（item 身份 + current_version + 谱系 + 门状态）

宪法 D1：仅 SELECT；宪法 A5/X6：不 import 学科包。
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.templating import Jinja2Templates

from src.api.deps import get_async_session
from src.core.models.item import Item
from src.core.models.item_version import ItemVersion
from src.workbench.auth import require_session

router = APIRouter(prefix="", tags=["workbench-items"])


def _get_templates(request: Request) -> Jinja2Templates:
    """从 app.state 取 templates（main.py 初始化时挂载）.

    为什么走 app.state：Jinja2Templates 实例化时绑定 directory，
    在测试 ASGI 启动时路径可能不同；统一从 app.state 取避免路径硬编码。
    """
    return request.app.state.templates


# ────────────────────────────────────────────────────────────────────
# 列表页
# ────────────────────────────────────────────────────────────────────


@router.get("/items", response_class=HTMLResponse, summary="题库列表页")
async def list_items(
    request: Request,
    pack_id: Optional[str] = Query(default=None, description="按学科包过滤"),
    session: AsyncSession = Depends(get_async_session),
    _token: str = Depends(require_session),
) -> HTMLResponse:
    """展示 item_id/pack_id/tier/status 列表，支持 ?pack_id= 过滤.

    验收 §2：题库列表页展示 item_id/title/status/pack_id，支持按 pack_id 过滤。
    item 表无 title 列（题面在 item_version.content.blocks 内），故列表页
    展示 item_id 截断作为标识 + pack_id + tier + 当前版本 status；
    若 item 有 current_version_id 则联表取 status，否则 status 显示 'no_version'。
    """
    stmt = select(Item)
    if pack_id:
        stmt = stmt.where(Item.pack_id == pack_id)
    stmt = stmt.order_by(Item.created_at.desc()).limit(100)
    items = (await session.execute(stmt)).scalars().all()

    # 联表取 current_version status
    rows: list[dict[str, Any]] = []
    for it in items:
        status_str = "no_version"
        current_version_id = it.current_version_id
        if current_version_id is not None:
            ver = await session.get(ItemVersion, current_version_id)
            if ver is not None:
                status_str = ver.status
        rows.append(
            {
                "item_id": it.item_id,
                "pack_id": it.pack_id,
                "tier": it.tier,
                "current_version_id": current_version_id,
                "status": status_str,
                "created_at": it.created_at,
            }
        )

    tmpl = _get_templates(request)
    return tmpl.TemplateResponse(
        request=request,
        name="items/list.html",
        context={
            "items": rows,
            "pack_id_filter": pack_id or "",
            "packs": _PACK_OPTIONS,
        },
    )


# 学科包选项（W2 写死；后续波次从 SubjectPack 注册表读取）
_PACK_OPTIONS: list[str] = ["subject-math", "subject-chinese", "subject-english"]


# ────────────────────────────────────────────────────────────────────
# 详情页
# ────────────────────────────────────────────────────────────────────


@router.get(
    "/items/{item_id}",
    response_class=HTMLResponse,
    summary="题库详情页",
)
async def item_detail(
    request: Request,
    item_id: str,
    session: AsyncSession = Depends(get_async_session),
    _token: str = Depends(require_session),
) -> HTMLResponse:
    """展示 item 身份 + current_version 的 objective/content/lineage/gate_certificate.

    验收 §3：详情页展示 ItemVersion 的 objective/content/lineage/gate_certificate。
    若 item 无 current_version，展示 'no_version' 占位（允许 item 仅 draft 状态）。
    """
    item = await session.get(Item, item_id)
    if item is None:
        tmpl = _get_templates(request)
        return tmpl.TemplateResponse(
            request=request,
            name="error.html",
            context={
                "code": 404,
                "message": f"item_id={item_id} 不存在",
            },
            status_code=404,
        )

    current_version: Optional[ItemVersion] = None
    if item.current_version_id is not None:
        current_version = await session.get(ItemVersion, item.current_version_id)

    # 取该 item 的全部版本（按创建时间倒序）
    stmt = (
        select(ItemVersion)
        .where(ItemVersion.item_id == item_id)
        .order_by(ItemVersion.created_at.desc())
    )
    versions = (await session.execute(stmt)).scalars().all()

    tmpl = _get_templates(request)
    return tmpl.TemplateResponse(
        request=request,
        name="items/detail.html",
        context={
            "item": item,
            "current_version": current_version,
            "versions": versions,
        },
    )


__all__ = ["router"]
