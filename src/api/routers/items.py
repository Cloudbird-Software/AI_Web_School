"""T-W2-039 题库只读路由：item / item_version / item_template.

三个 GET 端点：
- GET /items/{item_id}：返回 item 身份 + current_version 内容（若有）
- GET /item_versions/{item_version_id}：返回版本六大块 + 谱系
- GET /templates/{template_id}：返回母题身份 + current_version（若有）

宪法 D1：仅 SELECT；宪法 A5/X6：不 import 学科包。
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import get_async_session, require_auth
from src.core.models.item import Item, ItemPydantic
from src.core.models.item_template import ItemTemplate, ItemTemplatePydantic
from src.core.models.item_template_version import (
    ItemTemplateVersion,
    ItemTemplateVersionPydantic,
)
from src.core.models.item_version import ItemVersion, ItemVersionPydantic

router = APIRouter(prefix="", tags=["items"])


# ────────────────────────────────────────────────────────────────────
# 响应模型
# ────────────────────────────────────────────────────────────────────


class ItemDetailResponse(ItemPydantic):
    """GET /items/{item_id} 响应：item 身份 + current_version（若有）.

    current_version 为 None 表示该 item 尚无已发布版本（current_version_id IS NULL）。
    """

    current_version: Optional[ItemVersionPydantic] = None


class TemplateDetailResponse(ItemTemplatePydantic):
    """GET /templates/{template_id} 响应：母题身份 + current_version（若有）."""

    current_version: Optional[ItemTemplateVersionPydantic] = None


# ────────────────────────────────────────────────────────────────────
# 端点
# ────────────────────────────────────────────────────────────────────


@router.get(
    "/items/{item_id}",
    response_model=ItemDetailResponse,
    summary="查询 item 身份与当前版本",
    responses={404: {"description": "item_id 不存在"}},
)
async def get_item(
    item_id: str,
    session: AsyncSession = Depends(get_async_session),
    _auth: None = Depends(require_auth),
) -> ItemDetailResponse:
    """返回 item 不变身份 + current_version_id 指向的版本内容（若有）.

    current_version 为 None 表示该 item 尚未发布任何版本。
    """
    item = await session.get(Item, item_id)
    if item is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"item_id={item_id} 不存在",
        )

    current_version: Optional[ItemVersion] = None
    if item.current_version_id is not None:
        current_version = await session.get(ItemVersion, item.current_version_id)

    return ItemDetailResponse(
        item_id=item.item_id,
        pack_id=item.pack_id,
        tier=item.tier,
        template_version_id=item.template_version_id,
        current_version_id=item.current_version_id,
        created_at=item.created_at,
        current_version=(
            ItemVersionPydantic.model_validate(current_version, from_attributes=True)
            if current_version is not None
            else None
        ),
    )


@router.get(
    "/item_versions/{item_version_id}",
    response_model=ItemVersionPydantic,
    summary="查询 item_version 内容与谱系",
    responses={404: {"description": "item_version_id 不存在"}},
)
async def get_item_version(
    item_version_id: str,
    session: AsyncSession = Depends(get_async_session),
    _auth: None = Depends(require_auth),
) -> ItemVersionPydantic:
    """返回 item_version 六大块 + 谱系 + 门证书引用（若已发布）."""
    version = await session.get(ItemVersion, item_version_id)
    if version is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"item_version_id={item_version_id} 不存在",
        )
    return ItemVersionPydantic.model_validate(version, from_attributes=True)


@router.get(
    "/templates/{template_id}",
    response_model=TemplateDetailResponse,
    summary="查询母题身份与当前版本",
    responses={404: {"description": "template_id 不存在"}},
)
async def get_template(
    template_id: str,
    session: AsyncSession = Depends(get_async_session),
    _auth: None = Depends(require_auth),
) -> TemplateDetailResponse:
    """返回母题不变身份 + current_version_id 指向的母题版本（若有）."""
    template = await session.get(ItemTemplate, template_id)
    if template is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"template_id={template_id} 不存在",
        )

    current_version: Optional[ItemTemplateVersion] = None
    if template.current_version_id is not None:
        current_version = await session.get(
            ItemTemplateVersion, template.current_version_id
        )

    return TemplateDetailResponse(
        template_id=template.template_id,
        pack_id=template.pack_id,
        current_version_id=template.current_version_id,
        created_at=template.created_at,
        current_version=(
            ItemTemplateVersionPydantic.model_validate(
                current_version, from_attributes=True
            )
            if current_version is not None
            else None
        ),
    )


__all__ = ["router"]
