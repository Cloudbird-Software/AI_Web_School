"""T-W2-029 数学渲染组件单元测试.

覆盖任务卡 4 条验收标准：
1. number_line.py 生成 SVG 数轴（含刻度、标签、高亮点/区间）
2. grid.py 生成方格纸 SVG（行列配置、单元格高亮）
3. 输出可作为 math_svg 块被 HTML 渲染器消费
4. 单元测试覆盖渲染输出校验

实现策略：
- subject-math 目录带连字符，无法作为 Python 包导入，用 importlib 加载
- 同 test_math_functions.py 的模式
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

# ────────────────────────────────────────────────────────────────────
# 加载被测模块（连字符目录无法用普通 import）
# ────────────────────────────────────────────────────────────────────
_ROOT = Path(__file__).parent.parent.parent
_NL_PATH = _ROOT / "src" / "packs" / "subject-math" / "render" / "number_line.py"
_GRID_PATH = _ROOT / "src" / "packs" / "subject-math" / "render" / "grid.py"
_INIT_PATH = _ROOT / "src" / "packs" / "subject-math" / "render" / "__init__.py"


def _load_module(path: Path, name: str):
    """以 importlib 加载 .py 文件为独立模块（复用 sys.modules 实例）."""
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载 {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


number_line = _load_module(_NL_PATH, "subject_math_render_number_line")
grid = _load_module(_GRID_PATH, "subject_math_render_grid")
NumberLineSpec = number_line.NumberLineSpec
render_number_line = number_line.render_number_line
GridSpec = grid.GridSpec
render_grid = grid.render_grid


# ════════════════════════════════════════════════════════════════════
# 1. 数轴 SVG（验收标准 #1）
# ════════════════════════════════════════════════════════════════════

class TestNumberLineSvg:
    def test_basic_svg_structure(self):
        """基础结构：<svg> 根 + 主轴线 + 闭合标签."""
        svg = render_number_line(NumberLineSpec(start=0, end=10))
        assert svg.startswith("<svg ")
        assert svg.endswith("</svg>")
        assert 'xmlns="http://www.w3.org/2000/svg"' in svg
        # 主轴线存在
        assert "<line " in svg
        assert "stroke=" in svg

    def test_has_arrow_head(self):
        """右箭头（polygon）存在."""
        svg = render_number_line(NumberLineSpec(start=0, end=5))
        assert "<polygon" in svg
        assert "fill=" in svg

    def test_has_all_tick_labels(self):
        """每个刻度都有数字标签：0..5."""
        svg = render_number_line(NumberLineSpec(start=0, end=5, step=1))
        for v in range(6):
            assert f">{v}<" in svg, f"刻度 {v} 的标签缺失"

    def test_step_skips_ticks(self):
        """step=2 时只显示 0/2/4/6/8/10，不显示 1/3/5/7/9."""
        svg = render_number_line(NumberLineSpec(start=0, end=10, step=2))
        for v in [0, 2, 4, 6, 8, 10]:
            assert f">{v}<" in svg
        for v in [1, 3, 5, 7, 9]:
            assert f">{v}<" not in svg

    def test_highlight_point_renders_circle(self):
        """高亮点渲染为 circle 元素."""
        svg = render_number_line(
            NumberLineSpec(start=0, end=10, highlight_point=5)
        )
        assert "<circle " in svg
        assert "fill=" in svg

    def test_highlight_range_renders_thick_line(self):
        """高亮区间渲染为粗 line + 端点 circle."""
        svg = render_number_line(
            NumberLineSpec(start=0, end=10, highlight_range=(3, 7))
        )
        # 粗线段（stroke-width="6"）
        assert 'stroke-width="6"' in svg
        # 端点圆点（至少 2 个 circle）
        assert svg.count("<circle ") >= 2

    def test_label_renders_text(self):
        """label 显示为右上角 text."""
        svg = render_number_line(
            NumberLineSpec(start=0, end=10, label="数轴示例")
        )
        assert "数轴示例" in svg
        assert 'text-anchor="end"' in svg

    def test_label_escaped(self):
        """label 含特殊字符时转义（防 SVG 注入）."""
        svg = render_number_line(
            NumberLineSpec(start=0, end=10, label="<script>x</script>")
        )
        assert "<script>" not in svg
        assert "&lt;script&gt;" in svg

    def test_viewbox_present(self):
        """viewBox 保证缩放不变形."""
        svg = render_number_line(NumberLineSpec(start=0, end=10, width=480, height=80))
        assert 'viewBox="0 0 480 80"' in svg


# ════════════════════════════════════════════════════════════════════
# 2. 数轴 spec 校验
# ════════════════════════════════════════════════════════════════════

class TestNumberLineValidation:
    def test_end_must_be_greater_than_start(self):
        with pytest.raises(ValueError, match="end.*必须.*>.*start"):
            render_number_line(NumberLineSpec(start=5, end=5))

    def test_step_must_divide_span(self):
        with pytest.raises(ValueError, match="必须被 step"):
            render_number_line(NumberLineSpec(start=0, end=10, step=3))

    def test_step_must_be_positive(self):
        with pytest.raises(ValueError, match="step 必须 > 0"):
            render_number_line(NumberLineSpec(start=0, end=10, step=0))

    def test_highlight_point_out_of_range(self):
        with pytest.raises(ValueError, match="highlight_point.*不在"):
            render_number_line(
                NumberLineSpec(start=0, end=10, highlight_point=15)
            )

    def test_highlight_range_out_of_bounds(self):
        with pytest.raises(ValueError, match="highlight_range.*不在"):
            render_number_line(
                NumberLineSpec(start=0, end=10, highlight_range=(5, 15))
            )

    def test_highlight_range_lo_gt_hi(self):
        with pytest.raises(ValueError, match="lo.*>.*hi"):
            render_number_line(
                NumberLineSpec(start=0, end=10, highlight_range=(7, 3))
            )


# ════════════════════════════════════════════════════════════════════
# 3. 方格纸 SVG（验收标准 #2）
# ════════════════════════════════════════════════════════════════════

class TestGridSvg:
    def test_basic_svg_structure(self):
        svg = render_grid(GridSpec(rows=4, cols=6))
        assert svg.startswith("<svg ")
        assert svg.endswith("</svg>")
        assert 'xmlns="http://www.w3.org/2000/svg"' in svg

    def test_grid_lines_count(self):
        """4 行 6 列方格应有 7 条横线 + 5 条竖线（含外边框矩形）.

        横线数 = rows + 1 = 5
        竖线数 = cols + 1 = 7
        （外边框 rect 不算 <line>）
        """
        svg = render_grid(GridSpec(rows=4, cols=6))
        line_count = svg.count("<line ")
        assert line_count == (4 + 1) + (6 + 1)  # 5+7=12

    def test_outer_border_rect(self):
        """外边框 rect 存在且 fill=none."""
        svg = render_grid(GridSpec(rows=3, cols=3))
        assert "<rect " in svg
        assert 'fill="none"' in svg

    def test_highlight_cells_renders_rect(self):
        """高亮单元格渲染为半透明 rect."""
        svg = render_grid(
            GridSpec(
                rows=4, cols=4,
                highlight_cells=[(0, 0), (2, 3)],
            )
        )
        # 至少 2 个 fill 高亮 rect（加上外边框 rect 共 3 个）
        # 高亮 rect 有 opacity 属性，外边框没有
        opacity_rects = svg.count('opacity="0.35"')
        assert opacity_rects == 2

    def test_label_renders_text(self):
        svg = render_grid(GridSpec(rows=3, cols=3, label="方格示例"))
        assert "方格示例" in svg
        assert 'text-anchor="end"' in svg

    def test_label_escaped(self):
        svg = render_grid(GridSpec(rows=3, cols=3, label="<x>"))
        assert "<x>" not in svg
        assert "&lt;x&gt;" in svg

    def test_major_lines_thicker(self):
        """major_every=5 时第 5 条线加粗（stroke-width 0.8 vs 0.4）."""
        svg = render_grid(GridSpec(rows=10, cols=10, major_every=5))
        assert 'stroke-width="0.8"' in svg
        assert 'stroke-width="0.4"' in svg

    def test_dimensions_match_spec(self):
        """SVG 总尺寸 = grid_w+4 x grid_h+label_band+4."""
        spec = GridSpec(rows=4, cols=6, cell_size=20)
        svg = render_grid(spec)
        expected_w = 6 * 20 + 4
        expected_h = 4 * 20 + 16 + 4
        assert f'width="{expected_w}"' in svg
        assert f'height="{expected_h}"' in svg
        assert f'viewBox="0 0 {expected_w} {expected_h}"' in svg


# ════════════════════════════════════════════════════════════════════
# 4. 方格纸 spec 校验
# ════════════════════════════════════════════════════════════════════

class TestGridValidation:
    def test_rows_must_be_positive(self):
        with pytest.raises(ValueError, match="rows 必须"):
            render_grid(GridSpec(rows=0, cols=5))

    def test_cols_must_be_positive(self):
        with pytest.raises(ValueError, match="cols 必须"):
            render_grid(GridSpec(rows=5, cols=0))

    def test_cell_size_too_small(self):
        with pytest.raises(ValueError, match="cell_size 太小"):
            render_grid(GridSpec(rows=3, cols=3, cell_size=2))

    def test_highlight_cell_out_of_bounds(self):
        with pytest.raises(ValueError, match="越界"):
            render_grid(
                GridSpec(rows=3, cols=3, highlight_cells=[(5, 0)])
            )


# ════════════════════════════════════════════════════════════════════
# 5. 集成：作为 math_svg 块被消费（验收标准 #3）
# ════════════════════════════════════════════════════════════════════

class TestMathSvgBlockIntegration:
    """验证 SVG 输出可直接作为 RenderIR.MathSvgBlock 的 svg 字段."""

    def test_number_line_svg_consumable_by_ir(self):
        """数轴 SVG 可被 MathSvgBlock 接受（schema 不拒绝字符串）."""
        from src.core.render.ir import MathSvgBlock

        svg = render_number_line(NumberLineSpec(start=0, end=10))
        block = MathSvgBlock(svg=svg, caption="数轴示例")
        assert block.type == "math_svg"
        assert block.svg == svg
        assert block.caption == "数轴示例"

    def test_grid_svg_consumable_by_ir(self):
        """方格 SVG 可被 MathSvgBlock 接受."""
        from src.core.render.ir import MathSvgBlock

        svg = render_grid(GridSpec(rows=4, cols=4))
        block = MathSvgBlock(svg=svg)
        assert block.svg == svg

    def test_svg_well_formed_xml(self):
        """SVG 是良构的（<svg> 闭合）."""
        svg1 = render_number_line(NumberLineSpec(start=0, end=5))
        svg2 = render_grid(GridSpec(rows=3, cols=3))
        for svg in (svg1, svg2):
            assert svg.count("<svg") == 1
            assert svg.count("</svg>") == 1

    def test_html_renderer_can_render_math_svg_block_with_grid(self):
        """HTML 渲染器能消费 MathSvgBlock（含方格 SVG）并输出 <svg>."""
        from src.core.render.ir import MathSvgBlock, RenderIR
        from src.core.render.html_renderer import render_item

        svg = render_grid(GridSpec(rows=3, cols=3))
        block = MathSvgBlock(svg=svg, caption="3x3 方格")
        ir = RenderIR(
            item_version_id="iv-test-001",
            item_id="i-test-001",
            interaction_id="single_choice",
            item_number="1",
            blocks=[block],
        )
        html = render_item(ir)
        # HTML 渲染器应原样嵌入 SVG（透传，不解释）
        assert "<svg" in html
        assert "</svg>" in html
        # caption 也应出现
        assert "3x3 方格" in html


# ════════════════════════════════════════════════════════════════════
# 6. 学科零特判（反向：学科包不 import 核心域）
# ════════════════════════════════════════════════════════════════════

# 匹配真正的 import 语句（行首），避免 docstring 中提到 "import src.core" 误判
_IMPORT_CORE_RE = __import__("re").compile(
    r"(?m)^\s*(?:from\s+src\.core|import\s+src\.core)"
)


class TestNoCoreImports:
    """学科包不得反向 import src.core.* （宪法 X6）."""

    def test_number_line_no_core_imports(self):
        import inspect
        src_text = inspect.getsource(number_line)
        # 检查行首的 import/from 语句，docstring 中的 "import src.core" 不算
        assert _IMPORT_CORE_RE.search(src_text) is None, (
            f"number_line.py 反向引用核心域：\n{src_text}"
        )

    def test_grid_no_core_imports(self):
        import inspect
        src_text = inspect.getsource(grid)
        assert _IMPORT_CORE_RE.search(src_text) is None, (
            f"grid.py 反向引用核心域：\n{src_text}"
        )
