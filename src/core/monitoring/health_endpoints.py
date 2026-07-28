"""T-W4-041 健康端点 + 关键指标端点 + 告警阈值基础.

端点（FastAPI APIRouter，由调用方 include 到 app）：
- GET /health：返回 DB / Redis / 对象存储连通状态（验收 #2）
- GET /metrics：返回组卷 p95 / 评分 avg / 近 5min 错误率（验收 #3）

设计取舍（non_goals：Grafana/Prometheus 集成、自动告警通知）：
- MetricsCollector 用内存环形缓冲（deque maxlen），单进程内有效，重启丢失；
  满足 T-W4-041「监控告警基础」需求，生产级时序库留后续 wave.
- 告警规则仅返回触发的告警列表，不发送通知（non_goal #2）.

为什么 Redis/对象存储探测是可选的：
- 开发/测试环境不一定部署 Redis/MinIO；未配置时返回 not_configured 而非 unhealthy，
  避免开发环境 /health 永远 503.
- DB 是必选项（宪法 D2：PostgreSQL 强约束），DB 不可达时整体 unhealthy.

宪法 A5/X6：本包不 import 任何学科包/学段包.
"""
from __future__ import annotations

import os
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from typing import Any, Optional

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import get_async_session

router = APIRouter(tags=["monitoring"])


# ────────────────────────────────────────────────────────────────────
# 运行时指标采集（内存环形缓冲）
# ────────────────────────────────────────────────────────────────────


def _percentile(values: list[float], pct: float) -> float:
    """线性插值百分位数（无第三方依赖）.

    Args:
        values: 样本列表（无序输入，内部排序拷贝）.
        pct: 百分位（0-100）.

    Returns:
        百分位值；空样本返回 0.0.
    """
    if not values:
        return 0.0
    sorted_vals = sorted(values)
    n = len(sorted_vals)
    if n == 1:
        return sorted_vals[0]
    # 线性插值索引（与 numpy.percentile 默认一致）
    rank = (pct / 100.0) * (n - 1)
    lo = int(rank)
    hi = min(lo + 1, n - 1)
    frac = rank - lo
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * frac


class MetricsCollector:
    """运行时指标采集（内存环形缓冲）.

    采集三类指标（对齐验收 #3）：
    - 组卷延迟样本 → assembly_p95（p95 百分位）
    - 评分延迟样本 → grading_avg（算术平均）
    - 请求结果（成功/失败）+ 时间戳 → error_rate_last_5min

    为什么用 deque(maxlen=...)：固定上限防止内存膨胀；旧样本自动淘汰，
    无需显式清理线程. 1000 样本对单进程监控足够（约 1000 次最近请求）.
    """

    def __init__(self, max_samples: int = 1000) -> None:
        self._assembly_latencies: deque[float] = deque(maxlen=max_samples)
        self._grading_latencies: deque[float] = deque(maxlen=max_samples)
        # (monotonic_timestamp, is_error)：用于近 5min 错误率
        self._outcomes: deque[tuple[float, bool]] = deque(maxlen=max_samples)

    def record_assembly(self, seconds: float, error: bool = False) -> None:
        """记录一次组卷请求的延迟与结果."""
        self._assembly_latencies.append(seconds)
        self._outcomes.append((monotonic(), error))

    def record_grading(self, seconds: float, error: bool = False) -> None:
        """记录一次评分请求的延迟与结果."""
        self._grading_latencies.append(seconds)
        self._outcomes.append((monotonic(), error))

    def record_error(self, error_type: str = "generic") -> None:
        """记录非延迟型错误（如 DB 连接失败、校验门拒绝）.

        error_type 参数当前仅用于日志，不参与聚合（未来扩展可按类型分桶）.
        """
        self._outcomes.append((monotonic(), True))

    def assembly_p95(self) -> float:
        """组卷 p95 延迟（秒）."""
        return _percentile(list(self._assembly_latencies), 95)

    def grading_avg(self) -> float:
        """评分平均延迟（秒）."""
        if not self._grading_latencies:
            return 0.0
        return sum(self._grading_latencies) / len(self._grading_latencies)

    def error_rate_last_5min(self) -> float:
        """近 5 分钟错误率 = 错误数 / 总请求数.

        为什么用 outcomes 而非 assembly+grading 样本：record_error 记录的非延迟
        错误（如 DB 连接失败）不会进延迟样本但应计入错误率分母；
        outcomes 统一记录所有请求结果（含 record_error），覆盖更全.

        Returns:
            0.0-1.0 之间的浮点数；无样本时返回 0.0.
        """
        cutoff = monotonic() - 300.0  # 5 min
        recent = [(ts, err) for ts, err in self._outcomes if ts >= cutoff]
        if not recent:
            return 0.0
        errors = sum(1 for _, err in recent if err)
        return errors / len(recent)

    def sample_counts(self) -> dict[str, int]:
        """返回各指标当前样本数（调试/自省用）."""
        return {
            "assembly": len(self._assembly_latencies),
            "grading": len(self._grading_latencies),
            "outcomes": len(self._outcomes),
        }

    def reset(self) -> None:
        """清空所有样本（测试隔离用）."""
        self._assembly_latencies.clear()
        self._grading_latencies.clear()
        self._outcomes.clear()


# 模块级单例（生产用；测试用 set_metrics_collector 注入或直接调 reset）
_metrics_collector: Optional[MetricsCollector] = None


def get_metrics_collector() -> MetricsCollector:
    """获取默认 MetricsCollector 单例（懒建）."""
    global _metrics_collector
    if _metrics_collector is None:
        _metrics_collector = MetricsCollector()
    return _metrics_collector


def set_metrics_collector(collector: Optional[MetricsCollector]) -> None:
    """注入默认 MetricsCollector（测试隔离用；传 None 重置）."""
    global _metrics_collector
    _metrics_collector = collector


# ────────────────────────────────────────────────────────────────────
# 组件连通性探测
# ────────────────────────────────────────────────────────────────────


async def probe_db(session: AsyncSession) -> dict[str, Any]:
    """探测 PostgreSQL 连通性（SELECT 1）.

    Returns:
        {"status": "ok"} 或 {"status": "unhealthy", "reason": "..."}.
    """
    try:
        result = await session.execute(text("SELECT 1"))
        scalar = result.scalar()
        if scalar == 1:
            return {"status": "ok"}
        return {
            "status": "unhealthy",
            "reason": f"SELECT 1 返回非预期值: {scalar!r}",
        }
    except Exception as exc:  # noqa: BLE001 — 探测需捕获所有异常转 unhealthy
        return {"status": "unhealthy", "reason": str(exc)}


def probe_redis() -> dict[str, Any]:
    """探测 Redis 连通性（可选基础设施）.

    为什么 try import：redis-py 非必装依赖（开发环境可能未装）；
    未安装时返回 not_configured 而非报错，避免开发环境 /health 永远降级.

    Returns:
        {"status": "ok"} / {"status": "not_configured", "reason": "..."} /
        {"status": "unhealthy", "reason": "..."}.
    """
    try:
        import redis  # type: ignore[import-untyped]
    except ImportError:
        return {
            "status": "not_configured",
            "reason": "redis 包未安装",
        }

    url = os.environ.get("REDIS_URL")
    if not url:
        return {
            "status": "not_configured",
            "reason": "REDIS_URL 未设置",
        }
    try:
        client = redis.from_url(url, socket_connect_timeout=2, socket_timeout=2)
        pong = client.ping()
        client.close()
        return {"status": "ok" if pong else "unhealthy"}
    except Exception as exc:  # noqa: BLE001
        return {"status": "unhealthy", "reason": str(exc)}


def probe_object_storage() -> dict[str, Any]:
    """探测对象存储连通性（可选基础设施）.

    为什么用本地路径探测：MinIO/S3 集成未在 core 实现（T-W4-041 non_goals），
    用 OBJECT_STORAGE_PATH（本地挂载点或共享路径）探测读写；
    未配置时返回 not_configured.

    Returns:
        {"status": "ok"} / {"status": "not_configured", "reason": "..."} /
        {"status": "unhealthy", "reason": "..."}.
    """
    path = os.environ.get("OBJECT_STORAGE_PATH")
    if not path:
        return {
            "status": "not_configured",
            "reason": "OBJECT_STORAGE_PATH 未设置",
        }
    p = Path(path)
    if not p.exists():
        return {"status": "unhealthy", "reason": f"{path} 不存在"}
    if not p.is_dir():
        return {"status": "unhealthy", "reason": f"{path} 不是目录"}
    # 写探针文件验证可写
    probe = p / ".health_probe"
    try:
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return {"status": "ok"}
    except Exception as exc:  # noqa: BLE001
        return {"status": "unhealthy", "reason": str(exc)}


# ────────────────────────────────────────────────────────────────────
# 告警规则（阈值触发，不发送通知 — non_goal #2）
# ────────────────────────────────────────────────────────────────────


@dataclass
class AlertRule:
    """告警规则：指标超阈值时触发.

    Attributes:
        metric: 指标名（assembly_p95 / grading_avg / error_rate_5min /
            total_cost_cny）.
        threshold: 阈值（与指标同单位）.
        comparison: "gt"（大于触发）/ "lt"（小于触发）.
        message: 告警描述（人类可读）.
    """

    metric: str
    threshold: float
    comparison: str = "gt"
    message: str = ""


# 默认告警规则（与任务卡目标对齐：组卷 p95 < 2s，评分 avg < 10s，错误率 < 5%）
DEFAULT_ALERT_RULES: list[AlertRule] = [
    AlertRule(
        metric="assembly_p95",
        threshold=2.0,
        comparison="gt",
        message="组卷 p95 延迟超过 2s 阈值",
    ),
    AlertRule(
        metric="grading_avg",
        threshold=10.0,
        comparison="gt",
        message="评分平均延迟超过 10s 阈值",
    ),
    AlertRule(
        metric="error_rate_5min",
        threshold=0.05,
        comparison="gt",
        message="近 5min 错误率超过 5% 阈值",
    ),
]


def check_alerts(
    metrics: dict[str, float],
    rules: Optional[list[AlertRule]] = None,
) -> list[dict[str, Any]]:
    """检查告警规则，返回触发的告警列表.

    Args:
        metrics: 指标字典，key 对齐 AlertRule.metric.
        rules: 告警规则列表（None 时用 DEFAULT_ALERT_RULES）.

    Returns:
        触发的告警列表，每项 {"metric", "threshold", "actual", "message"}.
    """
    if rules is None:
        rules = list(DEFAULT_ALERT_RULES)
    triggered: list[dict[str, Any]] = []
    for rule in rules:
        actual = metrics.get(rule.metric)
        if actual is None:
            continue  # 指标缺失不触发（避免误报）
        fired = (
            actual > rule.threshold
            if rule.comparison == "gt"
            else actual < rule.threshold
        )
        if fired:
            triggered.append(
                {
                    "metric": rule.metric,
                    "threshold": rule.threshold,
                    "actual": actual,
                    "comparison": rule.comparison,
                    "message": rule.message,
                }
            )
    return triggered


# ────────────────────────────────────────────────────────────────────
# FastAPI 端点
# ────────────────────────────────────────────────────────────────────


@router.get("/health", summary="健康检查（DB/Redis/对象存储连通状态）")
async def health(
    session: AsyncSession = Depends(get_async_session),
) -> dict[str, Any]:
    """返回各组件连通状态（验收 #2）.

    整体状态判定：
    - ok：所有已配置组件正常（not_configured 不影响整体）
    - unhealthy：任一已配置组件不可达
    """
    db_status = await probe_db(session)
    redis_status = probe_redis()
    storage_status = probe_object_storage()

    components = [db_status, redis_status, storage_status]
    # not_configured 视为非降级（开发环境可选组件未部署）
    overall = "ok" if all(
        s["status"] in ("ok", "not_configured") for s in components
    ) else "unhealthy"

    return {
        "status": overall,
        "components": {
            "db": db_status,
            "redis": redis_status,
            "object_storage": storage_status,
        },
    }


@router.get("/metrics", summary="关键运行指标（组卷p95/评分avg/错误率）")
async def metrics() -> dict[str, Any]:
    """返回关键运行指标 + 告警状态（验收 #3）."""
    collector = get_metrics_collector()
    snapshot = {
        "assembly_p95": collector.assembly_p95(),
        "grading_avg": collector.grading_avg(),
        "error_rate_5min": collector.error_rate_last_5min(),
    }
    alerts = check_alerts(snapshot)
    return {
        "assembly_p95_seconds": snapshot["assembly_p95"],
        "grading_avg_seconds": snapshot["grading_avg"],
        "error_rate_5min": snapshot["error_rate_5min"],
        "sample_counts": collector.sample_counts(),
        "alerts": alerts,
    }


__all__ = [
    "AlertRule",
    "DEFAULT_ALERT_RULES",
    "MetricsCollector",
    "check_alerts",
    "get_metrics_collector",
    "health",
    "metrics",
    "probe_db",
    "probe_object_storage",
    "probe_redis",
    "router",
    "set_metrics_collector",
]
