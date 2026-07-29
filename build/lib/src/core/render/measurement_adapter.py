"""§4.4 测量卷渲染适配（T-W4-029）.

将 MeasurementPaper 适配为测量卷专用渲染中间态 MeasurementRenderIR——
在普通 RenderIR（per-item 内容）之上叠加测量卷版式特征：
- 作答卡区域（AnswerCardRegion）：每题一行，题号 + 选项涂卡位
- 题号对齐（ordered_item_irs 按卷题序分配 1..N）
- 禁止标记（ProhibitionMarker）：如「翻页无效」「禁止交头接耳」

为什么不直接复用 weekly_batch / html_renderer：
- weekly_batch（T-W2-038）面向在线周卷的批量 HTML 渲染，版式为练习/诊断
  场景（无作答卡、无翻页禁止标记）；
- 测量卷是纸笔考试场景，作答卡与禁止标记是测量有效性的物理保障
  （作答卡集中采集、翻页无效防作弊），版式诉求不同；
- 在 weekly_batch 加测量分支会改 W3 既有渲染契约（owner=src/core/render 的
  weekly_batch），本任务以独立 adapter 增量扩展，render 域内只增不改。

职责边界：本 adapter 只做「测量卷版式适配」——把已构建的 per-item RenderIR
按题序排列、附加作答卡与禁止标记；不负责 item→IR 转换（由 item_to_ir 完成）、
不负责 HTML/PDF 出口（由 html_renderer / pdf_exporter 完成）。

宪法 A5/A7：本模块不 import 任何学科包/学段包；option_labels 从 ChoiceBlock
原样读取（学科选项文本由调用方在 item_to_ir 阶段已嵌入）。
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from src.core.assembly.measurement_paper import MeasurementPaper
from src.core.render.ir import ChoiceBlock, RenderIR


# ════════════════════════════════════════════════════════════════════
# 作答卡
# ════════════════════════════════════════════════════════════════════

class AnswerCardRow(BaseModel):
    """作答卡单行：题号 + 选项涂卡位.

    option_labels 为该题 ChoiceBlock 的选项 label 列表（如 ['A','B','C','D']）；
    非选择题（填空题等）为空列表——填空题在题内空位作答，作答卡不留涂卡位。
    """

    model_config = ConfigDict(extra="forbid")

    item_number: str
    item_version_id: str
    option_labels: list[str] = Field(default_factory=list)


class AnswerCardRegion(BaseModel):
    """作答卡区域：卷内全部题的涂卡行集合（按题序）."""

    model_config = ConfigDict(extra="forbid")

    rows: list[AnswerCardRow] = Field(min_length=1)


# ════════════════════════════════════════════════════════════════════
# 禁止标记
# ════════════════════════════════════════════════════════════════════

class ProhibitionMarker(BaseModel):
    """禁止标记：卷面印刷的考试纪律提示.

    position 标识印刷位置——header（卷首）/ footer（卷尾）/ page_break
    （分页处）；「翻页无效」固定 page_break（测量卷分页时提示翻页作答无效）。
    """

    model_config = ConfigDict(extra="forbid")

    text: str
    position: Literal["header", "footer", "page_break"]


# ════════════════════════════════════════════════════════════════════
# 测量卷渲染 IR
# ════════════════════════════════════════════════════════════════════

class MeasurementRenderIR(BaseModel):
    """测量卷渲染中间态：题序 IR + 作答卡 + 禁止标记 + 作答说明.

    由 adapt_measurement_paper 产出，供下游 HTML/PDF 渲染器消费：
    - ordered_item_irs：按卷题序排列的 per-item RenderIR（题号已分配 1..N）
    - answer_card：作答卡区域（验收 #3）
    - prohibition_markers：禁止标记（验收 #3，含「翻页无效」）
    - page_instructions：作答说明（卷首页印刷）
    """

    model_config = ConfigDict(extra="forbid")

    paper_title: str
    spec_table_ref: str
    ordered_item_irs: list[RenderIR] = Field(min_length=1)
    answer_card: AnswerCardRegion
    prohibition_markers: list[ProhibitionMarker] = Field(min_length=1)
    page_instructions: str


# ════════════════════════════════════════════════════════════════════
# 内部辅助
# ════════════════════════════════════════════════════════════════════

def _extract_option_labels(ir: RenderIR) -> list[str]:
    """从 RenderIR 提取选项 label（选择题的 ChoiceBlock）.

    遍历顶层 blocks，取第一个 ChoiceBlock 的选项 label；非选择题返回空列表。
    不递归 GroupBlock——测量卷题组作答卡以题组首题位登记，子题选项在子题 IR
    内（v1 不展开，留开放项）。
    """
    for block in ir.blocks:
        if isinstance(block, ChoiceBlock):
            return [o.label for o in block.options]
    return []


def _default_prohibition_markers() -> list[ProhibitionMarker]:
    """测量卷默认禁止标记（验收 #3：含「翻页无效」）."""
    return [
        ProhibitionMarker(text="考试期间禁止交头接耳", position="header"),
        ProhibitionMarker(text="翻页无效", position="page_break"),
        ProhibitionMarker(text="答题结束请停笔", position="footer"),
    ]


# ════════════════════════════════════════════════════════════════════
# 主入口
# ════════════════════════════════════════════════════════════════════

def adapt_measurement_paper(
    paper: MeasurementPaper,
    item_irs: dict[str, RenderIR],
    *,
    paper_title: str = "测量卷",
    prohibition_markers: list[ProhibitionMarker] | None = None,
) -> MeasurementRenderIR:
    """将测量卷适配为渲染中间态（验收 #1/#3）.

    参数:
        paper: 已组装的测量卷（MeasurementPaper）。
        item_irs: item_version_id → 该题的 RenderIR（由调用方经 item_to_ir 预构建）。
            必须覆盖 paper.ordered_item_version_ids 中的每一题。
        paper_title: 卷面标题（默认「测量卷」）。
        prohibition_markers: 自定义禁止标记（None=用默认标记，含「翻页无效」）。

    返回:
        MeasurementRenderIR（ordered_item_irs 按题序、题号 1..N；answer_card 每题
        一行；prohibition_markers 含禁止标记）。

    异常:
        ValueError: paper.ordered_item_version_ids 中存在 item_irs 未覆盖的题
            （缺题则卷面不完整，fail fast 禁止静默丢题）。
    """
    if not paper.ordered_item_version_ids:
        raise ValueError("测量卷题序为空，无法适配渲染")

    ordered_item_irs: list[RenderIR] = []
    answer_rows: list[AnswerCardRow] = []
    for idx, vid in enumerate(paper.ordered_item_version_ids, start=1):
        ir = item_irs.get(vid)
        if ir is None:
            raise ValueError(
                f"item_irs 缺少 item_version_id={vid} 的 RenderIR（题号 {idx}），"
                f"卷面不完整，禁止静默丢题"
            )
        # 题号对齐：按卷题序分配 1..N（model_copy 保持原 IR 不可变）
        numbered_ir = ir.model_copy(update={"item_number": str(idx)})
        ordered_item_irs.append(numbered_ir)
        answer_rows.append(
            AnswerCardRow(
                item_number=str(idx),
                item_version_id=vid,
                option_labels=_extract_option_labels(ir),
            )
        )

    markers = prohibition_markers or _default_prohibition_markers()

    return MeasurementRenderIR(
        paper_title=paper_title,
        spec_table_ref=f"{paper.spec_table_id}/{paper.spec_table_version}",
        ordered_item_irs=ordered_item_irs,
        answer_card=AnswerCardRegion(rows=answer_rows),
        prohibition_markers=markers,
        page_instructions=paper.answer_instructions,
    )


__all__ = [
    "AnswerCardRow",
    "AnswerCardRegion",
    "ProhibitionMarker",
    "MeasurementRenderIR",
    "adapt_measurement_paper",
]
