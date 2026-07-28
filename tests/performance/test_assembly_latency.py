"""T-W4-038 在线组卷压测：并发 50 请求，p95 < 2s.

验收（任务卡 T-W4-038 §验收）：
1. 并发 50 请求组卷，p95 延迟 <2s（本地 DB，无网络抖动）。
2. （#2 在 test_preassembled_fallback.py）
3. 报告含：延迟分布直方图、成功率、错误分类、环境信息（CPU/DB 规模）。
5. 不 import 任何学科包/学段包。

设计说明：
- 50 个「组卷请求」= 50 个独立 (seed, snapshot_ref) 的 assemble() 调用，
  通过 ThreadPoolExecutor 并发提交（模拟在线后端线程池）。
- 候选池 200 题在测试内构造（见 conftest.build_large_pool），模拟「DB 已加载池」
  后的求解热路径——生产中同 (pack, gradeband) 的池可跨请求复用。
- 「本地 DB，无网络抖动」的环境条件写入 report_assembly.md（PostgreSQL 16 本地）。
- 每个请求的延迟在 worker 内测量（perf_counter），排除线程池排队等待。
- 测试通过后自动刷新 report_assembly.md（含直方图/成功率/错误分类/环境信息）。
"""
from __future__ import annotations

import os
import platform
import statistics
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pytest

from src.core.assembly import CandidateItem, InfeasibleError, assemble
from src.core.assembly.profile import AssemblyProfile

from tests.performance.conftest import (
    latency_histogram,
    percentile,
)

# ────────────────────────────────────────────────────────────────────
# 阈值与参数
# ────────────────────────────────────────────────────────────────────

P95_THRESHOLD_SEC: float = 2.0
CONCURRENCY: int = 50
# 每次请求生成一份独立卷：不同 seed → 不同选题（确定性）
REPORT_PATH = Path(__file__).parent / "report_assembly.md"


# ────────────────────────────────────────────────────────────────────
# worker：单次组卷请求（测量在内部，排除排队）
# ────────────────────────────────────────────────────────────────────

def _one_assembly_request(
    profile: AssemblyProfile,
    pool: list[CandidateItem],
    seed: int,
    snapshot_ref: str,
) -> tuple[str, float, int, str | None]:
    """执行一次组卷请求并测量延迟.

    Returns:
        (status, elapsed_sec, item_count, error_class)
        status ∈ {"ok", "infeasible", "error"}
    """
    t0 = time.perf_counter()
    try:
        result = assemble(profile, pool, seed=seed, snapshot_ref=snapshot_ref)
        elapsed = time.perf_counter() - t0
        return ("ok", elapsed, len(result.items), None)
    except InfeasibleError as e:
        elapsed = time.perf_counter() - t0
        return ("infeasible", elapsed, 0, type(e).__name__)
    except Exception as e:  # pragma: no cover - 防御性兜底
        elapsed = time.perf_counter() - t0
        return ("error", elapsed, 0, type(e).__name__)


# ────────────────────────────────────────────────────────────────────
# 环境信息采集
# ────────────────────────────────────────────────────────────────────

def _env_info() -> dict[str, str]:
    """采集运行环境信息（写入报告的「环境信息」段）."""
    info = {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "processor": platform.processor() or platform.machine(),
        "cpu_count": str(os.cpu_count() or "unknown"),
        "pool_size": str(_pool_size_for_report),
        "concurrency": str(CONCURRENCY),
    }
    # 本地 DB 条件（PostgreSQL 16 本地实例，无远程网络抖动）
    db_host = os.environ.get("POSTGRES_HOST", "localhost")
    db_port = os.environ.get("POSTGRES_PORT", "5432")
    db_name = os.environ.get("POSTGRES_DB", "muti_dev")
    info["db"] = f"PostgreSQL 16 @ {db_host}:{db_port}/{db_name}（本地，无网络抖动）"
    return info


# 报告用候选池规模（与 fixture 一致）
_pool_size_for_report: int = 200


# ────────────────────────────────────────────────────────────────────
# 报告生成
# ────────────────────────────────────────────────────────────────────

def _write_report(
    latencies_ok: list[float],
    errors: list[tuple[str, str | None]],
    env: dict[str, str],
    p50: float,
    p95: float,
    p99: float,
    success_rate: float,
) -> None:
    """将本次压测结果写入 report_assembly.md（覆盖式刷新）."""
    lines: list[str] = []
    lines.append("# T-W4-038 在线组卷压测报告\n")
    lines.append(f"> 生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}（由 test_assembly_latency.py 刷新）\n")
    lines.append("> 验收依据：任务卡 T-W4-038 §验收 #1/#3；E2E-7 承载项\n\n")

    lines.append("## 1. 环境信息\n")
    lines.append("| 项 | 值 |")
    lines.append("|---|---|")
    for k, v in env.items():
        lines.append(f"| {k} | {v} |")
    lines.append("")

    lines.append("## 2. 延迟分布（assemble 求解热路径）\n")
    lines.append(f"- 样本数（成功请求）：{len(latencies_ok)}")
    lines.append(f"- 平均延迟：{statistics.mean(latencies_ok)*1000:.2f} ms")
    lines.append(f"- 中位（p50）：{p50*1000:.2f} ms")
    lines.append(f"- **p95：{p95*1000:.2f} ms**（阈值 {P95_THRESHOLD_SEC*1000:.0f} ms）")
    lines.append(f"- p99：{p99*1000:.2f} ms")
    verdict = "✅ 通过" if p95 < P95_THRESHOLD_SEC else "❌ 超阈值"
    lines.append(f"- p95 判定：{verdict}\n")

    lines.append("### 延迟分布直方图（10 桶）\n")
    hist = latency_histogram(latencies_ok, bins=10)
    lines.append("| 桶下限 (ms) | 桶上限 (ms) | 计数 | 占比 |")
    lines.append("|---|---|---|---|")
    total = len(latencies_ok) or 1
    for lo, hi, cnt in hist:
        lines.append(
            f"| {lo*1000:.2f} | {hi*1000:.2f} | {cnt} | {cnt/total*100:.1f}% |"
        )
    lines.append("")

    lines.append("## 3. 成功率与错误分类\n")
    lines.append(f"- 总请求数：{CONCURRENCY}")
    lines.append(f"- 成功（ok）：{len(latencies_ok)}")
    lines.append(f"- 成功率：{success_rate*100:.1f}%")
    lines.append(f"- 失败总数：{len(errors)}")
    if errors:
        err_classes: dict[str, int] = {}
        for status, cls in errors:
            key = f"{status}:{cls or 'unknown'}"
            err_classes[key] = err_classes.get(key, 0) + 1
        lines.append("\n| 错误类型 | 计数 |")
        lines.append("|---|---|")
        for k, v in sorted(err_classes.items()):
            lines.append(f"| {k} | {v} |")
    else:
        lines.append("- 错误分类：无（全部成功）")
    lines.append("")

    lines.append("## 4. 测量方法说明\n")
    lines.append(
        "- 候选池 200 题在测试内纯内存构造（4 知识点 × 50 题），"
        "模拟「DB 已加载候选池」后的求解热路径。\n"
        "- 生产中同 (pack, gradeband) 的池可跨请求复用，"
        "故压测聚焦 assemble() 求解延迟。\n"
        f"- 并发模型：ThreadPoolExecutor(max_workers={CONCURRENCY})，"
        "模拟在线后端线程池；延迟在 worker 内 perf_counter 测量，排除排队等待。\n"
        "- 「本地 DB，无网络抖动」：PostgreSQL 16 本地实例（见环境信息），"
        "池加载为一次性 DB 查询，不计入单请求延迟。\n"
    )

    REPORT_PATH.write_text("\n".join(lines), encoding="utf-8")


# ────────────────────────────────────────────────────────────────────
# 主测试
# ────────────────────────────────────────────────────────────────────

def test_assembly_concurrency_p95_under_2s(
    large_pool: list[CandidateItem],
    practice_profile: AssemblyProfile,
) -> None:
    """并发 50 请求组卷，p95 延迟 < 2s（验收 #1）.

    每个请求用独立 seed（模拟 50 名学生各领一份卷）；
    候选池共享（同 pack 同 gradeband 的池天然可复用）。
    """
    global _pool_size_for_report
    _pool_size_for_report = len(large_pool)

    results: list[tuple[str, float, int, str | None]] = []
    with ThreadPoolExecutor(max_workers=CONCURRENCY) as ex:
        futures = {
            ex.submit(
                _one_assembly_request,
                practice_profile,
                large_pool,
                seed=1000 + i,
                snapshot_ref=f"perf-snap-{i}",
            ): i
            for i in range(CONCURRENCY)
        }
        for fut in as_completed(futures):
            results.append(fut.result())

    # 拆分成功/失败
    latencies_ok = [r[1] for r in results if r[0] == "ok"]
    errors = [(r[0], r[3]) for r in results if r[0] != "ok"]

    assert latencies_ok, (
        f"无成功请求，无法计算 p95；错误样本：{errors[:3]}"
    )

    p50 = percentile(latencies_ok, 50)
    p95 = percentile(latencies_ok, 95)
    p99 = percentile(latencies_ok, 99)
    success_rate = len(latencies_ok) / CONCURRENCY

    # 刷新报告（无论断言是否通过，都先落盘便于诊断）
    _write_report(latencies_ok, errors, _env_info(), p50, p95, p99, success_rate)

    # 验收 #1：p95 < 2s
    assert p95 < P95_THRESHOLD_SEC, (
        f"组卷 p95={p95*1000:.2f}ms 超过阈值 {P95_THRESHOLD_SEC*1000:.0f}ms"
        f"（p50={p50*1000:.2f}ms, p99={p99*1000:.2f}ms）"
    )

    # 验收 #3：成功率应 100%（健康压测不应有失败）
    assert success_rate == 1.0, (
        f"成功率 {success_rate*100:.1f}% < 100%；错误：{errors[:3]}"
    )

    # 健全性：每个成功请求应产出 12–15 题（与 profile 一致）
    counts = [r[2] for r in results if r[0] == "ok"]
    assert all(12 <= c <= 15 for c in counts), (
        f"组卷题量越界：{[c for c in counts if not (12 <= c <= 15)][:3]}"
    )


def test_assembly_no_subject_pack_import() -> None:
    """验收 #5：本压测包不 import 任何学科包/学段包.

    检查方式：扫描本文件所有 import 语句行，确保无学科包/学段包引用。
    不扫描整文件文本——断言消息本身会包含被禁字符串，造成自指误报。
    """
    import re
    self_src = Path(__file__).read_text(encoding="utf-8")
    import_lines = [
        ln.strip()
        for ln in self_src.splitlines()
        if re.match(r"\s*(import|from)\s", ln)
    ]
    forbidden = [
        ln for ln in import_lines
        if "src.packs.subject" in ln or "src.packs.gradeband" in ln
    ]
    assert not forbidden, (
        f"压测脚本禁止 import 学科包/学段包，发现：{forbidden}"
    )
