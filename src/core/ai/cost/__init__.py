"""T-W4-010 单题全生命周期 AI 成本归集.

按 item_revision 聚合各阶段（起草/实例化/验证/评分/重判/其他）AI 调用台账，
输出单题成本报告；支持按学科/学段/生产线维度批量汇总。

依赖 T-W4-008 台账接口（query_by_artifact），数据一致性由定义保证：
台账 cost_cny 总和 = 归集总和。

宪法 A5：本包不 import 任何学科包/学段包；学科维度通过参数注入（验收 #5）。
"""
from src.core.ai.cost.item_lifecycle_cost import (
    STAGES,
    aggregate_cost,
    aggregate_cost_by_dimension,
    total_cost,
)

__all__ = [
    "STAGES",
    "aggregate_cost",
    "aggregate_cost_by_dimension",
    "total_cost",
]
