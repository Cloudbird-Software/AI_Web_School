"""T-W2-033 Render IR v1：内容与样式分离的中间态.

IR 是 ItemVersion → 渲染出口（HTML/PDF）之间的纯内容表示，承载：
- 题号、题面文本、选项、填空位、数学 SVG、题组嵌套
- 版式提示（layout_hints）：分页/保同页等渲染偏好

设计原则：
- 内容与样式分离：IR 不含任何 CSS/HTML，样式由品牌模板（T-W2-036）决定
- 学科零特判：IR 是核心域类型，不 import 学科包（A5）；学科 SVG 组件经
  注册表挂载后以 math_svg 块原样进入 IR（T-W2-029）
- 不可变快照：IR 序列化结果可作为 rendered_snapshot 物化（D2 复现不依赖引擎）

为什么不直接从 ItemVersion.content 渲染：content.blocks 是 permissive dict
（契约 §5 未强制 schema），交互类型决定块语义；IR 把这种隐式语义显式化为
强类型 Block，让 HTML/PDF 渲染器只面对一种稳定契约。
"""
from __future__ import annotations

from typing import Annotated, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field


# ════════════════════════════════════════════════════════════════════
# Block 类型（discriminated union by "type"）
# ════════════════════════════════════════════════════════════════════

class TextBlock(BaseModel):
    """纯文本块（题面段落、说明文字）.

    value 为已渲染的最终文本（变量已替换、语料已嵌入）；
    若含数学公式，用 KaTeX 定界符 $...$ / $$...$$ 标记，由 HTML 渲染器转换。
    """

    model_config = ConfigDict(extra="forbid")

    type: Literal["text"] = "text"
    value: str


class FillBlock(BaseModel):
    """填空块（text_blank / numeric_blank 交互的空位）.

    kind 区分文本填空与数值填空（呼应 interaction.yaml 的 text_blank /
    numeric_blank）；numeric 情形下 unit 可选（题目声明需要单位时填单位 id）。
    blank_id 与 scoring_ref 的 blanks 键对齐，评分器按 blank_id 逐空判分。
    """

    model_config = ConfigDict(extra="forbid")

    type: Literal["fill"] = "fill"
    blank_id: str
    kind: Literal["text", "numeric"]
    unit: Optional[str] = None
    # 空位显示长度（字符数），0 表示用默认下划线长度；学段包可覆盖
    width: int = 0


class OptionItem(BaseModel):
    """选项条目（single_choice / multi_choice 交互的选项）."""

    model_config = ConfigDict(extra="forbid")

    id: str
    label: str


class ChoiceBlock(BaseModel):
    """选择题块（single_choice / multi_choice 交互的选项集合）.

    mode 由 interaction_ref.interaction_id 推导：single_choice→single，
    multi_choice→multi。options 为已渲染的最终选项文本（变量已替换）。
    """

    model_config = ConfigDict(extra="forbid")

    type: Literal["choice"] = "choice"
    mode: Literal["single", "multi"]
    options: list[OptionItem]


class MathSvgBlock(BaseModel):
    """数学 SVG 块（学科包渲染组件产出的 SVG 原样嵌入）.

    svg 为完整 <svg>...</svg> 字符串；caption 为图注（可选）。
    核心域不解释 SVG 语义，只做透传嵌入——学科零特判（A5）。
    """

    model_config = ConfigDict(extra="forbid")

    type: Literal["math_svg"] = "math_svg"
    svg: str
    caption: Optional[str] = None


class GroupBlock(BaseModel):
    """题组块（一材多题：共享素材 + 嵌套子题 IR）.

    题组（item_group）的 RenderIR 用一个 group 块承载共享素材与子题列表；
    子题各自是完整的 RenderIR（递归结构）。material 为共享素材的已渲染文本
    （语篇/图表说明等），可为空。
    """

    model_config = ConfigDict(extra="forbid")

    type: Literal["group"] = "group"
    material: Optional[str] = None
    items: list["RenderIR"]


# discriminated union：按 type 字段分发到对应 Block 类型
Block = Annotated[
    Union[TextBlock, FillBlock, ChoiceBlock, MathSvgBlock, GroupBlock],
    Field(discriminator="type"),
]


# ════════════════════════════════════════════════════════════════════
# 版式提示
# ════════════════════════════════════════════════════════════════════

class LayoutHints(BaseModel):
    """版式提示（渲染器参考，非强制）.

    为什么用提示而非强制：不同出口（PDF 分页 vs HTML 流式）对版式约束的
    执行能力不同；IR 表达意图，出口自行取舍。
    - page_break_before：本块前强制分页（大题开始）
    - keep_with_next：与下一块保同页（题干+选项不被分页隔开）
    - preferred_columns：选项/填空排列列数（1=纵向，2=两列）
    """

    model_config = ConfigDict(extra="forbid")

    page_break_before: bool = False
    keep_with_next: bool = False
    preferred_columns: int = 1


# ════════════════════════════════════════════════════════════════════
# RenderIR 顶层
# ════════════════════════════════════════════════════════════════════

class RenderIR(BaseModel):
    """渲染中间态顶层 schema（T-W2-033）.

    一份 RenderIR = 一道题（含题组嵌套）的纯内容表示。
    - item_version_id / item_id：溯源到 item_version 表（D3 内容寻址）
    - interaction_id：来自 interaction_ref，决定作答采集与评分契约
    - item_number：卷内题号（由组卷器分配，IR 自身不含排序逻辑）
    - blocks：题面内容序列（text/fill/choice/math_svg/group）
    - layout_hints：版式提示
    """

    model_config = ConfigDict(extra="forbid")

    item_version_id: str
    item_id: str
    interaction_id: str
    item_number: Optional[str] = None
    blocks: list[Block] = Field(default_factory=list)
    layout_hints: LayoutHints = Field(default_factory=LayoutHints)


# 前向引用解析（GroupBlock.items 引用 RenderIR）
GroupBlock.model_rebuild()


__all__ = [
    "RenderIR",
    "Block",
    "TextBlock",
    "FillBlock",
    "OptionItem",
    "ChoiceBlock",
    "MathSvgBlock",
    "GroupBlock",
    "LayoutHints",
]
