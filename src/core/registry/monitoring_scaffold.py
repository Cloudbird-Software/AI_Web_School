"""Issue #22 / T-W2-041: CI tests & monitoring scaffold.

提供：
1. `HealthReport`：健康快照（db/redis/import_latency/search_latency/p95 响应等）
2. `record_event`：事件打点（counter/timer）—— 本地 JSON 文件落盘（便于 CI 收集；
   接入 Prometheus/StatsD 时可替换后端）。
3. `run_smoke_suite`：启动时自检套件（连接 DB/Redis、打 1 次 import 基线、
   打 1 次 search 基线；0=全绿，非 0=不通过，便于 CI 作为 pre-merge 健康检查）。

设计约束：
- 不引入新依赖（不用 prometheus_client、不用 redis-py 之外的 pkg；Redis 失败不 fail）。
- 输出：`out/metrics/<timestamp>.json`。
- 核心域零特判（宪法 A5）：不含学科代码判断。
"""
from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Optional, Protocol

from src.core.registry.search_service import (
    PerfBaselineReport,
    SearchQuery,
    perf_baseline_in_memory,
    search_in_pool,
    _generate_random_pool,
)


PROJECT_ROOT = Path(__file__).resolve().parents[3]
METRICS_DIR = PROJECT_ROOT / "out" / "metrics"


# ════════════════════════════════════════════════════════════════════
# 事件打点器
# ════════════════════════════════════════════════════════════════════


@dataclass
class CounterRecord:
    name: str
    value: int
    labels: dict[str, str]
    ts: str


@dataclass
class TimerRecord:
    name: str
    duration_ms: int
    labels: dict[str, str]
    ts: str


class MetricsStore:
    """轻量事件存储：内存 + 可选 JSON flush.

    单例：使用 `get_metrics_store()`。
    """

    _INSTANCE: Optional["MetricsStore"] = None
    _LOCK = threading.Lock()

    def __init__(self) -> None:
        self._counters: dict[tuple[str, tuple], int] = {}
        self._timers_ms: dict[tuple[str, tuple], list[int]] = {}
        self._counter_log: list[CounterRecord] = []
        self._timer_log: list[TimerRecord] = []

    def incr(self, name: str, n: int = 1, **labels: str) -> None:
        key = (name, tuple(sorted(labels.items())))
        self._counters[key] = self._counters.get(key, 0) + n
        self._counter_log.append(CounterRecord(
            name=name, value=self._counters[key],
            labels=dict(labels),
            ts=_now_iso(),
        ))

    def observe(self, name: str, duration_ms: int, **labels: str) -> None:
        key = (name, tuple(sorted(labels.items())))
        lst = self._timers_ms.setdefault(key, [])
        lst.append(duration_ms)
        self._timer_log.append(TimerRecord(
            name=name, duration_ms=duration_ms,
            labels=dict(labels),
            ts=_now_iso(),
        ))

    def summary(self) -> dict[str, Any]:
        out_counters = []
        for (name, kvs), v in self._counters.items():
            out_counters.append({"name": name, "labels": dict(kvs), "value": v})
        out_timers = []
        for (name, kvs), vals in self._timers_ms.items():
            sv = sorted(vals)
            def _p(pct: float) -> int:
                return sv[min(len(sv) - 1, max(0, int(len(sv) * pct)))]
            out_timers.append({
                "name": name,
                "labels": dict(kvs),
                "samples": len(vals),
                "p50_ms": _p(0.5),
                "p95_ms": _p(0.95),
                "p99_ms": _p(0.99),
                "max_ms": sv[-1] if sv else 0,
            })
        return {"counters": out_counters, "timers": out_timers}

    def flush(self, output_dir: Optional[Path] = None) -> Path:
        output_dir = output_dir or METRICS_DIR
        output_dir.mkdir(parents=True, exist_ok=True)
        path = output_dir / f"metrics-{int(time.time())}.json"
        path.write_text(json.dumps({
            "generated_at": _now_iso(),
            "summary": self.summary(),
            "counter_log": [asdict(x) for x in self._counter_log[-2000:]],
            "timer_log": [asdict(x) for x in self._timer_log[-2000:]],
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        return path


def get_metrics_store() -> MetricsStore:
    with MetricsStore._LOCK:
        if MetricsStore._INSTANCE is None:
            MetricsStore._INSTANCE = MetricsStore()
    return MetricsStore._INSTANCE


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


# ════════════════════════════════════════════════════════════════════
# 健康快照
# ════════════════════════════════════════════════════════════════════


@dataclass
class HealthReport:
    overall_ok: bool
    db_ok: Optional[bool] = None
    redis_ok: Optional[bool] = None
    perf_baseline: Optional[dict[str, Any]] = None
    search_latency_sample_ms: Optional[int] = None
    counters: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    generated_at: str = field(default_factory=_now_iso)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


async def _check_db_optional() -> Optional[bool]:
    try:
        from tests.conftest import _get_sync_engine  # may not exist
    except Exception:
        _get_sync_engine = None  # type: ignore
    try:
        from sqlalchemy import text
        from sqlalchemy.ext.asyncio import create_async_engine
        db_url = os.environ.get("DATABASE_URL") or os.environ.get("PG_URL")
        if not db_url:
            return None
        engine = create_async_engine(db_url, pool_pre_ping=False)
        try:
            async with engine.connect() as conn:
                r = await conn.execute(text("SELECT 1"))
                return bool(r.scalar_one() == 1)
        finally:
            await engine.dispose()
    except Exception:
        return False


async def _check_redis_optional() -> Optional[bool]:
    try:
        import redis  # type: ignore
    except Exception:
        return None
    try:
        url = os.environ.get("REDIS_URL")
        if not url:
            return None
        r = redis.Redis.from_url(url, socket_connect_timeout=1)
        try:
            return bool(r.ping())
        finally:
            try:
                r.close()
            except Exception:
                pass
    except Exception:
        return False


# ════════════════════════════════════════════════════════════════════
# 启动自检套件（CI pre-merge 钩子）
# ════════════════════════════════════════════════════════════════════


async def run_smoke_suite(
    *,
    require_db: bool = False,
    require_redis: bool = False,
    n_items_baseline: int = 1000,
) -> HealthReport:
    """Issue #22 / 验收 #3 启动自检：CI / 容器启动 / 部署前可调用.

    - 检查 DB（可选择必须通过）
    - 检查 Redis（可选择必须通过）
    - 跑 1 次性能基线（内存版）
    - 打 1 次 search 查询 sample，记录 latency
    - 返回 HealthReport.overall_ok；CI `if overall_ok: exit(0) else exit(1)`
    """
    errs: list[str] = []
    store = get_metrics_store()
    t0 = time.perf_counter()

    db_ok = await _check_db_optional()
    if require_db and db_ok is not True:
        errs.append(f"DB 检查失败（require_db=True），实际 db_ok={db_ok}")
    redis_ok = await _check_redis_optional()
    if require_redis and redis_ok is not True:
        errs.append(f"Redis 检查失败（require_redis=True），实际 redis_ok={redis_ok}")

    perf: Optional[dict[str, Any]] = None
    try:
        pb = await perf_baseline_in_memory(n_items=n_items_baseline)
        perf = asdict(pb)
    except Exception as e:  # pragma: no cover
        errs.append(f"perf_baseline 异常：{type(e).__name__}: {e}")
        store.incr("baseline.failed")
    else:
        store.observe("perf.baseline_ms", int(pb.kp_search_latency_ms or 0), kind="kp")
        store.observe("perf.baseline_ms", int(pb.keyword_search_latency_ms or 0), kind="kw")
        store.incr("baseline.ok")

    sample_latency: Optional[int] = None
    try:
        pool = _generate_random_pool(500)
        q = SearchQuery(gradebands=["L", "M"], limit=20)
        r = search_in_pool(pool, q)
        sample_latency = r.latency_ms
        store.observe("search.latency_ms", r.latency_ms, kind="gradeband")
    except Exception as e:  # pragma: no cover
        errs.append(f"search sample 异常：{type(e).__name__}: {e}")

    store.observe("smoke.duration_ms", int((time.perf_counter() - t0) * 1000))
    overall = (not errs) and (not require_db or db_ok is True) and (not require_redis or redis_ok is True)
    report = HealthReport(
        overall_ok=overall,
        db_ok=db_ok,
        redis_ok=redis_ok,
        perf_baseline=perf,
        search_latency_sample_ms=sample_latency,
        errors=errs,
    )
    # 持久化 metrics + health report
    try:
        mfile = store.flush()
        hfile = mfile.with_name(f"health-{mfile.stem.split('-', 1)[1]}.json")
        hfile.write_text(json.dumps(report.to_dict(), ensure_ascii=False, indent=2),
                         encoding="utf-8")
    except Exception:  # pragma: no cover
        pass
    return report


def run_smoke_suite_sync(**kwargs) -> HealthReport:  # convenience for bash entry
    return asyncio.run(run_smoke_suite(**kwargs))


__all__ = [
    "HealthReport",
    "MetricsStore",
    "get_metrics_store",
    "run_smoke_suite",
    "run_smoke_suite_sync",
]
