"""T-W2-034 HTML 渲染器单元测试.

覆盖验收标准：
1. html_renderer.py 将 RenderIR 转为 HTML，含 item 容器/题号/选项/填空下划线
2. 数学公式块渲染为 KaTeX 兼容标记；SVG 块原样嵌入
3. 单元测试覆盖文本、选择、填空、公式、SVG 五种 block
4. HTML 输出可通过白名单校验（无 script/onclick）
"""
from __future__ import annotations

import pytest

from src.core.render.html_renderer import render_item, render_items
from src.core.render.ir import (
    ChoiceBlock,
    FillBlock,
    GroupBlock,
    LayoutHints,
    MathSvgBlock,
    OptionItem,
    RenderIR,
    TextBlock,
)


# ════════════════════════════════════════════════════════════════════
# 辅助
# ════════════════════════════════════════════════════════════════════

def _ir_with(blocks: list, *, interaction_id: str = "single_choice",
             item_number: str | None = "1") -> RenderIR:
    return RenderIR(
        item_version_id="iv-001",
        item_id="item-001",
        interaction_id=interaction_id,
        item_number=item_number,
        blocks=blocks,
    )


# ════════════════════════════════════════════════════════════════════
# 1. item 容器与题号（验收标准 #1）
# ════════════════════════════════════════════════════════════════════

class TestItemContainer:
    def test_item_container_has_data_attrs(self):
        ir = _ir_with([TextBlock(value="x")])
        html = render_item(ir)
        assert 'class="item"' in html
        assert 'data-item-version-id="iv-001"' in html
        assert 'data-item-id="item-001"' in html
        assert 'data-interaction-id="single_choice"' in html

    def test_item_number_rendered(self):
        ir = _ir_with([TextBlock(value="x")], item_number="3")
        html = render_item(ir)
        assert '<div class="item-number">3.</div>' in html

    def test_item_number_omitted_when_none(self):
        ir = _ir_with([TextBlock(value="x")], item_number=None)
        html = render_item(ir)
        assert "item-number" not in html

    def test_layout_hints_as_data_attrs(self):
        ir = _ir_with(
            [TextBlock(value="x")],
        )
        ir.layout_hints = LayoutHints(
            page_break_before=True, keep_with_next=True, preferred_columns=2
        )
        html = render_item(ir)
        assert 'data-page-break-before="true"' in html
        assert 'data-keep-with-next="true"' in html
        assert 'data-preferred-columns="2"' in html


# ════════════════════════════════════════════════════════════════════
# 2. 五种 block 覆盖（验收标准 #3）
# ════════════════════════════════════════════════════════════════════

class TestTextBlock:
    """验收标准 #3：文本 block."""

    def test_text_basic(self):
        ir = _ir_with([TextBlock(value="你好世界")])
        html = render_item(ir)
        assert '<p class="item-text">你好世界</p>' in html

    def test_text_escape_html(self):
        """用户内容含 HTML 特殊字符时转义（防 XSS）."""
        ir = _ir_with([TextBlock(value="<script>alert(1)</script>")])
        html = render_item(ir)
        assert "<script>" not in html
        assert "&lt;script&gt;" in html


class TestFormulaInText:
    """验收标准 #2/#3：公式（TextBlock 内的 KaTeX 定界符）."""

    def test_inline_formula_preserved(self):
        """行内公式 $...$ 定界符保留（KaTeX auto-render 在浏览器端处理）."""
        ir = _ir_with([TextBlock(value="面积公式 $S = \\pi r^2$ 成立")])
        html = render_item(ir)
        # 定界符 $ 保留，反斜杠转义后保留在 HTML 文本中
        assert "$" in html
        assert "\\pi" in html or r"\pi" in html

    def test_display_formula_preserved(self):
        """块级公式 $$...$$ 定界符保留."""
        ir = _ir_with([TextBlock(value="$$E = mc^2$$")])
        html = render_item(ir)
        assert "$$" in html

    def test_formula_with_lt_gt_escaped(self):
        """公式中的 < > 仍被转义（防 XSS），KaTeX 接受 &lt; &gt; 输入."""
        ir = _ir_with([TextBlock(value="$a < b$ 且 $b > c$")])
        html = render_item(ir)
        assert "<script>" not in html
        # < > 被转义
        assert "&lt;" in html or "&gt;" in html


class TestChoiceBlock:
    """验收标准 #3：选择 block."""

    def test_single_choice_options(self):
        ir = _ir_with([
            ChoiceBlock(
                mode="single",
                options=[
                    OptionItem(id="A", label="选项 A"),
                    OptionItem(id="B", label="选项 B"),
                ],
            )
        ], interaction_id="single_choice")
        html = render_item(ir)
        assert '<ul class="options single">' in html
        assert '<span class="option-label">A</span>' in html
        assert '<span class="option-text">选项 A</span>' in html
        assert '<span class="option-label">B</span>' in html

    def test_multi_choice_class(self):
        ir = _ir_with([
            ChoiceBlock(mode="multi", options=[OptionItem(id="A", label="x")])
        ])
        html = render_item(ir)
        assert '<ul class="options multi">' in html

    def test_choice_empty_options(self):
        ir = _ir_with([ChoiceBlock(mode="single", options=[])])
        html = render_item(ir)
        assert '<ul class="options single"></ul>' in html

    def test_choice_label_escaped(self):
        ir = _ir_with([
            ChoiceBlock(
                mode="single",
                options=[OptionItem(id="A", label="<img onerror=alert(1)>")],
            )
        ])
        html = render_item(ir)
        assert "<img" not in html
        assert "&lt;img" in html


class TestFillBlock:
    """验收标准 #3：填空 block（下划线空位）."""

    def test_text_blank(self):
        ir = _ir_with([FillBlock(blank_id="b1", kind="text")])
        html = render_item(ir)
        assert '<span class="blank"' in html
        assert 'data-blank-id="b1"' in html
        assert 'data-kind="text"' in html

    def test_numeric_blank_with_unit(self):
        ir = _ir_with([
            FillBlock(blank_id="b2", kind="numeric", unit="cm²", width=4)
        ])
        html = render_item(ir)
        assert 'data-blank-id="b2"' in html
        assert 'data-kind="numeric"' in html
        assert 'data-unit="cm²"' in html
        assert "--blank-width:4ch" in html

    def test_blank_is_void_element(self):
        """blank span 是空元素（自闭合），内部无内容."""
        ir = _ir_with([FillBlock(blank_id="b1", kind="text")])
        html = render_item(ir)
        # 匹配 <span ...></span>（空内容）
        assert '></span>' in html


class TestMathSvgBlock:
    """验收标准 #3：SVG block."""

    def test_svg_embedded(self):
        svg = '<svg xmlns="http://www.w3.org/2000/svg" width="100"><line x1="0" y1="50" x2="100" y2="50" stroke="black"/></svg>'
        ir = _ir_with([MathSvgBlock(svg=svg, caption="数轴")])
        html = render_item(ir)
        assert '<figure class="math-svg">' in html
        assert svg in html  # SVG 原样嵌入
        assert "<figcaption>数轴</figcaption>" in html

    def test_svg_without_caption(self):
        svg = '<svg></svg>'
        ir = _ir_with([MathSvgBlock(svg=svg)])
        html = render_item(ir)
        assert "<figcaption>" not in html

    def test_svg_no_caption_when_none(self):
        svg = '<svg><circle r="5"/></svg>'
        ir = _ir_with([MathSvgBlock(svg=svg, caption=None)])
        html = render_item(ir)
        assert "<figcaption>" not in html


class TestGroupBlock:
    """题组 block（嵌套子题）."""

    def test_group_with_material_and_subitems(self):
        ir = _ir_with([
            GroupBlock(
                material="共享语篇",
                items=[
                    RenderIR(
                        item_version_id="iv-sub1",
                        item_id="item-sub1",
                        interaction_id="single_choice",
                        item_number="1",
                        blocks=[
                            TextBlock(value="子题 1"),
                            ChoiceBlock(
                                mode="single",
                                options=[OptionItem(id="A", label="a")],
                            ),
                        ],
                    ),
                ],
            )
        ])
        html = render_item(ir)
        assert '<div class="group">' in html
        assert '<div class="group-material">共享语篇</div>' in html
        assert '<div class="group-items">' in html
        # 子题被渲染
        assert 'data-item-version-id="iv-sub1"' in html
        assert "子题 1" in html

    def test_group_without_material(self):
        ir = _ir_with([
            GroupBlock(material=None, items=[
                RenderIR(
                    item_version_id="iv-s",
                    item_id="item-s",
                    interaction_id="text_blank",
                    blocks=[TextBlock(value="x")],
                )
            ])
        ])
        html = render_item(ir)
        assert "group-material" not in html
        assert 'data-item-version-id="iv-s"' in html


# ════════════════════════════════════════════════════════════════════
# 3. 安全：白名单校验（验收标准 #4）
# ════════════════════════════════════════════════════════════════════

class TestSecurityWhitelist:
    """DOMPurify 风格白名单：无 script / on* 事件属性."""

    def test_no_script_tag_in_text(self):
        ir = _ir_with([TextBlock(value="<script>alert(1)</script>")])
        html = render_item(ir)
        assert "<script" not in html.lower()

    def test_no_onclick_in_text(self):
        """用户试图注入 onclick 属性，html.escape 把 " 转义为 &quot; 阻止属性注入."""
        ir = _ir_with([TextBlock(value='x" onclick="alert(1)')])  # type: ignore
        html = render_item(ir)
        # 真实的 onclick 属性（后跟未转义引号）不应存在
        assert 'onclick="' not in html.lower()
        assert "onclick='" not in html.lower()
        # " 被转义为 &quot; 证明注入失败
        assert "&quot;" in html

    def test_no_on_event_in_choice_labels(self):
        ir = _ir_with([
            ChoiceBlock(
                mode="single",
                options=[OptionItem(id='A" onmouseover="alert(1)', label="x")],  # type: ignore
            )
        ])
        html = render_item(ir)
        assert 'onmouseover="' not in html.lower()
        assert "onmouseover='" not in html.lower()

    def test_svg_with_script_rejected(self):
        svg = '<svg><script>alert(1)</script></svg>'
        ir = _ir_with([MathSvgBlock(svg=svg)])
        with pytest.raises(ValueError, match="script"):
            render_item(ir)

    def test_svg_with_onclick_rejected(self):
        svg = '<svg><rect onclick="alert(1)"/></svg>'
        ir = _ir_with([MathSvgBlock(svg=svg)])
        with pytest.raises(ValueError, match="on\\* 事件属性"):
            render_item(ir)

    def test_svg_with_javascript_href_rejected(self):
        svg = '<svg><a href="javascript:alert(1)"><text>x</text></a></svg>'
        ir = _ir_with([MathSvgBlock(svg=svg)])
        with pytest.raises(ValueError, match="javascript"):
            render_item(ir)

    def test_full_output_no_script(self):
        """组合多种 block 的完整输出无 script/on* 事件属性注入."""
        ir = _ir_with([
            TextBlock(value="题面 <script>x</script>"),
            ChoiceBlock(
                mode="single",
                options=[OptionItem(id="A", label='x" onclick="bad()')],  # type: ignore
            ),
            FillBlock(blank_id="b1", kind="text"),
        ])
        html = render_item(ir)
        low = html.lower()
        assert "<script" not in low
        # 未转义的 on* 属性（后跟真实引号）不应存在
        assert 'onclick="' not in low
        assert "onclick='" not in low
        assert 'onmouseover="' not in low


# ════════════════════════════════════════════════════════════════════
# 4. 多题渲染
# ════════════════════════════════════════════════════════════════════

class TestRenderItems:
    def test_multiple_items_concatenated(self):
        irs = [
            _ir_with([TextBlock(value="题 1")], item_number="1"),
            _ir_with([TextBlock(value="题 2")], item_number="2"),
        ]
        html = render_items(irs)
        assert html.count('class="item"') == 2
        assert "题 1" in html
        assert "题 2" in html
        assert '<div class="item-number">1.</div>' in html
        assert '<div class="item-number">2.</div>' in html
