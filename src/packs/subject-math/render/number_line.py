"""T-W2-029 数轴 SVG 渲染组件.

生成数学试卷常用的数轴 SVG，支持：
- 配置起点/终点/步长（刻度间隔）
- 整数刻度自动标签
- 高亮点（在数轴上标记一个值，常用于"在数轴上标出 X"题型）
- 高亮区间（标记一段区间，用于不等式解集可视化）

输出为完整 <svg>...</svg> 字符串，可被嵌入 RenderIR 的 math_svg 块。

设计要点：
- 纯字符串拼装 SVG，不依赖第三方 SVG 库（X8：避免新依赖）
- 坐标系：SVG 原点左上角；数轴方向 left→right；y 居中
- 标签字体大小固定为 12px（A4 打印可读），可经 spec 覆盖
- 学科包零反向：本模块只依赖标准库 + typing，不 import src.core.*
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


# ════════════════════════════════════════════════════════════════════
# Spec
# ════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class NumberLineSpec:
    """数轴配置.

    Attributes:
        start: 数轴起点（含），整数
        end: 数轴终点（含），整数，必须 > start
        step: 刻度步长（正整数），(end-start) 必须被 step 整除
        width: SVG 宽度（像素）
        height: SVG 高度（像素），含上下边距留给标签
        highlight_point: 高亮点值（必须在 [start, end] 内）
        highlight_range: 高亮区间 (lo, hi)，闭区间
        label: 数轴标签（可选，显示在右上角）
        font_size: 刻度数字字号
    """

    start: int
    end: int
    step: int = 1
    width: int = 480
    height: int = 80
    highlight_point: Optional[int] = None
    highlight_range: Optional[tuple[int, int]] = None
    label: Optional[str] = None
    font_size: int = 12


# ════════════════════════════════════════════════════════════════════
# 渲染
# ════════════════════════════════════════════════════════════════════

# 上下边距（标签/高亮点呼吸空间）
_TOP_MARGIN = 24
_BOTTOM_MARGIN = 24
# 箭头大小
_ARROW_SIZE = 6


def _validate(spec: NumberLineSpec) -> None:
    """校验 spec 合法性（违反则抛 ValueError，避免生成错误 SVG）."""
    if spec.end <= spec.start:
        raise ValueError(f"end({spec.end}) 必须 > start({spec.start})")
    if spec.step <= 0:
        raise ValueError(f"step 必须 > 0，得到 {spec.step}")
    span = spec.end - spec.start
    if span % spec.step != 0:
        raise ValueError(
            f"(end-start)={span} 必须被 step={spec.step} 整除"
        )
    if spec.highlight_point is not None and not (spec.start <= spec.highlight_point <= spec.end):
        raise ValueError(
            f"highlight_point={spec.highlight_point} 不在 [{spec.start}, {spec.end}] 内"
        )
    if spec.highlight_range is not None:
        lo, hi = spec.highlight_range
        if lo > hi:
            raise ValueError(f"highlight_range lo={lo} > hi={hi}")
        if not (spec.start <= lo <= hi <= spec.end):
            raise ValueError(
                f"highlight_range=({lo},{hi}) 不在 [{spec.start}, {spec.end}] 内"
            )


def _value_to_x(spec: NumberLineSpec, value: int) -> float:
    """数轴值 → SVG x 坐标.

    数轴在 SVG 内水平铺开，留左右各 _ARROW_SIZE*2 边距给箭头。
    """
    left_pad = _ARROW_SIZE * 2
    right_pad = _ARROW_SIZE * 2
    usable = spec.width - left_pad - right_pad
    span = spec.end - spec.start
    # value 在 [start, end] 内的归一化位置
    ratio = (value - spec.start) / span
    return left_pad + ratio * usable


def _escape_text(s: str) -> str:
    """转义 SVG 文本中的特殊字符（& < >）."""
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def render_number_line(spec: NumberLineSpec) -> str:
    """根据 spec 生成数轴 SVG 字符串.

    Args:
        spec: 数轴配置（已校验）

    Returns:
        完整 <svg>...</svg> 字符串，可嵌入 HTML

    Raises:
        ValueError: spec 不合法
    """
    _validate(spec)
    y_axis = spec.height // 2
    left_x = _ARROW_SIZE * 2
    right_x = spec.width - _ARROW_SIZE * 2

    parts: list[str] = []
    # SVG 头部：viewBox 让缩放保持比例
    parts.append(
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'width="{spec.width}" height="{spec.height}" '
        f'viewBox="0 0 {spec.width} {spec.height}" '
        f'role="img" aria-label="数轴 {spec.start} 到 {spec.end}">'
    )

    # ── 高亮区间（先画，让数轴压在上面） ──
    if spec.highlight_range is not None:
        lo, hi = spec.highlight_range
        x1 = _value_to_x(spec, lo)
        x2 = _value_to_x(spec, hi)
        # 粗线段 + 端点圆点（闭区间）
        parts.append(
            f'<line x1="{x1:.2f}" y1="{y_axis}" x2="{x2:.2f}" y2="{y_axis}" '
            f'stroke="#d62728" stroke-width="6" stroke-linecap="round" '
            f'opacity="0.55"/>'
        )
        for x in (x1, x2):
            parts.append(
                f'<circle cx="{x:.2f}" cy="{y_axis}" r="4" fill="#d62728"/>'
            )

    # ── 主轴线 + 箭头 ──
    parts.append(
        f'<line x1="{left_x}" y1="{y_axis}" x2="{right_x}" y2="{y_axis}" '
        f'stroke="#1a1a1a" stroke-width="1.5"/>'
    )
    # 右箭头
    parts.append(
        f'<polygon points="{right_x},{y_axis} '
        f'{right_x - _ARROW_SIZE},{y_axis - _ARROW_SIZE} '
        f'{right_x - _ARROW_SIZE},{y_axis + _ARROW_SIZE}" '
        f'fill="#1a1a1a"/>'
    )
    # 左箭头（双向数轴？暂不需要，仅右向）

    # ── 刻度 + 标签 ──
    for v in range(spec.start, spec.end + 1, spec.step):
        x = _value_to_x(spec, v)
        # 短刻度线
        parts.append(
            f'<line x1="{x:.2f}" y1="{y_axis - 5}" '
            f'x2="{x:.2f}" y2="{y_axis + 5}" '
            f'stroke="#1a1a1a" stroke-width="1"/>'
        )
        # 标签（数字，下方）
        parts.append(
            f'<text x="{x:.2f}" y="{y_axis + _BOTTOM_MARGIN - 2}" '
            f'font-family="Arial, sans-serif" font-size="{spec.font_size}" '
            f'text-anchor="middle" fill="#1a1a1a">{v}</text>'
        )

    # ── 高亮点 ──
    if spec.highlight_point is not None:
        x = _value_to_x(spec, spec.highlight_point)
        parts.append(
            f'<circle cx="{x:.2f}" cy="{y_axis}" r="5" '
            f'fill="#d62728" stroke="#1a1a1a" stroke-width="1"/>'
        )

    # ── 标签（右上角） ──
    if spec.label:
        parts.append(
            f'<text x="{spec.width - 4}" y="14" '
            f'font-family="Arial, sans-serif" font-size="{spec.font_size}" '
            f'text-anchor="end" fill="#555">{_escape_text(spec.label)}</text>'
        )

    parts.append("</svg>")
    return "".join(parts)


__all__ = ["NumberLineSpec", "render_number_line"]
