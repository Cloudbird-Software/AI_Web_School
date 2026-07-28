"""T-W4-010 单题全生命周期 AI 成本归集.

从 T-W4-008 台账按 item_revision_id 聚合各阶段 AI 调用成本，输出单题成本报告。

阶段分桶（对齐 ADR §4.8 单题全生命周期）：
- draft：母题/语篇起草
- instantiate：实例化（A/B 级生成）
- validate：校验门（事实/适龄/许可等 AI 校验）
- score：评分（含 AI 量规评分）
- rescore：重判（新 scorer 重放写平行 score_run）
- other：上述之外的 AI 调用

数据一致性（验收 #3）：归集总和 = 台账 cost_cny 总和。由 aggregate_cost 直接
求和 ledger.query_by_artifact 返回的 entries 保证，无独立计费路径。

宪法 A5：学科维度通过 dimension_extractor 参数注入，本包不 import 学科包。
"""
from __future__ import annotations

from typing import Callable, Optional

from src.core.ai.ledger.ledger import Ledger, get_default_ledger
from src.core.ai.ledger.schemas import TaskStage

# 成本归集阶段（对齐 ledger.schemas.TaskStage）
# 顺序即报告输出顺序；other 兜底未分类调用
STAGES: tuple[str, ...] = (
    "draft",
    "instantiate",
    "validate",
    "score",
    "rescore",
    "other",
)

# TaskStage 合法值集合，用于桶归属判定
_VALID_STAGES: frozenset[str] = frozenset(STAGES)


def aggregate_cost(
    item_revision_id: str,
    ledger: Optional[Ledger] = None,
) -> dict[str, float]:
    """单题全生命周期 AI 成本，按阶段分桶（验收 #1）.

    Args:
        item_revision_id: 题目版本 id（台账 artifact_ref）。
        ledger: 台账实例（None 时用默认实例）。

    Returns:
        {stage: cost_cny}，stage 覆盖 STAGES 全部六阶段；无调用的阶段为 0.0。
        例：{"draft": 0.032, "validate": 0.001, "score": 0.002, ...}
    """
    lg = ledger if ledger is not None else get_default_ledger()
    entries = lg.query_by_artifact(item_revision_id)
    cost_by_stage: dict[str, float] = {stage: 0.0 for stage in STAGES}
    for entry in entries:
        # task_stage 不在已知阶段时归入 other（兜底）
        stage = entry.task_stage if entry.task_stage in _VALID_STAGES else "other"
        cost_by_stage[stage] += entry.cost_cny
    # 保留 6 位小数对齐 ledger.compute_cost_cny 精度
    return {stage: round(cost, 6) for stage, cost in cost_by_stage.items()}


def total_cost(
    item_revision_id: str,
    ledger: Optional[Ledger] = None,
) -> float:
    """单题全生命周期总成本（所有阶段合计，验收 #3 一致性验证用）.

    与 aggregate_cost 的关系：total_cost = sum(aggregate_cost(...).values())。
    独立实现（直接求和 ledger entries）而非聚合 aggregate_cost，用于交叉验证
    归集逻辑无丢失。
    """
    lg = ledger if ledger is not None else get_default_ledger()
    entries = lg.query_by_artifact(item_revision_id)
    return round(sum(e.cost_cny for e in entries), 6)


def aggregate_cost_by_dimension(
    item_revision_ids: list[str],
    dimension_extractor: Callable[[str], str],
    ledger: Optional[Ledger] = None,
) -> dict[str, float]:
    """按维度（学科/学段/生产线）批量汇总总成本（验收 #2）.

    Args:
        item_revision_ids: 待归集的题目版本 id 列表。
        dimension_extractor: item_revision_id -> 维度值 的回调。
            调用方注入维度提取逻辑（如查 ItemRevision.subject），
            本包不 import 学科包（A5），学科维度由调用方解析。
        ledger: 台账实例（None 时用默认实例）。

    Returns:
        {dimension_value: total_cost_cny}，每个维度值的合计成本。
        例：{"数学": 1.23, "语文": 4.56}（dimension_extractor 返回学科名时）。

    Notes:
        维度提取由调用方负责——本包只做成本聚合，不解析题目元数据，
        避免核心域 import 学科包（A5）或反查 ORM（保持纯函数）。
    """
    lg = ledger if ledger is not None else get_default_ledger()
    totals: dict[str, float] = {}
    for ir_id in item_revision_ids:
        dimension = dimension_extractor(ir_id)
        # 复用 total_cost 保证一致性（验收 #3）
        cost = total_cost(ir_id, lg)
        totals[dimension] = round(totals.get(dimension, 0.0) + cost, 6)
    return totals


def aggregate_cost_stages_by_dimension(
    item_revision_ids: list[str],
    dimension_extractor: Callable[[str], str],
    ledger: Optional[Ledger] = None,
) -> dict[str, dict[str, float]]:
    """按维度汇总各阶段成本（维度 × 阶段 矩阵，验收 #2 增强版）.

    Returns:
        {dimension_value: {stage: cost}}，每个维度下各阶段成本明细。
        例：{"数学": {"draft": 0.5, "score": 0.3, ...}, "语文": {...}}
    """
    lg = ledger if ledger is not None else get_default_ledger()
    result: dict[str, dict[str, float]] = {}
    for ir_id in item_revision_ids:
        dimension = dimension_extractor(ir_id)
        stages = aggregate_cost(ir_id, lg)
        if dimension not in result:
            result[dimension] = {stage: 0.0 for stage in STAGES}
        for stage, cost in stages.items():
            result[dimension][stage] = round(
                result[dimension].get(stage, 0.0) + cost, 6
            )
    return result
