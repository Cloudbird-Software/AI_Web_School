"""T-W4-041 成本仪表盘 + 监控告警基础.

聚合 AI 台账（T-W4-008/010）与运行时遥测，输出成本报告与健康/指标端点：
- cost_dashboard：总成本 / 按模型 / 按任务 / 按学科 / 单题平均成本
- health_endpoints：/health（DB/Redis/对象存储连通状态）+ /metrics（组卷 p95 /
  评分 avg / 近 5min 错误率）+ 告警阈值规则

宪法 A5/X6：本包不 import 任何学科包/学段包；学科维度通过 dimension_extractor
参数注入（与 T-W4-010 aggregate_cost_by_dimension 同模式）。
"""
from src.core.monitoring.cost_dashboard import (
    CostReport,
    build_cost_report,
    render_cost_report_markdown,
)
from src.core.monitoring.health_endpoints import (
    AlertRule,
    MetricsCollector,
    check_alerts,
    get_metrics_collector,
    probe_object_storage,
    probe_redis,
    router,
)

__all__ = [
    "AlertRule",
    "CostReport",
    "MetricsCollector",
    "build_cost_report",
    "check_alerts",
    "get_metrics_collector",
    "probe_object_storage",
    "probe_redis",
    "render_cost_report_markdown",
    "router",
]
