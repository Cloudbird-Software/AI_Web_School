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
    """text / stem / passage 块兼容：value 或 text 任一字段."""
    if "value" in raw:
        return TextBlock(value=str(raw["value"]))
    if "text" in raw:
        return TextBlock(value=str(raw["text"]))
    raise ValueError(f"text/stem/passage 块缺 value 或 text 字段: {raw!r}")


def _convert_fill_block(
    raw: Mapping[str, Any], interaction_id: str
) -> FillBlock:
    """填空块转换：kind 优先取 block 声明，否则由 interaction_id 推导."""
    kind = raw.get("kind") or _INTERACTION_FILL_KIND.get(interaction_id, "text")
    blank_id = str(raw.get("blank_id") or raw.get("id") or "blank_1")
    return FillBlock(
        blank_id=blank_id,
        kind=kind,
        unit=raw.get("unit"),
        width=int(raw.get("width", 0)),
    )


def _convert_choice_block(
    raw: Mapping[str, Any], interaction_id: str
) -> ChoiceBlock:
    """选择题块转换：mode 优先取 block 声明，否则由 interaction_id 推导.

    兼容两种选项结构：
    - RenderIR 原生：raw["options"] = [{id, label}, ...]
    - W0 schema 级：raw 直接 id/label 表示单选项；或 raw["choices"] = [{id, label}]
    """
    mode = raw.get("mode") or _INTERACTION_CHOICE_MODE.get(
        interaction_id, "single"
    )
    options_raw = raw.get("options")
    if options_raw is None and isinstance(raw.get("choices"), (list, tuple)):
        options_raw = list(raw["choices"])
    if options_raw is None and "id" in raw and "label" in raw:
        # 单选项展开
        options_raw = [{"id": raw.get("id"), "label": raw.get("label")}]
    if options_raw is None:
        options_raw = []
    options = [
        OptionItem(id=str(o["id"]), label=str(o["label"]))
        for o in options_raw
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
    """单 block dict → 强类型 Block（按 type 分发）.

    兼容两层 type：
    - 下层渲染类型：text / fill / choice / math_svg / group（RenderIR 原生）
    - W0 schema 级语义块类型：stem / passage / options / blank
      本函数在内存中转为等价的下层类型（不改写 caller 数据）。
    """
    raw_type = raw.get("type")
    # ── 兼容：schema 级语义块 → RenderIR 原生块 ────────────────────
    if raw_type in ("stem", "passage"):
        # stem / passage 等价于 text block（附加 source_id 若有）
        r: dict[str, Any] = dict(raw)
        r["type"] = "text"
        if raw_type == "passage":
            # passage 增加一级「阅读材料」标记，前端/样式可按 material 区分
            if "metadata" not in r or not isinstance(r["metadata"], dict):
                r["metadata"] = {}
            r["metadata"].setdefault("block_kind", raw_type)
        return _convert_text_block(r)
    if raw_type == "options":
        # options 块 → 单个 ChoiceBlock（所有 options 作为一个 choices 数组）
        choices = raw.get("choices") or []
        if not isinstance(choices, (list, tuple)) or not choices:
            raise ValueError(f"options.choices 必须是非空 list: {raw!r}")
        normalized: dict[str, Any] = {
            "type": "choice",
            "options": list(choices),  # [{id,label}, ...] → 透传给 _convert_choice_block
        }
        for k in ("mode",):
            if k in raw:
                normalized[k] = raw[k]  # type: ignore[assignment]
        return _convert_choice_block(normalized, interaction_id)
    if raw_type == "blank":
        r = dict(raw)
        r["type"] = "fill"
        return _convert_fill_block(r, interaction_id)
    # ── 原生 RenderIR 类型 ───────────────────────────────────────────
    block_type = raw_type
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
    # ── 学科语义块降级：有 text 则当 text（附加 metadata.kind 保留语义）──
    # 宪法 A5：核心域零特判学科包 → 未知 block_type 不做语义识别，
    # 只在已知的学科扩展 type 白名单（可作为文本降级展示） + 有 text 字段时才降级，
    # 其他陌生 type（如 "bogus"）继续 fail fast，避免静默吞错。
    DEGRADED_WHITELIST = {
        # 语文学科（拼音/注音/部首/田字格 等）
        "pinyin", "zhuyin", "radical", "tianzi_ge", "stroke_order",
        # 英语学科（词汇/音标/词形变换）
        "ipa", "phonetic", "word_form",
        # 数学学科（表达式/图形/数轴/表格）
        "math_expr", "figure_zh", "number_line", "tabular",
        # 通用扩展
        "rubric", "hint", "solution", "explanation", "note", "figure",
        "audio", "video", "image", "illustration",
    }
    if raw_type in DEGRADED_WHITELIST and isinstance(raw.get("text"), str):
        r = dict(raw)
        r["type"] = "text"
        if "metadata" not in r or not isinstance(r["metadata"], dict):
            r["metadata"] = {}
        r["metadata"].setdefault("block_kind", str(raw_type))
        return _convert_text_block(r)
    # value 字段只有在原生 type（text/fill/...）时生效——陌生 type 带 value 不降级
    raise ValueError(f"未知 block type: {block_type!r}（block={raw!r}）")


def item_to_ir(
    item_version: Any,
    *,
    item_number: Optional[str] = None,
    placement_token: Optional[str] = None,
    item_short_code: Optional[str] = None,
) -> RenderIR:
    """将 ItemVersion 转换为 RenderIR.

    参数:
        item_version: ItemVersion ORM 行 / ItemVersionPydantic / dict
        item_number: 卷内题号（由组卷器分配，可选）
        placement_token: 卷内位置标识（如 'q1'/'q2.sub1'，组卷器分配，可选；
            W3 遗留 S9：卷面印每题短码需要）
        item_short_code: 题短码（paper_item.item_short_code，可选；
            与 placement_token 一起印于卷面供扫码查源）

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
        placement_token=placement_token,
        item_short_code=item_short_code,
        blocks=ir_blocks,
        layout_hints=layout_hints,
    )


__all__ = ["item_to_ir"]
