"""T-W4-037 在线渲染学段适配层：Render IR → 学段差异化 HTML/样式提示.

adapt_for_gradeband(render_ir, grade_band, hints=...) 按学段注入：
- 低段（L）：注音层 <ruby>、朗读按钮 data 属性、大字号 CSS 类、数字键盘触发标记
- 中段（M）/ 高段（H）：保持常规呈现，不注入低段专属元素（验收 #3）

设计要点：
- **核心域零特判（A5）**：本模块是核心域，不 import 学科包/学段包；
  学段参数通过 ``hints`` dict 注入（由调用方加载学段包 config.yaml 生成）。
- **IR 不可变**：本模块不修改入参 RenderIR；返回适配后的 IR 副本 + 渲染提示。
- **与学科渲染组件正交**：本适配层只做学段差异化（注音/字号/键盘/朗读），
  不感知学科（数学 SVG / 语文语篇 / 英语听力均由各组件自处理）。

调用约定（hints dict 形状，由学段包 render_hints.py 产出）::

    {
      "grade_band": "L",
      "phonetic": true,
      "phonetic_coverage": "full",
      "font_size": "24px",
      "read_aloud": true,
      "keyboard": "numeric",          # 仅数值填空类交互触发
      "keyboard_allowed": "0123456789"
    }

中/高段 hints 形如 ``{phonetic: False, font_size: None, ...}``（不注入）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

from src.core.render.components.phonetic_overlay import apply_phonetic_to_text
from src.core.render.ir import (
    Block,
    ChoiceBlock,
    FillBlock,
    GroupBlock,
    MathSvgBlock,
    RenderIR,
    TextBlock,
)

# 触发数字键盘的交互类型（与低学段包 render_hints 同约定；
# 核心不 import 学段包，此常量是核心域交互类型分类）
_NUMERIC_KEYBOARD_INTERACTIONS = frozenset({"numeric_blank", "text_blank_numeric"})

# 低段大字号 CSS 类（HTML 渲染器附在 item 容器上）
LOW_BAND_FONT_CLASS = "gb-low-large-font"

# 低段朗读按钮 data 属性前缀
READ_ALOUD_DATA_ATTR = "data-read-aloud"

# 低段数字键盘触发标记
NUMERIC_KEYBOARD_DATA_ATTR = "data-keyboard"

# 低段注音层 CSS 类（标注在 item 容器上，CSS 控制注音渲染样式）
PHONETIC_DATA_ATTR = "data-phonetic"


@dataclass(frozen=True)
class GradeBandAdaptation:
    """adapt_for_gradeband 返回结果.

    Attributes:
        ir: 适配后的 RenderIR（学段专属标记注入 layout_hints / item 容器属性）。
        html_hints: 渲染器消费的学段样式提示 dict（CSS 类/data 属性/注音 map）。
        grade_band: 学段（L/M/H）。
        phonetic_applied: 是否实际注入了注音层（低段 + 有 phonetic_map 时 True）。
    """

    ir: RenderIR
    html_hints: dict[str, Any]
    grade_band: str
    phonetic_applied: bool


def _is_low_band(grade_band: str) -> bool:
    """判定是否低段（核心域不感知学段包，仅按标识 'L' 判定）."""
    return grade_band == "L"


def _merge_hints(
    grade_band: str,
    hints: Optional[Mapping[str, Any]],
) -> dict[str, Any]:
    """合并默认 hints 与调用方提供的 hints（调用方覆盖默认）.

    中/高段默认无学段专属元素；低段默认空（待调用方注入完整 hints）。
    """
    default: dict[str, Any] = {
        "grade_band": grade_band,
        "phonetic": False,
        "phonetic_coverage": None,
        "font_size": None,
        "read_aloud": False,
        "keyboard": None,
        "keyboard_allowed": None,
    }
    if hints:
        default.update(dict(hints))
    return default


def _phonetic_text_block(
    block: TextBlock,
    phonetic_map: Optional[Mapping[str, str]],
) -> TextBlock:
    """对文本块应用注音（返回新 TextBlock，原 IR 不可变）.

    为什么把注音烘焙进 text.value：RenderIR 是「最终内容表示」，注音是低段
    专属的呈现，烘焙进 value 后 HTML/PDF 渲染器无需感知学段差异——它们只
    面对一种稳定契约（文本块即 HTML 片段）。
    """
    if not phonetic_map:
        return block
    return TextBlock(value=apply_phonetic_to_text(block.value, phonetic_map))


def _adapt_block(
    block: Block,
    phonetic_map: Optional[Mapping[str, str]],
) -> Block:
    """单 block 学段适配（当前仅文本块受注音影响；其他类型透传）."""
    if isinstance(block, TextBlock):
        return _phonetic_text_block(block, phonetic_map)
    if isinstance(block, GroupBlock):
        # 题组：递归适配子题 IR（共享素材 material 是字符串，不注音——
        # 素材通常是语篇，由学科包渲染组件单独处理注音）
        return GroupBlock(
            material=block.material,
            items=[_adapt_ir(ir, phonetic_map) for ir in block.items],
        )
    # choice / fill / math_svg：学段不影响内容（CSS/交互由 html_hints 控制）
    return block


def _adapt_ir(
    ir: RenderIR,
    phonetic_map: Optional[Mapping[str, str]],
) -> RenderIR:
    """对单题 IR 应用学段适配（注音烘焙；其他由 html_hints 表达）."""
    if not phonetic_map:
        return ir
    new_blocks = [_adapt_block(b, phonetic_map) for b in ir.blocks]
    # RenderIR.model_copy 保持其他字段（item_version_id 等）原样
    return ir.model_copy(update={"blocks": new_blocks})


def adapt_for_gradeband(
    render_ir: RenderIR,
    grade_band: str,
    *,
    hints: Optional[Mapping[str, Any]] = None,
    phonetic_map: Optional[Mapping[str, str]] = None,
) -> GradeBandAdaptation:
    """按学段适配 RenderIR，返回适配后 IR + 渲染器样式提示.

    Args:
        render_ir: 待适配的 RenderIR（不可变；本函数返回副本）。
        grade_band: 学段（L/M/H）。
        hints: 学段包 render_hints() 产出的 dict（含 phonetic/font_size/
            keyboard 等）。None 时用核心默认（不注入任何学段元素）。
        phonetic_map: 注音字典 {字符: 拼音}，仅低段 + hints.phonetic=True 时
            应用。None 时不烘焙注音（适配层只输出注音标记，由前端按需注音）。

    Returns:
        GradeBandAdaptation：适配后 IR + html_hints（CSS 类/data 属性/标记）。

    Notes:
        验收 #2 低段输出含：<ruby> 注音（烘焙进 text 块）、朗读按钮 data 属性、
        大字号 CSS 类、数字键盘触发标记——全部通过 html_hints 表达给渲染器。
        验收 #3 中/高段：html_hints 中 phonetic=False / font_size=None /
        keyboard=None，渲染器据此不注入低段元素。
    """
    merged = _merge_hints(grade_band, hints)
    is_low = _is_low_band(grade_band)

    # 注音应用：低段 + phonetic=True + 提供 phonetic_map 时烘焙
    phonetic_applied = False
    adapted_ir = render_ir
    if is_low and merged.get("phonetic") and phonetic_map:
        adapted_ir = _adapt_ir(render_ir, phonetic_map)
        phonetic_applied = any(
            isinstance(b, TextBlock) and b.value for b in adapted_ir.blocks
        )

    # html_hints：渲染器消费的学段样式提示
    html_hints: dict[str, Any] = {
        "grade_band": grade_band,
        "phonetic": bool(merged.get("phonetic", False)),
        "phonetic_coverage": merged.get("phonetic_coverage"),
        "font_size": merged.get("font_size"),
        "font_class": LOW_BAND_FONT_CLASS if is_low and merged.get("font_size") else None,
        "read_aloud": bool(merged.get("read_aloud", False)),
        "read_aloud_attr": READ_ALOUD_DATA_ATTR if merged.get("read_aloud") else None,
        "keyboard": merged.get("keyboard"),
        "keyboard_allowed": merged.get("keyboard_allowed"),
        "keyboard_attr": (
            NUMERIC_KEYBOARD_DATA_ATTR
            if is_low
            and merged.get("keyboard") == "numeric"
            and render_ir.interaction_id in _NUMERIC_KEYBOARD_INTERACTIONS
            else None
        ),
        "phonetic_attr": PHONETIC_DATA_ATTR if merged.get("phonetic") else None,
    }

    return GradeBandAdaptation(
        ir=adapted_ir,
        html_hints=html_hints,
        grade_band=grade_band,
        phonetic_applied=phonetic_applied,
    )


__all__ = [
    "GradeBandAdaptation",
    "LOW_BAND_FONT_CLASS",
    "READ_ALOUD_DATA_ATTR",
    "NUMERIC_KEYBOARD_DATA_ATTR",
    "PHONETIC_DATA_ATTR",
    "adapt_for_gradeband",
]
