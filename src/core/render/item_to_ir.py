"""T-W2-033 ItemVersion → RenderIR 映射.

将 ItemVersion 的六大块中的 content.blocks（permissive dict）转换为强类型
RenderIR blocks，保留题号、选项、填空位置、题组嵌套。

输入兼容三种形态：
- ItemVersion ORM 行（取 .content / .interaction_ref 等属性）
- ItemVersionPydantic 实例
- dict（与 ORM/Pydantic 序列化形态一致）

为什么不依赖 ItemVersion ORM 类型作为入参：批处理（T-W2-038）从 serving
视图读出的是 dict，不构造 ORM；让转换器吃 dict/Pydantic/ORM 三态，避免
批处理为凑类型而多构造一层 ORM。
"""
from __future__ import annotations

from typing import Any, Mapping, Optional

from src.core.render.ir import (
    Block,
    ChoiceBlock,
    FillBlock,
    GroupBlock,
    LayoutHints,
    MathSvgBlock,
    OptionItem,
    RenderIR,
    TextBlock,
)


# interaction_id → 默认 choice mode / fill kind 映射
# 为什么需要：content.blocks 可能省略 mode/kind，由 interaction_id 推导
_INTERACTION_CHOICE_MODE = {
    "single_choice": "single",
    "multi_choice": "multi",
}

_INTERACTION_FILL_KIND = {
    "text_blank": "text",
    "numeric_blank": "numeric",
}


def _get(obj: Any, key: str, default: Any = None) -> Any:
    """从 dict 或对象取属性（兼容 ORM/Pydantic/dict 三态）."""
    if isinstance(obj, Mapping):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _convert_text_block(raw: Mapping[str, Any]) -> TextBlock:
    return TextBlock(value=str(raw["value"]))


def _convert_fill_block(
    raw: Mapping[str, Any], interaction_id: str
) -> FillBlock:
    """填空块转换：kind 优先取 block 声明，否则由 interaction_id 推导."""
    kind = raw.get("kind") or _INTERACTION_FILL_KIND.get(interaction_id, "text")
    return FillBlock(
        blank_id=str(raw["blank_id"]),
        kind=kind,
        unit=raw.get("unit"),
        width=int(raw.get("width", 0)),
    )


def _convert_choice_block(
    raw: Mapping[str, Any], interaction_id: str
) -> ChoiceBlock:
    """选择题块转换：mode 优先取 block 声明，否则由 interaction_id 推导."""
    mode = raw.get("mode") or _INTERACTION_CHOICE_MODE.get(
        interaction_id, "single"
    )
    options = [
        OptionItem(id=str(o["id"]), label=str(o["label"]))
        for o in raw.get("options", [])
    ]
    return ChoiceBlock(mode=mode, options=options)


def _convert_math_svg_block(raw: Mapping[str, Any]) -> MathSvgBlock:
    return MathSvgBlock(
        svg=str(raw["svg"]),
        caption=raw.get("caption"),
    )


def _convert_group_block(
    raw: Mapping[str, Any], interaction_id: str
) -> GroupBlock:
    """题组块转换：递归转换子题.

    子题允许两种形态：
    - 完整 ItemVersion dict（含 content.blocks + interaction_ref）：走 item_to_ir
    - 已是 IR dict（顶层有 blocks + interaction_id）：直接 model_validate
    其他形态 → ValueError（fail fast，避免静默丢题）.
    """
    material = raw.get("material")
    raw_items = raw.get("items", [])
    sub_items: list[RenderIR] = []
    for sub in raw_items:
        if not isinstance(sub, Mapping):
            raise ValueError(f"题组子题必须是 dict，得到 {type(sub).__name__}")
        # ItemVersion 形态：有 content 键（blocks 嵌套在 content 下）
        if "content" in sub or "interaction_ref" in sub:
            sub_items.append(item_to_ir(sub))
        # 已是 IR 形态：顶层有 blocks + interaction_id
        elif "blocks" in sub and "interaction_id" in sub:
            sub_items.append(RenderIR.model_validate(sub))
        else:
            raise ValueError(
                "题组子题缺少必要字段（需 content/interaction_ref 或 blocks/interaction_id）: "
                f"{sub!r}"
            )
    return GroupBlock(material=material, items=sub_items)


def _convert_block(
    raw: Mapping[str, Any], interaction_id: str
) -> Block:
    """单 block dict → 强类型 Block（按 type 分发）."""
    block_type = raw.get("type")
    if block_type == "text":
        return _convert_text_block(raw)
    if block_type == "fill":
        return _convert_fill_block(raw, interaction_id)
    if block_type == "choice":
        return _convert_choice_block(raw, interaction_id)
    if block_type == "math_svg":
        return _convert_math_svg_block(raw)
    if block_type == "group":
        return _convert_group_block(raw, interaction_id)
    raise ValueError(f"未知 block type: {block_type!r}（block={raw!r}）")


def item_to_ir(
    item_version: Any,
    *,
    item_number: Optional[str] = None,
) -> RenderIR:
    """将 ItemVersion 转换为 RenderIR.

    参数:
        item_version: ItemVersion ORM 行 / ItemVersionPydantic / dict
        item_number: 卷内题号（由组卷器分配，可选）

    返回:
        RenderIR 实例

    异常:
        ValueError: content.blocks 含未知 type 或缺必要字段
        KeyError: interaction_ref 缺 interaction_id
    """
    interaction_ref = _get(item_version, "interaction_ref", {}) or {}
    interaction_id = interaction_ref.get("interaction_id") or (
        _get(item_version, "interaction_id", None) or ""
    )
    if not interaction_id:
        raise ValueError("ItemVersion 缺 interaction_ref.interaction_id")

    content = _get(item_version, "content", {}) or {}
    raw_blocks = content.get("blocks", []) if isinstance(content, Mapping) else []

    ir_blocks: list[Block] = [
        _convert_block(rb, interaction_id) for rb in raw_blocks
    ]

    # layout_hints 可选：content.layout_hints 或缺省
    layout_raw = (
        content.get("layout_hints") if isinstance(content, Mapping) else None
    )
    if isinstance(layout_raw, Mapping):
        layout_hints = LayoutHints(
            page_break_before=bool(layout_raw.get("page_break_before", False)),
            keep_with_next=bool(layout_raw.get("keep_with_next", False)),
            preferred_columns=int(layout_raw.get("preferred_columns", 1)),
        )
    else:
        layout_hints = LayoutHints()

    item_version_id = _get(item_version, "item_version_id", "") or ""
    item_id = _get(item_version, "item_id", "") or ""

    return RenderIR(
        item_version_id=str(item_version_id),
        item_id=str(item_id),
        interaction_id=str(interaction_id),
        item_number=item_number,
        blocks=ir_blocks,
        layout_hints=layout_hints,
    )


__all__ = ["item_to_ir"]
