"""T-W4-041 成本仪表盘：聚合 AI 台账输出成本报告.

复用 T-W4-008 台账（query_all）与 T-W4-010 维度归集思路，跨全量台账 entries
输出：
- 总成本（人民币元）
- 按模型拆分（model -> cost_cny）
- 按任务拆分（task_name -> cost_cny）
- 按学科拆分（dimension_extractor(artifact_ref) -> cost_cny）
- 单题平均成本（总成本 / 唯一 artifact_ref 数）

数据一致性（验收 #1）：报告 total_cost_cny = 台账全部 entries 的 cost_cny 之和。
与 T-W4-010 total_cost(item_revision_id) 的关系：本仪表盘是跨题汇总，
T-W4-010 是单题明细；两者对同一 ledger 求和结果一致（定义保证）。

宪法 A5：学科维度通过 dimension_extractor 参数注入，本包不 import 学科包。
artifact_ref=None 的调用（如 ad_hoc / 未关联产物的 draft）在学科维度归入
"unassigned"，避免 dimension_extractor 收到 None 报错。
"""
from __future__ import annotations

from collections import defaultdict
from typing import Callable, Optional

from pydantic import BaseModel, Field

from src.core.ai.ledger.ledger import Ledger, get_default_ledger


class CostReport(BaseModel):
    """成本报告（聚合结果容器，验收 #1）.

    Attributes:
        total_cost_cny: 全量台账成本合计（人民币元）.
        by_model: {model: cost_cny}，按实际命中模型拆分.
        by_task: {task_name: cost_cny}，按业务任务名拆分.
        by_subject: {dimension_value: cost_cny}，按学科/维度拆分；
            dimension_extractor=None 时所有成本归 "unknown".
        avg_cost_per_item: 单题平均成本 = total / 唯一 artifact_ref 数；
            无 artifact_ref 时为 0.0（避免除零）.
        item_count: 唯一 artifact_ref 数（单题全生命周期归集基数）.
        call_count: 台账总调用数（entries 数）.
    """

    total_cost_cny: float = Field(ge=0.0)
    by_model: dict[str, float] = Field(default_factory=dict)
    by_task: dict[str, float] = Field(default_factory=dict)
    by_subject: dict[str, float] = Field(default_factory=dict)
    avg_cost_per_item: float = Field(ge=0.0)
    item_count: int = Field(ge=0)
    call_count: int = Field(ge=0)

    def to_dict(self) -> dict[str, object]:
        """转字典（便于 JSON 序列化与端点返回）."""
        return self.model_dump()


def build_cost_report(
    ledger: Optional[Ledger] = None,
    dimension_extractor: Optional[Callable[[Optional[str]], str]] = None,
) -> CostReport:
    """构建成本报告：聚合全量台账 entries（验收 #1）.

    Args:
        ledger: 台账实例（None 时用默认实例 get_default_ledger）.
        dimension_extractor: artifact_ref -> 维度值（如学科名）的回调.
            接收 Optional[str] 以处理 artifact_ref=None 的调用（ad_hoc 调用）；
            None 时所有成本归 "unknown"（不区分学科）.
            调用方负责解析题目元数据（如查 ItemRevision.subject），
            本包不 import 学科包（A5），保持纯聚合.

    Returns:
        CostReport，字段对齐验收 #1 五项指标.

    Notes:
        为什么不强制要求 dimension_extractor：开发/测试环境可能无学科映射，
        强制会阻断报告生成；None 时 by_subject={"unknown": total} 仍有总成本.
    """
    lg = ledger if ledger is not None else get_default_ledger()
    entries = lg.query_all()

    total = 0.0
    by_model: dict[str, float] = defaultdict(float)
    by_task: dict[str, float] = defaultdict(float)
    by_subject: dict[str, float] = defaultdict(float)
    artifact_refs: set[str] = set()

    for entry in entries:
        total += entry.cost_cny
        by_model[entry.model] += entry.cost_cny
        by_task[entry.task_name] += entry.cost_cny

        # 学科维度：None artifact_ref 归 "unassigned"，避免 extractor 收到 None
        if dimension_extractor is not None:
            if entry.artifact_ref is None:
                dim = "unassigned"
            else:
                dim = dimension_extractor(entry.artifact_ref)
        else:
            dim = "unknown"
        by_subject[dim] += entry.cost_cny

        if entry.artifact_ref is not None:
            artifact_refs.add(entry.artifact_ref)

    avg_per_item = total / len(artifact_refs) if artifact_refs else 0.0

    return CostReport(
        total_cost_cny=round(total, 6),
        by_model={k: round(v, 6) for k, v in by_model.items()},
        by_task={k: round(v, 6) for k, v in by_task.items()},
        by_subject={k: round(v, 6) for k, v in by_subject.items()},
        avg_cost_per_item=round(avg_per_item, 6),
        item_count=len(artifact_refs),
        call_count=len(entries),
    )


def render_cost_report_markdown(report: CostReport) -> str:
    """渲染成本报告为 markdown（人工查阅/导出用）.

    输出结构：总览 → 按模型 → 按任务 → 按学科 → 单题平均.
    与 tools/telemetry.py render_markdown 风格一致（表格 + 对齐）.
    """
    lines = [
        "# AI 成本仪表盘报告",
        "",
        "## 总览",
        f"- 总成本: ¥{report.total_cost_cny:.4f}",
        f"- 调用总数: {report.call_count}",
        f"- 单题数（唯一 artifact_ref）: {report.item_count}",
        f"- 单题平均成本: ¥{report.avg_cost_per_item:.4f}",
        "",
        "## 按模型拆分",
        "| 模型 | 成本 (¥) |",
        "|---|---|",
    ]
    for model, cost in sorted(report.by_model.items(), key=lambda kv: -kv[1]):
        lines.append(f"| {model} | {cost:.4f} |")

    lines += ["", "## 按任务拆分", "| 任务 | 成本 (¥) |", "|---|---|"]
    for task, cost in sorted(report.by_task.items(), key=lambda kv: -kv[1]):
        lines.append(f"| {task} | {cost:.4f} |")

    lines += ["", "## 按学科拆分", "| 学科 | 成本 (¥) |", "|---|---|"]
    for subject, cost in sorted(report.by_subject.items(), key=lambda kv: -kv[1]):
        lines.append(f"| {subject} | {cost:.4f} |")

    return "\n".join(lines) + "\n"


__all__ = [
    "CostReport",
    "build_cost_report",
    "render_cost_report_markdown",
]
