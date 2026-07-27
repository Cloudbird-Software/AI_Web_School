"""T-W2-036 品牌模板 v1 单元测试.

覆盖验收标准：
1. default.css 定义 A4 尺寸、字体、题号样式、选项排列、填空下划线
2. page.html 是 Jinja2 模板，含 {{ paper_code }} / {{ qr_svg }} / {{ items_html }} 插槽
3. 模板可通过 HTML 渲染器填充并生成有效 HTML
"""
from __future__ import annotations

from pathlib import Path

import pytest
from jinja2 import Environment, FileSystemLoader, select_autoescape

from src.core.render.html_renderer import render_item
from src.core.render.ir import (
    ChoiceBlock,
    FillBlock,
    OptionItem,
    RenderIR,
    TextBlock,
)


BRAND_DIR = Path(__file__).resolve().parent.parent.parent / "src" / "core" / "render" / "brand"


# ════════════════════════════════════════════════════════════════════
# 1. default.css 内容校验（验收标准 #1）
# ════════════════════════════════════════════════════════════════════

class TestDefaultCSS:
    def test_css_file_exists(self):
        assert (BRAND_DIR / "default.css").is_file()

    def test_css_defines_a4_page(self):
        css = (BRAND_DIR / "default.css").read_text(encoding="utf-8")
        assert "A4" in css
        assert "@page" in css

    def test_css_defines_fonts(self):
        css = (BRAND_DIR / "default.css").read_text(encoding="utf-8")
        assert "font-family" in css

    def test_css_defines_item_number_style(self):
        css = (BRAND_DIR / "default.css").read_text(encoding="utf-8")
        assert ".item-number" in css

    def test_css_defines_options_layout(self):
        css = (BRAND_DIR / "default.css").read_text(encoding="utf-8")
        assert "ul.options" in css
        # 选项纵向排列 / 多列支持
        assert "list-style" in css or "column-count" in css

    def test_css_defines_blank_underline(self):
        css = (BRAND_DIR / "default.css").read_text(encoding="utf-8")
        assert ".blank" in css
        assert "border-bottom" in css

    def test_css_defines_page_break(self):
        """版式提示：分页控制."""
        css = (BRAND_DIR / "default.css").read_text(encoding="utf-8")
        assert "page-break" in css


# ════════════════════════════════════════════════════════════════════
# 2. page.html 模板插槽（验收标准 #2）
# ════════════════════════════════════════════════════════════════════

class TestPageTemplate:
    def test_template_file_exists(self):
        assert (BRAND_DIR / "page.html").is_file()

    def test_template_has_paper_code_slot(self):
        tpl = (BRAND_DIR / "page.html").read_text(encoding="utf-8")
        assert "{{ paper_code" in tpl

    def test_template_has_qr_svg_slot(self):
        tpl = (BRAND_DIR / "page.html").read_text(encoding="utf-8")
        assert "{{ qr_svg" in tpl

    def test_template_has_items_html_slot(self):
        tpl = (BRAND_DIR / "page.html").read_text(encoding="utf-8")
        assert "{{ items_html" in tpl

    def test_template_has_paper_title_slot(self):
        tpl = (BRAND_DIR / "page.html").read_text(encoding="utf-8")
        assert "{{ paper_title" in tpl

    def test_template_has_css_text_slot(self):
        tpl = (BRAND_DIR / "page.html").read_text(encoding="utf-8")
        assert "{{ css_text" in tpl


# ════════════════════════════════════════════════════════════════════
# 3. 模板填充生成有效 HTML（验收标准 #3）
# ════════════════════════════════════════════════════════════════════

class TestTemplateRendering:
    """模板可通过 HTML 渲染器填充并生成有效 HTML."""

    def _render_page(
        self,
        *,
        paper_title: str = "三年级数学周练",
        paper_code: str = "P-01H3K7X9-3",
        qr_svg: str = '<svg class="qr"></svg>',
        items: list[RenderIR] | None = None,
    ) -> str:
        """加载 page.html + default.css，用 Jinja2 渲染整页."""
        env = Environment(
            loader=FileSystemLoader(str(BRAND_DIR)),
            autoescape=select_autoescape(["html"]),
        )
        tpl = env.get_template("page.html")
        css_text = (BRAND_DIR / "default.css").read_text(encoding="utf-8")
        items_html = (
            "".join(render_item(ir) for ir in items) if items else ""
        )
        return tpl.render(
            paper_title=paper_title,
            paper_code=paper_code,
            qr_svg=qr_svg,
            items_html=items_html,
            css_text=css_text,
        )

    def test_page_renders_with_all_slots(self):
        html = self._render_page()
        assert "<!DOCTYPE html>" in html
        assert "三年级数学周练" in html
        assert "P-01H3K7X9-3" in html
        assert '<svg class="qr"></svg>' in html
        assert "@page" in html  # CSS 嵌入

    def test_page_with_rendered_items(self):
        items = [
            RenderIR(
                item_version_id="iv-1",
                item_id="item-1",
                interaction_id="single_choice",
                item_number="1",
                blocks=[
                    TextBlock(value="1 + 1 = ?"),
                    ChoiceBlock(
                        mode="single",
                        options=[
                            OptionItem(id="A", label="1"),
                            OptionItem(id="B", label="2"),
                        ],
                    ),
                ],
            ),
            RenderIR(
                item_version_id="iv-2",
                item_id="item-2",
                interaction_id="text_blank",
                item_number="2",
                blocks=[
                    TextBlock(value="首都是"),
                    FillBlock(blank_id="b1", kind="text"),
                ],
            ),
        ]
        html = self._render_page(items=items)
        # 题号、题面、选项、填空均出现在页面
        assert '<div class="item-number">1.</div>' in html
        assert "1 + 1 = ?" in html
        assert '<span class="option-label">A</span>' in html
        assert '<div class="item-number">2.</div>' in html
        assert "首都是" in html
        assert 'data-blank-id="b1"' in html

    def test_page_without_qr(self):
        """qr_svg 为空时不渲染 QR 区块（HTML 结构中无 paper-qr div）."""
        html = self._render_page(qr_svg="")
        assert '<div class="paper-qr">' not in html

    def test_page_html_well_formed(self):
        """产出 HTML 包含完整 html/head/body 结构."""
        html = self._render_page()
        assert html.count("<html") == 1
        assert html.count("</html>") == 1
        assert html.count("<head>") == 1
        assert html.count("</head>") == 1
        assert html.count("<body>") == 1
        assert html.count("</body>") == 1
