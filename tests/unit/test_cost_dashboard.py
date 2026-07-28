"""T-W4-041 成本仪表盘 + 监控告警基础 单元测试.

验收对照：
  #1 cost_dashboard 输出总成本 / 按模型 / 按任务 / 按学科 / 单题平均成本
  #2 /health 返回 DB/Redis/对象存储连通状态
  #3 /metrics 返回组卷 p95 / 评分 avg / 近 5min 错误率
  #4 make accept 全绿
  #5 不 import 任何学科包/学段包（静态扫描）

测试隔离：
- 成本仪表盘用 tmp_path 台账（set_default_ledger 注入），不污染开发库；
- MetricsCollector 每用例 reset 或注入新实例，避免跨测试污染；
- /health 端点用 async_session fixture（事务回滚隔离），不污染 DB 数据.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, AsyncIterator

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import get_async_session
from src.core.ai.ledger.ledger import Ledger, set_default_ledger
from src.core.monitoring.cost_dashboard import (
    CostReport,
    build_cost_report,
    render_cost_report_markdown,
)
from src.core.monitoring.health_endpoints import (
    AlertRule,
    DEFAULT_ALERT_RULES,
    MetricsCollector,
    check_alerts,
    get_metrics_collector,
    probe_db,
    probe_object_storage,
    probe_redis,
    router,
    set_metrics_collector,
)


# ────────────────────────────────────────────────────────────────────
# Fixture：隔离台账
# ────────────────────────────────────────────────────────────────────


@pytest.fixture
def isolated_ledger(tmp_path: Path) -> Ledger:
    """每个成本测试用独立 tmp_path 台账，互不污染."""
    ledger = Ledger(tmp_path / "ai_ledger.jsonl")
    set_default_ledger(ledger)
    yield ledger
    set_default_ledger(None)


@pytest.fixture
def isolated_metrics() -> MetricsCollector:
    """每个指标测试用独立 MetricsCollector，互不污染."""
    collector = MetricsCollector()
    set_metrics_collector(collector)
    yield collector
    set_metrics_collector(None)


def _record_sample_calls(ledger: Ledger) -> None:
    """写入一批样本台账调用（3 模型 × 2 任务 × 2 学科 = 8 条）."""
    # 学科通过 artifact_ref 前缀模拟：math:xxx / chinese:xxx
    samples = [
        # (task_name, model, token_in, token_out, artifact_ref, task_stage)
        ("draft_passage", "deepseek-reasoner", 100, 800, "math:item-rev-001", "draft"),
        ("validate", "deepseek-chat", 50, 30, "math:item-rev-001", "validate"),
        ("score", "deepseek-chat", 80, 40, "math:item-rev-001", "score"),
        ("draft_passage", "deepseek-reasoner", 120, 900, "chinese:item-rev-002", "draft"),
        ("validate", "deepseek-chat", 60, 35, "chinese:item-rev-002", "validate"),
        ("score", "gpt-4o", 90, 50, "chinese:item-rev-002", "score"),
        ("draft_passage", "deepseek-reasoner", 110, 850, "math:item-rev-003", "draft"),
        # ad_hoc 调用（无 artifact_ref）
        ("ad_hoc", "deepseek-chat", 10, 5, None, "other"),
    ]
    for task_name, model, tin, tout, ref, stage in samples:
        ledger.record_call(
            task_level="L2" if "draft" in task_name else "L1",
            task_name=task_name,
            provider="deepseek" if "deepseek" in model else "openai",
            model=model,
            prompt=f"prompt-{task_name}-{ref}",
            token_in=tin,
            token_out=tout,
            duration_ms=500.0,
            task_stage=stage,
            artifact_ref=ref,
        )


# ════════════════════════════════════════════════════════════════════
# 验收 #1：成本仪表盘
# ════════════════════════════════════════════════════════════════════


def test_build_cost_report_empty_ledger(isolated_ledger: Ledger) -> None:
    """空台账报告：全部为零，不报错（除零保护）."""
    report = build_cost_report()
    assert report.total_cost_cny == 0.0
    assert report.by_model == {}
    assert report.by_task == {}
    # 空台账无 entries → by_subject 也为空（defaultdict 未触发任何 key）
    assert report.by_subject == {}
    assert report.avg_cost_per_item == 0.0
    assert report.item_count == 0
    assert report.call_count == 0


def test_build_cost_report_total_matches_sum(isolated_ledger: Ledger) -> None:
    """总成本 = 台账全部 entries cost_cny 之和（数据一致性）."""
    _record_sample_calls(isolated_ledger)
    report = build_cost_report()

    # 直接从台账求和交叉验证
    entries = isolated_ledger.query_all()
    expected_total = round(sum(e.cost_cny for e in entries), 6)
    assert report.total_cost_cny == pytest.approx(expected_total, rel=1e-9)
    assert report.call_count == len(entries) == 8


def test_build_cost_report_by_model(isolated_ledger: Ledger) -> None:
    """按模型拆分：3 个模型各自成本合计."""
    _record_sample_calls(isolated_ledger)
    report = build_cost_report()

    assert set(report.by_model.keys()) == {
        "deepseek-chat",
        "deepseek-reasoner",
        "gpt-4o",
    }
    # 交叉验证 deepseek-reasoner 的成本
    expected_dr = round(
        sum(
            e.cost_cny
            for e in isolated_ledger.query_all()
            if e.model == "deepseek-reasoner"
        ),
        6,
    )
    assert report.by_model["deepseek-reasoner"] == pytest.approx(
        expected_dr, rel=1e-9
    )


def test_build_cost_report_by_task(isolated_ledger: Ledger) -> None:
    """按任务拆分：draft_passage / validate / score / ad_hoc."""
    _record_sample_calls(isolated_ledger)
    report = build_cost_report()

    assert set(report.by_task.keys()) == {
        "draft_passage",
        "validate",
        "score",
        "ad_hoc",
    }
    # draft_passage 占大头（reasoner 模型，token_out 高）
    assert report.by_task["draft_passage"] > report.by_task["validate"]
    assert report.by_task["draft_passage"] > report.by_task["ad_hoc"]


def test_build_cost_report_by_subject_with_extractor(
    isolated_ledger: Ledger,
) -> None:
    """按学科拆分：dimension_extractor 从 artifact_ref 解析学科前缀."""
    _record_sample_calls(isolated_ledger)

    def extract_subject(artifact_ref: str | None) -> str:
        """从 artifact_ref 前缀解析学科（模拟调用方查 ItemRevision.subject）."""
        if artifact_ref is None:
            return "unassigned"
        # artifact_ref 格式 "math:item-rev-001" / "chinese:item-rev-002"
        return artifact_ref.split(":", 1)[0]

    report = build_cost_report(dimension_extractor=extract_subject)

    assert "math" in report.by_subject
    assert "chinese" in report.by_subject
    assert "unassigned" in report.by_subject  # ad_hoc 调用

    # 交叉验证 math 学科成本
    expected_math = round(
        sum(
            e.cost_cny
            for e in isolated_ledger.query_all()
            if e.artifact_ref and e.artifact_ref.startswith("math:")
        ),
        6,
    )
    assert report.by_subject["math"] == pytest.approx(expected_math, rel=1e-9)


def test_build_cost_report_by_subject_no_extractor(isolated_ledger: Ledger) -> None:
    """无 dimension_extractor 时所有成本归 "unknown"."""
    _record_sample_calls(isolated_ledger)
    report = build_cost_report()

    assert set(report.by_subject.keys()) == {"unknown"}
    assert report.by_subject["unknown"] == pytest.approx(
        report.total_cost_cny, rel=1e-9
    )


def test_build_cost_report_avg_per_item(isolated_ledger: Ledger) -> None:
    """单题平均成本 = 总成本 / 唯一 artifact_ref 数."""
    _record_sample_calls(isolated_ledger)
    report = build_cost_report()

    # 3 个唯一 artifact_ref（math:001 / chinese:002 / math:003）
    assert report.item_count == 3
    expected_avg = round(report.total_cost_cny / 3, 6)
    assert report.avg_cost_per_item == pytest.approx(expected_avg, rel=1e-9)


def test_build_cost_report_no_artifact_ref_only(tmp_path: Path) -> None:
    """全部调用无 artifact_ref 时 avg_per_item=0（除零保护），item_count=0."""
    ledger = Ledger(tmp_path / "empty.jsonl")
    set_default_ledger(ledger)
    try:
        ledger.record_call(
            task_level="L1",
            task_name="ad_hoc",
            provider="deepseek",
            model="deepseek-chat",
            prompt="p",
            token_in=10,
            token_out=5,
            duration_ms=100.0,
            artifact_ref=None,
        )
        report = build_cost_report()
        assert report.item_count == 0
        assert report.avg_cost_per_item == 0.0
        assert report.total_cost_cny > 0  # 有调用即有成本
    finally:
        set_default_ledger(None)


def test_render_cost_report_markdown(isolated_ledger: Ledger) -> None:
    """markdown 报告包含全部章节与关键字段."""
    _record_sample_calls(isolated_ledger)
    report = build_cost_report()
    md = render_cost_report_markdown(report)

    assert "# AI 成本仪表盘报告" in md
    assert "## 总览" in md
    assert "## 按模型拆分" in md
    assert "## 按任务拆分" in md
    assert "## 按学科拆分" in md
    assert "deepseek-reasoner" in md
    assert "draft_passage" in md
    # 人民币符号
    assert "¥" in md


# ════════════════════════════════════════════════════════════════════
# MetricsCollector 测试（验收 #3 基础）
# ════════════════════════════════════════════════════════════════════


def test_metrics_collector_empty_returns_zero(
    isolated_metrics: MetricsCollector,
) -> None:
    """空采集器：所有指标返回 0.0."""
    assert isolated_metrics.assembly_p95() == 0.0
    assert isolated_metrics.grading_avg() == 0.0
    assert isolated_metrics.error_rate_last_5min() == 0.0
    counts = isolated_metrics.sample_counts()
    assert counts == {"assembly": 0, "grading": 0, "outcomes": 0}


def test_metrics_collector_assembly_p95(
    isolated_metrics: MetricsCollector,
) -> None:
    """组卷 p95：100 个样本（1.0-2.0s），p95 应接近 1.95s."""
    for i in range(100):
        # 延迟从 1.0s 线性递增到 1.99s
        isolated_metrics.record_assembly(1.0 + i * 0.01)
    p95 = isolated_metrics.assembly_p95()
    # p95 应在 1.94-1.99 之间（线性插值）
    assert 1.93 <= p95 <= 2.0


def test_metrics_collector_grading_avg(
    isolated_metrics: MetricsCollector,
) -> None:
    """评分平均：3 个样本 (0.5, 1.0, 1.5) → avg=1.0."""
    for v in (0.5, 1.0, 1.5):
        isolated_metrics.record_grading(v)
    assert isolated_metrics.grading_avg() == pytest.approx(1.0, rel=1e-9)


def test_metrics_collector_error_rate_5min(
    isolated_metrics: MetricsCollector,
) -> None:
    """错误率：10 次请求中 2 次错误 → 0.2."""
    for i in range(8):
        isolated_metrics.record_assembly(1.0, error=False)
    isolated_metrics.record_assembly(2.0, error=True)
    isolated_metrics.record_error("db_failure")
    # 10 outcomes, 2 errors
    assert isolated_metrics.error_rate_last_5min() == pytest.approx(0.2, rel=1e-9)


def test_metrics_collector_reset(isolated_metrics: MetricsCollector) -> None:
    """reset 清空所有样本."""
    isolated_metrics.record_assembly(1.0)
    isolated_metrics.record_grading(2.0)
    isolated_metrics.record_error()
    isolated_metrics.reset()
    assert isolated_metrics.assembly_p95() == 0.0
    assert isolated_metrics.grading_avg() == 0.0
    assert isolated_metrics.error_rate_last_5min() == 0.0


# ════════════════════════════════════════════════════════════════════
# 组件探测测试（验收 #2 基础）
# ════════════════════════════════════════════════════════════════════


def test_probe_redis_not_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    """REDIS_URL 未设置时返回 not_configured（不报错）."""
    monkeypatch.delenv("REDIS_URL", raising=False)
    result = probe_redis()
    # redis 包可能未安装或 REDIS_URL 未设，均应 not_configured
    assert result["status"] in ("not_configured", "unhealthy")
    assert "reason" in result


def test_probe_object_storage_not_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OBJECT_STORAGE_PATH 未设置时返回 not_configured."""
    monkeypatch.delenv("OBJECT_STORAGE_PATH", raising=False)
    result = probe_object_storage()
    assert result["status"] == "not_configured"
    assert "OBJECT_STORAGE_PATH" in result["reason"]


def test_probe_object_storage_ok(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OBJECT_STORAGE_PATH 指向可写目录时返回 ok."""
    monkeypatch.setenv("OBJECT_STORAGE_PATH", str(tmp_path))
    result = probe_object_storage()
    assert result["status"] == "ok"
    # 探针文件应已清理
    assert not (tmp_path / ".health_probe").exists()


def test_probe_object_storage_unhealthy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OBJECT_STORAGE_PATH 指向不存在路径时返回 unhealthy."""
    monkeypatch.setenv(
        "OBJECT_STORAGE_PATH", "/definitely/does/not/exist/monitoring-test"
    )
    result = probe_object_storage()
    assert result["status"] == "unhealthy"


@pytest.mark.asyncio
async def test_probe_db_ok(async_session: AsyncSession) -> None:
    """DB 探测：真实 async_session 执行 SELECT 1 返回 ok."""
    result = await probe_db(async_session)
    assert result["status"] == "ok"


@pytest.mark.asyncio
async def test_probe_db_unhealthy() -> None:
    """DB 探测：模拟异常时返回 unhealthy + reason."""

    class _FailingSession:
        """模拟 execute 抛异常的会话."""

        async def execute(self, *args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("simulated connection failure")

    result = await probe_db(_FailingSession())  # type: ignore[arg-type]
    assert result["status"] == "unhealthy"
    assert "simulated connection failure" in result["reason"]


# ════════════════════════════════════════════════════════════════════
# 告警规则测试
# ════════════════════════════════════════════════════════════════════


def test_check_alerts_triggers_when_above_threshold() -> None:
    """指标超阈值时触发告警."""
    metrics = {
        "assembly_p95": 3.5,  # 阈值 2.0
        "grading_avg": 12.0,  # 阈值 10.0
        "error_rate_5min": 0.08,  # 阈值 0.05
    }
    alerts = check_alerts(metrics)
    assert len(alerts) == 3
    alert_metrics = {a["metric"] for a in alerts}
    assert alert_metrics == {"assembly_p95", "grading_avg", "error_rate_5min"}


def test_check_alerts_no_trigger_when_within_threshold() -> None:
    """指标在阈值内不触发告警."""
    metrics = {
        "assembly_p95": 1.5,  # < 2.0
        "grading_avg": 8.0,  # < 10.0
        "error_rate_5min": 0.02,  # < 0.05
    }
    alerts = check_alerts(metrics)
    assert alerts == []


def test_check_alerts_missing_metric_skipped() -> None:
    """缺失的指标不触发告警（避免误报）."""
    metrics = {"assembly_p95": 3.0}  # 只有 1 个指标超阈值
    alerts = check_alerts(metrics)
    assert len(alerts) == 1
    assert alerts[0]["metric"] == "assembly_p95"


def test_check_alerts_custom_rules() -> None:
    """自定义规则：lt 比较方向."""
    rules = [
        AlertRule(
            metric="sample_count",
            threshold=100.0,
            comparison="lt",
            message="样本数不足 100",
        )
    ]
    alerts = check_alerts({"sample_count": 50.0}, rules=rules)
    assert len(alerts) == 1
    assert alerts[0]["message"] == "样本数不足 100"


def test_default_alert_rules_cover_three_key_metrics() -> None:
    """默认规则覆盖组卷/评分/错误率三项关键指标."""
    metrics_covered = {r.metric for r in DEFAULT_ALERT_RULES}
    assert metrics_covered == {
        "assembly_p95",
        "grading_avg",
        "error_rate_5min",
    }


# ════════════════════════════════════════════════════════════════════
# 端点集成测试（验收 #2 / #3）
# ════════════════════════════════════════════════════════════════════


def _create_monitoring_app() -> FastAPI:
    """构造仅含监控路由的 FastAPI 测试 app（不污染 src/api/main.py）."""
    app = FastAPI()
    app.include_router(router)
    return app


@pytest_asyncio.fixture
async def monitoring_client(
    async_session: AsyncSession,
) -> AsyncIterator[AsyncClient]:
    """构造 httpx AsyncClient + ASGI 传输，DB 走 async_session fixture."""
    app = _create_monitoring_app()

    async def _override_session() -> AsyncIterator[AsyncSession]:
        yield async_session

    app.dependency_overrides[get_async_session] = _override_session

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client

    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_health_endpoint_returns_component_status(
    monitoring_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """/health 返回 DB/Redis/对象存储三组件状态（验收 #2）."""
    # Redis/对象存储标记为 not_configured，避免环境依赖
    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.delenv("OBJECT_STORAGE_PATH", raising=False)

    resp = await monitoring_client.get("/health")
    assert resp.status_code == 200
    body = resp.json()

    assert "status" in body
    assert "components" in body
    components = body["components"]
    assert "db" in components
    assert "redis" in components
    assert "object_storage" in components
    # DB 应正常（async_session fixture 提供真实连接）
    assert components["db"]["status"] == "ok"
    # Redis/对象存储未配置（开发环境）
    assert components["redis"]["status"] in ("not_configured", "ok", "unhealthy")
    assert components["object_storage"]["status"] == "not_configured"
    # DB 正常 + 其他未配置 → 整体 ok
    assert body["status"] == "ok"


@pytest.mark.asyncio
async def test_health_endpoint_with_storage_ok(
    monitoring_client: AsyncClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """配置 OBJECT_STORAGE_PATH 后对象存储探测返回 ok."""
    monkeypatch.setenv("OBJECT_STORAGE_PATH", str(tmp_path))
    monkeypatch.delenv("REDIS_URL", raising=False)

    resp = await monitoring_client.get("/health")
    body = resp.json()
    assert body["components"]["object_storage"]["status"] == "ok"
    assert body["status"] == "ok"


@pytest.mark.asyncio
async def test_metrics_endpoint_returns_key_metrics(
    monitoring_client: AsyncClient,
    isolated_metrics: MetricsCollector,
) -> None:
    """/metrics 返回组卷 p95 / 评分 avg / 错误率 / 告警（验收 #3）."""
    # 填充样本数据
    for i in range(20):
        isolated_metrics.record_assembly(1.0 + i * 0.05, error=(i == 0))
    for v in (0.5, 1.0, 1.5):
        isolated_metrics.record_grading(v)

    resp = await monitoring_client.get("/metrics")
    assert resp.status_code == 200
    body = resp.json()

    assert "assembly_p95_seconds" in body
    assert "grading_avg_seconds" in body
    assert "error_rate_5min" in body
    assert "sample_counts" in body
    assert "alerts" in body

    # 20 个组卷样本（1.0-1.95s），p95 应 < 2.0（不触发告警）
    assert 1.5 <= body["assembly_p95_seconds"] <= 2.0
    # 3 个评分样本 avg=1.0
    assert body["grading_avg_seconds"] == pytest.approx(1.0, rel=1e-9)
    # 20 组卷 + 3 评分 = 23 outcomes，其中 1 个错误（i==0）
    assert body["error_rate_5min"] == pytest.approx(1 / 23, rel=1e-6)
    # 样本计数
    assert body["sample_counts"]["assembly"] == 20
    assert body["sample_counts"]["grading"] == 3


@pytest.mark.asyncio
async def test_metrics_endpoint_empty_metrics(
    monitoring_client: AsyncClient,
    isolated_metrics: MetricsCollector,
) -> None:
    """空指标时 /metrics 返回零值，不报错."""
    resp = await monitoring_client.get("/metrics")
    assert resp.status_code == 200
    body = resp.json()
    assert body["assembly_p95_seconds"] == 0.0
    assert body["grading_avg_seconds"] == 0.0
    assert body["error_rate_5min"] == 0.0
    assert body["alerts"] == []  # 无指标不触发告警


@pytest.mark.asyncio
async def test_metrics_endpoint_alert_triggered(
    monitoring_client: AsyncClient,
    isolated_metrics: MetricsCollector,
) -> None:
    """指标超阈值时 /metrics 返回告警."""
    # 组卷 p95 = 3.0s（超 2.0 阈值）
    for _ in range(10):
        isolated_metrics.record_assembly(3.0)
    resp = await monitoring_client.get("/metrics")
    body = resp.json()
    alert_metrics = {a["metric"] for a in body["alerts"]}
    assert "assembly_p95" in alert_metrics


# ════════════════════════════════════════════════════════════════════
# 验收 #5：不 import 学科包/学段包（静态扫描）
# ════════════════════════════════════════════════════════════════════


def test_no_subject_pack_imports_in_monitoring() -> None:
    """src/core/monitoring/ 禁止 import 学科包/学段包（宪法 A5/X6）."""
    monitoring_dir = (
        Path(__file__).resolve().parent.parent.parent
        / "src"
        / "core"
        / "monitoring"
    )
    assert monitoring_dir.is_dir(), f"目录不存在: {monitoring_dir}"
    pattern = re.compile(
        r"^\s*(?:from\s+(?:packs|src\.packs)"
        r"|import\s+(?:packs|src\.packs))",
        re.MULTILINE,
    )
    violations: list[str] = []
    for py_file in sorted(monitoring_dir.rglob("*.py")):
        text = py_file.read_text(encoding="utf-8")
        if pattern.findall(text):
            violations.append(str(py_file.relative_to(monitoring_dir)))
    assert not violations, (
        f"monitoring 存在学科包 import（违反 A5）：{violations}"
    )
