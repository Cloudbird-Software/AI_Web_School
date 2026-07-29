"""T-W2-029 方格纸 SVG 渲染组件.

生成数学试卷常用的方格纸 SVG，支持：
- 配置行/列数（cell 数量）
- 单元格尺寸（像素）
- 单元格高亮（按 (row, col) 列表标记，用于"在方格中涂色"题型）

输出为完整 <svg>...</svg> 字符串，可被嵌入 RenderIR 的 math_svg 块。

设计要点：
- 纯字符串拼装 SVG（X8：避免新依赖）
- 原点左上角；row=0 在顶部，col=0 在左侧（与 SVG/HTML 一致）
- 主网格线深色，5 格处加粗辅助线（便于读数）
- 学科包零反向：本模块只依赖标准库 + typing，不 import src.core.*
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


# ════════════════════════════════════════════════════════════════════
# Spec
# ════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class GridSpec:
    """方格纸配置.

    Attributes:
        rows: 行数（≥1）
        cols: 列数（≥1）
        cell_size: 单元格边长（像素）
        highlight_cells: 高亮单元格列表 [(row, col), ...]
        label: 方格纸标签（可选，显示在右上角）
        major_every: 每多少格加粗辅助线（默认 5）
    """

    rows: int
    cols: int
    cell_size: int = 24
    highlight_cells: Optional[list[tuple[int, int]]] = None
    label: Optional[str] = None
    major_every: int = 5


# ════════════════════════════════════════════════════════════════════
# 渲染
# ════════════════════════════════════════════════════════════════════

# 标签区高度
_LABEL_BAND = 16


def _validate(spec: GridSpec) -> None:
    """校验 spec 合法性."""
    if spec.rows < 1:
        raise ValueError(f"rows 必须 ≥1，得到 {spec.rows}")
    if spec.cols < 1:
        raise ValueError(f"cols 必须 ≥1，得到 {spec.cols}")
    if spec.cell_size < 4:
        raise ValueError(f"cell_size 太小（{spec.cell_size}），最小 4")
    if spec.major_every < 1:
        raise ValueError(f"major_every 必须 ≥1，得到 {spec.major_every}")
    if spec.highlight_cells is not None:
        for r, c in spec.highlight_cells:
            if not (0 <= r < spec.rows and 0 <= c < spec.cols):
                raise ValueError(
                    f"highlight_cell ({r},{c}) 越界（grid {spec.rows}x{spec.cols}）"
                )


def _escape_text(s: str) -> str:
    """转义 SVG 文本特殊字符."""
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def render_grid(spec: GridSpec) -> str:
    """根据 spec 生成方格纸 SVG 字符串.

    Args:
        spec: 方格纸配置（已校验）

    Returns:
        完整 <svg>...</svg> 字符串，可嵌入 HTML

    Raises:
        ValueError: spec 不合法
    """
    _validate(spec)
    grid_w = spec.cols * spec.cell_size
    grid_h = spec.rows * spec.cell_size
    # 留顶部标签带 + 底部小边距
    total_h = grid_h + _LABEL_BAND + 4
    total_w = grid_w + 4  # 右侧 2px 边距对称

    parts: list[str] = []
    parts.append(
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'width="{total_w}" height="{total_h}" '
        f'viewBox="0 0 {total_w} {total_h}" '
        f'role="img" aria-label="方格纸 {spec.rows} 行 {spec.cols} 列">'
    )

    # 偏移：让网格在 (2, _LABEL_BAND) 开始
    ox = 2
    oy = _LABEL_BAND

    # ── 高亮单元格（先画，让网格压在上面） ──
    if spec.highlight_cells:
        for r, c in spec.highlight_cells:
            x = ox + c * spec.cell_size
            y = oy + r * spec.cell_size
            parts.append(
                f'<rect x="{x}" y="{y}" '
                f'width="{spec.cell_size}" height="{spec.cell_size}" '
                f'fill="#d62728" opacity="0.35"/>'
            )

    # ── 网格线 ──
    # 竖线（每 major_every 加粗）
    for i in range(spec.cols + 1):
        x = ox + i * spec.cell_size
        is_major = i % spec.major_every == 0
        sw = "0.8" if is_major else "0.4"
        color = "#1a1a1a" if is_major else "#888"
        parts.append(
            f'<line x1="{x}" y1="{oy}" x2="{x}" y2="{oy + grid_h}" '
            f'stroke="{color}" stroke-width="{sw}"/>'
        )
    # 横线
    for i in range(spec.rows + 1):
        y = oy + i * spec.cell_size
        is_major = i % spec.major_every == 0
        sw = "0.8" if is_major else "0.4"
        color = "#1a1a1a" if is_major else "#888"
        parts.append(
            f'<line x1="{ox}" y1="{y}" x2="{ox + grid_w}" y2="{y}" '
            f'stroke="{color}" stroke-width="{sw}"/>'
        )

    # 外边框（加粗，明确边界）
    parts.append(
        f'<rect x="{ox}" y="{oy}" width="{grid_w}" height="{grid_h}" '
        f'fill="none" stroke="#1a1a1a" stroke-width="1.2"/>'
    )

    # ── 标签 ──
    if spec.label:
        parts.append(
            f'<text x="{total_w - 2}" y="12" '
            f'font-family="Arial, sans-serif" font-size="11" '
            f'text-anchor="end" fill="#555">{_escape_text(spec.label)}</text>'
        )

    parts.append("</svg>")
    return "".join(parts)


__all__ = ["GridSpec", "render_grid"]
