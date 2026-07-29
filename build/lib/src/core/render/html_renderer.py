"""T-W2-034 HTML/CSS 渲染器：RenderIR → HTML 片段.

将强类型 RenderIR blocks 转为可嵌入试卷页面的 HTML 字符串：
- text → <p>（保留 $...$ / $$...$$ KaTeX 定界符，浏览器端 KaTeX auto-render）
- fill → <span class="blank"> 下划线空位
- choice → <ul class="options"> 选项列表
- math_svg → <div class="math-svg"> 原样嵌入 SVG
- group → <div class="group"> 素材 + 嵌套子题

安全（验收标准 #4）：
- 所有用户内容（文本/选项标签/图注/素材）经 html.escape 转义
- SVG 块做白名单校验：拒绝含 <script / on*= 事件属性的 SVG
- 输出无 <script 标签、无 on* 事件属性（DOMPurify 风格白名单）

为什么不引入 Jinja2 模板引擎渲染单题：item 片段结构简单且需精细控制 escape，
用 f-string + html.escape 比 Jinja2 更直白；模板引擎在 T-W2-036 页面模板才用。
"""
from __future__ import annotations

import html
import re
from pathlib import Path

from src.core.render.ir import (
    Block,
    ChoiceBlock,
    FillBlock,
    GroupBlock,
    MathSvgBlock,
    RenderIR,
    TextBlock,
)


# SVG 白名单校验：拒绝含危险构造的 SVG
# 为什么用正则而非 HTML 解析器：SVG 是 XML 子集，解析器引入复杂依赖；
# 纸卷渲染的 SVG 由学科包本地组件产出（T-W2-029），非用户自由输入，
# 此处只做兜底防线而非完整 sanitizer。
_SCRIPT_RE = re.compile(r"<\s*script", re.IGNORECASE)
_EVENT_ATTR_RE = re.compile(r"\son\w+\s*=", re.IGNORECASE)
_HREF_JS_RE = re.compile(r"href\s*=\s*['\"]?\s*javascript:", re.IGNORECASE)


def _escape(text: str) -> str:
    """HTML 转义文本内容（保留 $...$ 公式定界符）."""
    return html.escape(text, quote=True)


def _sanitize_svg(svg: str) -> str:
    """SVG 白名单校验：拒绝 script/事件属性/javascript: 链接.

    返回原 svg 字符串（若通过校验）；否则抛 ValueError。
    不做转义——SVG 是结构化 XML，转义会破坏渲染。
    """
    if _SCRIPT_RE.search(svg):
        raise ValueError("SVG 含 <script> 标签，拒绝渲染")
    if _EVENT_ATTR_RE.search(svg):
        raise ValueError("SVG 含 on* 事件属性，拒绝渲染")
    if _HREF_JS_RE.search(svg):
        raise ValueError("SVG 含 javascript: 链接，拒绝渲染")
    return svg


def _render_text(block: TextBlock) -> str:
    """文本块 → <p>（保留 KaTeX 定界符 $...$ / $$...$$）."""
    return f'<p class="item-text">{_escape(block.value)}</p>'


def _render_fill(block: FillBlock) -> str:
    """填空块 → 下划线空位 <span class="blank">.

    data-* 属性承载 blank_id/kind/unit，供前端采集与评分对齐；
    width 控制下划线字符宽度（0=默认）。
    """
    width_attr = f' style="--blank-width:{block.width}ch"' if block.width > 0 else ""
    unit_attr = f' data-unit="{_escape(block.unit)}"' if block.unit else ""
    return (
        f'<span class="blank" data-blank-id="{_escape(block.blank_id)}" '
        f'data-kind="{_escape(block.kind)}"{unit_attr}{width_attr}></span>'
    )


def _render_choice(block: ChoiceBlock) -> str:
    """选择题块 → <ul class="options"> 选项列表.

    mode 标注在 class 上（single/multi），CSS 控制圈选/勾选样式；
    选项标签（A/B/C/D）与文本均转义。
    """
    mode_class = "options single" if block.mode == "single" else "options multi"
    if not block.options:
        return f'<ul class="{mode_class}"></ul>'
    items = []
    for opt in block.options:
        items.append(
            f'<li><span class="option-label">{_escape(opt.id)}</span>'
            f'<span class="option-text">{_escape(opt.label)}</span></li>'
        )
    return f'<ul class="{mode_class}">{"".join(items)}</ul>'


def _render_math_svg(block: MathSvgBlock) -> str:
    """数学 SVG 块 → <div class="math-svg"> 原样嵌入."""
    svg = _sanitize_svg(block.svg)
    caption = (
        f'<figcaption>{_escape(block.caption)}</figcaption>'
        if block.caption
        else ""
    )
    return f'<figure class="math-svg">{svg}{caption}</figure>'


def _render_group(block: GroupBlock) -> str:
    """题组块 → <div class="group"> 素材 + 嵌套子题."""
    material = (
        f'<div class="group-material">{_escape(block.material)}</div>'
        if block.material
        else ""
    )
    sub_items = "".join(render_item(ir) for ir in block.items)
    return f'<div class="group">{material}<div class="group-items">{sub_items}</div></div>'


def _render_block(block: Block) -> str:
    """单 block → HTML（按类型分发）."""
    if isinstance(block, TextBlock):
        return _render_text(block)
    if isinstance(block, FillBlock):
        return _render_fill(block)
    if isinstance(block, ChoiceBlock):
        return _render_choice(block)
    if isinstance(block, MathSvgBlock):
        return _render_math_svg(block)
    if isinstance(block, GroupBlock):
        return _render_group(block)
    # 理论不可达：Block 是封闭 union
    raise ValueError(f"未知 block 类型: {type(block).__name__}")


def render_item(ir: RenderIR) -> str:
    """渲染单题为 HTML 片段（不含外层页面模板）.

    输出结构：
        <div class="item" data-item-version-id="..." data-interaction-id="...">
          <div class="item-number">题号.</div>
          <div class="item-body">blocks...</div>
          <div class="item-trace">q1 · 短码</div>   <!-- 仅组卷上下文提供时 -->
        </div>

    item_number 为 None 时不渲染题号行。
    placement_token / item_short_code 均为 None 时不渲染追溯行（W3 遗留 S9：
    卷面印每题短码；单题渲染无卷上下文时保持原输出不变）。
    """
    number_html = (
        f'<div class="item-number">{_escape(ir.item_number)}.</div>'
        if ir.item_number
        else ""
    )
    body = "".join(_render_block(b) for b in ir.blocks)
    # 追溯行：卷内位置标识 + 题短码（学生/家长扫码查源，T-W2-037 回溯链入口）
    trace_html = _render_trace(ir)
    # layout_hints 作为 data 属性透传，CSS/JS 可据此控制分页
    hints = ir.layout_hints
    hints_attr = (
        f' data-page-break-before="{"true" if hints.page_break_before else "false"}"'
        f' data-keep-with-next="{"true" if hints.keep_with_next else "false"}"'
        f' data-preferred-columns="{hints.preferred_columns}"'
    )
    return (
        f'<div class="item" data-item-version-id="{_escape(ir.item_version_id)}" '
        f'data-item-id="{_escape(ir.item_id)}" '
        f'data-interaction-id="{_escape(ir.interaction_id)}"{hints_attr}>'
        f'{number_html}<div class="item-body">{body}</div>{trace_html}</div>'
    )


def _render_trace(ir: RenderIR) -> str:
    """渲染卷面追溯行（placement_token + item_short_code）.

    两者都缺省时返回空串（不改变既有输出）；
    只提供其一时只渲染提供的部分（短码是查源主键，优先展示）。
    """
    if not ir.placement_token and not ir.item_short_code:
        return ""
    parts: list[str] = []
    if ir.placement_token:
        parts.append(
            f'<span class="placement-token">{_escape(ir.placement_token)}</span>'
        )
    if ir.item_short_code:
        parts.append(
            f'<span class="item-short-code">{_escape(ir.item_short_code)}</span>'
        )
    return f'<div class="item-trace">{"".join(parts)}</div>'


def render_items(irs: list[RenderIR]) -> str:
    """渲染多题为 HTML 片段序列（供页面模板填充 items_html 插槽）."""
    return "".join(render_item(ir) for ir in irs)


# ── 模板加载（T-W2-036 页面模板用，此处先占位） ──────────────────────

_TEMPLATES_DIR = Path(__file__).parent / "templates"


def get_template(name: str) -> str:
    """读取 templates/ 下的模板文件内容（供页面级渲染用）.

    为什么放这里：html_renderer 是渲染域入口，模板加载逻辑集中在此；
    T-W2-036 的 page.html 通过本函数加载。
    """
    path = _TEMPLATES_DIR / name
    return path.read_text(encoding="utf-8")


__all__ = ["render_item", "render_items", "get_template"]
