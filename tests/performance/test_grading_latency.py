"""T-W4-039 客观题批改 10s 级链路压测.

验收（任务卡 T-W4-039 §验收）：
1. 模拟 100 次客观题批改，平均延迟 <10s，p95<15s（本地环境，含 DB 写入）。
2. 覆盖至少 3 种交互类型（单选/数值填空/匹配连线）。
3. 报告含：延迟分布、评分准确率（与期望对比）、DB 写入耗时拆分。
4. make accept TASK=T-W4-039 全绿；E2E-7 承载项。
5. 不 import 任何学科包/学段包。

链路（架构 v2 §4.5 评分域在线链路）：
  response → run_scorer（注册表 exact_match）→ infer_option_errors
  → build_scoring_trace → record_event（response_event append-only 落账）

设计说明：
- 100 次批改 = 33 单选 + 34 数值填空 + 33 匹配连线（轮转）。
- 每次 score_and_record 经 async_session 真实写 response_event
  （事务回滚隔离：INSERT 真实执行测延迟，但不持久化污染 DB）。
- 延迟拆分：总延迟 = 评分器计算（run_scorer，无 DB）+ DB 写入（record_event + 编排）。
- 阈值 10s/15s 极宽松（客观题评分微秒级，DB 写入毫秒级）——为慢 CI 留余量。
"""
from __future__ import annotations

import os
import platform
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

# 导入 platform_scorers 触发注册（exact_match/keypoint_hit/stepwise_rubric）
import src.core.scoring.platform_scorers  # noqa: F401
from src.core.scoring.service import run_scorer, score_and_record
from src.core.scoring.registry import ScoreResult
from sqlalchemy.ext.asyncio import AsyncSession

from tests.performance.conftest import latency_histogram, percentile

# ────────────────────────────────────────────────────────────────────
# 阈值与参数
# ────────────────────────────────────────────────────────────────────

AVG_THRESHOLD_SEC: float = 10.0
P95_THRESHOLD_SEC: float = 15.0
TOTAL_CALLS: int = 100
REPORT_PATH = Path(__file__).parent / "report_grading.md"

# 三种交互类型轮转
INTERACTIONS: list[str] = ["single_choice", "numeric_blank", "matching"]


# ────────────────────────────────────────────────────────────────────
# 构造 item_version 与 response（3 种交互类型）
# ────────────────────────────────────────────────────────────────────

def _build_item_version(interaction: str, idx: int) -> dict[str, Any]:
    """构造 item_version dict（scoring_ref 指向 exact_match）."""
    vid = f"perf-{interaction}-{idx:03d}"
    if interaction == "single_choice":
        answer = "A"
        error_bindings = [
            {"option_value": "B", "error_type_id": "err.b", "label": "多1"},
            {"option_value": "C", "error_type_id": "err.c", "label": "少1"},
        ]
    elif interaction == "numeric_blank":
        answer = {"b1": 42, "b2": 100}
        error_bindings = []
    else:  # matching
        answer = {"l1": "r1", "l2": "r2", "l3": "r3"}
        error_bindings = []
    return {
        "item_version_id": vid,
        "item_id": f"item-{vid}",
        "status": "published",
        "objective": {"gradeband": "M", "kp_set": [{"code": "perf.kp"}]},
        "interaction_ref": {"interaction_id": interaction},
        "scoring_ref": {
            "scorer_id": "exact_match",
            "scorer_params": {"answer": answer},
        },
        "error_bindings": error_bindings,
        "content": {"blocks": []},
    }


def _build_response(interaction: str, idx: int, correct: bool) -> dict[str, Any]:
    """构造作答载荷（correct=True 给正解，False 给错答）."""
    if interaction == "single_choice":
        return {"selected": "A" if correct else "B"}
    if interaction == "numeric_blank":
        if correct:
            return {"blanks": {"b1": 42, "b2": 100}}
        return {"blanks": {"b1": 0, "b2": 1}}
    # matching
    if correct:
        return {"pairs": [
            {"left_id": "l1", "right_id": "r1"},
            {"left_id": "l2", "right_id": "r2"},
            {"left_id": "l3", "right_id": "r3"},
        ]}
    return {"pairs": [
        {"left_id": "l1", "right_id": "r2"},  # 错配
        {"left_id": "l2", "right_id": "r1"},
        {"left_id": "l3", "right_id": "r3"},
    ]}


def _build_grading_batch() -> list[tuple[dict, dict, bool, str]]:
    """构造 100 次批改任务：(item_version, response, expected_correct, interaction)."""
    batch = []
    for i in range(TOTAL_CALLS):
        interaction = INTERACTIONS[i % 3]
        # 每 3 题一组：2 对 1 错（覆盖正误两种路径）
        correct = (i % 3) != 2
        iv = _build_item_version(interaction, i)
        resp = _build_response(interaction, i, correct)
        batch.append((iv, resp, correct, interaction))
    return batch


# ────────────────────────────────────────────────────────────────────
# 环境信息
# ────────────────────────────────────────────────────────────────────

def _env_info() -> dict[str, str]:
    db_host = os.environ.get("POSTGRES_HOST", "localhost")
    db_port = os.environ.get("POSTGRES_PORT", "5432")
    db_name = os.environ.get("POSTGRES_DB", "muti_dev")
    return {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "processor": platform.processor() or platform.machine(),
        "cpu_count": str(os.cpu_count() or "unknown"),
        "total_calls": str(TOTAL_CALLS),
        "interactions": "/".join(INTERACTIONS),
        "db": f"PostgreSQL 16 @ {db_host}:{db_port}/{db_name}（本地，含 DB 写入）",
    }


# ────────────────────────────────────────────────────────────────────
# 报告生成
# ────────────────────────────────────────────────────────────────────

def _write_report(
    total_latencies: list[float],
    scorer_latencies: list[float],
    db_latencies: list[float],
    by_interaction: dict[str, list[float]],
    accuracy: float,
    mismatches: list[dict],
    env: dict[str, str],
    avg: float,
    p95: float,
) -> None:
    """写入 report_grading.md."""
    lines: list[str] = []
    lines.append("# T-W4-039 客观题批改链路压测报告\n")
    lines.append(f"> 生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}（由 test_grading_latency.py 刷新）\n")
    lines.append("> 验收依据：任务卡 T-W4-039 §验收 #1/#2/#3；E2E-7 承载项\n\n")

    lines.append("## 1. 环境信息\n")
    lines.append("| 项 | 值 |")
    lines.append("|---|---|")
    for k, v in env.items():
        lines.append(f"| {k} | {v} |")
    lines.append("")

    lines.append("## 2. 延迟分布（含 DB 写入的完整批改链路）\n")
    lines.append(f"- 样本数：{len(total_latencies)}")
    lines.append(f"- 平均延迟：{avg*1000:.2f} ms（阈值 {AVG_THRESHOLD_SEC*1000:.0f} ms）")
    lines.append(f"- **p95：{p95*1000:.2f} ms**（阈值 {P95_THRESHOLD_SEC*1000:.0f} ms）")
    v_avg = "✅ 通过" if avg < AVG_THRESHOLD_SEC else "❌ 超阈值"
    v_p95 = "✅ 通过" if p95 < P95_THRESHOLD_SEC else "❌ 超阈值"
    lines.append(f"- 平均判定：{v_avg}")
    lines.append(f"- p95 判定：{v_p95}\n")

    lines.append("### 延迟分布直方图（10 桶）\n")
    hist = latency_histogram(total_latencies, bins=10)
    lines.append("| 桶下限 (ms) | 桶上限 (ms) | 计数 | 占比 |")
    lines.append("|---|---|---|---|")
    total = len(total_latencies) or 1
    for lo, hi, cnt in hist:
        lines.append(f"| {lo*1000:.2f} | {hi*1000:.2f} | {cnt} | {cnt/total*100:.1f}% |")
    lines.append("")

    lines.append("## 3. DB 写入耗时拆分\n")
    lines.append(f"- 评分器计算（run_scorer，无 DB）平均：{statistics.mean(scorer_latencies)*1000:.3f} ms")
    lines.append(f"- DB 写入 + 落账编排平均：{statistics.mean(db_latencies)*1000:.3f} ms")
    lines.append(f"- 总链路平均：{avg*1000:.3f} ms")
    s_pct = (statistics.mean(scorer_latencies) / avg * 100) if avg else 0
    d_pct = (statistics.mean(db_latencies) / avg * 100) if avg else 0
    lines.append(f"- 占比：评分器 {s_pct:.1f}% / DB+编排 {d_pct:.1f}%\n")

    lines.append("### 按交互类型拆分\n")
    lines.append("| 交互类型 | 样本数 | 平均 (ms) | p95 (ms) |")
    lines.append("|---|---|---|---|")
    for interaction, lats in by_interaction.items():
        lines.append(
            f"| {interaction} | {len(lats)} | "
            f"{statistics.mean(lats)*1000:.3f} | {percentile(lats, 95)*1000:.3f} |"
        )
    lines.append("")

    lines.append("## 4. 评分准确率（与期望对比）\n")
    lines.append(f"- 总批改数：{TOTAL_CALLS}")
    lines.append(f"- 评分与期望一致数：{int(accuracy * TOTAL_CALLS)}")
    lines.append(f"- **准确率：{accuracy*100:.1f}%**")
    if mismatches:
        lines.append(f"\n### 不一致样本（前 5）\n")
        lines.append("| 序号 | 交互 | 期望 | 实际 |")
        lines.append("|---|---|---|---|")
        for m in mismatches[:5]:
            lines.append(f"| {m['idx']} | {m['interaction']} | {m['expected']} | {m['actual']} |")
    else:
        lines.append("- 不一致样本：无（评分器 100% 准确）")
    lines.append("")

    lines.append("## 5. 测量方法说明\n")
    lines.append(
        "- 100 次批改：33 单选 + 34 数值填空 + 33 匹配连线（轮转）。\n"
        "- 每次调用 score_and_record：run_scorer（exact_match）→ infer_option_errors "
        "→ build_scoring_trace → record_event（response_event INSERT）。\n"
        "- DB 写入经 async_session 事务回滚隔离：INSERT 真实执行（测延迟），"
        "测试结束回滚不污染 DB。\n"
        "- 延迟拆分：总延迟 - run_scorer 单独延迟 = DB 写入 + 落账编排。\n"
        "- 阈值 10s/15s 极宽松：客观题评分微秒级、DB 写入毫秒级，为慢 CI 留余量。\n"
    )

    REPORT_PATH.write_text("\n".join(lines), encoding="utf-8")


# ────────────────────────────────────────────────────────────────────
# 主测试
# ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_grading_100_calls_latency(async_session: AsyncSession) -> None:
    """验收 #1/#2/#3：100 次批改，avg<10s p95<15s，3 种交互，含 DB 写入."""
    batch = _build_grading_batch()
    assert len(batch) == TOTAL_CALLS

    total_latencies: list[float] = []
    scorer_latencies: list[float] = []
    db_latencies: list[float] = []
    by_interaction: dict[str, list[float]] = {iv: [] for iv in INTERACTIONS}
    mismatches: list[dict] = []

    student_alias_id = uuid4()
    session_id = uuid4()
    now = datetime.now(timezone.utc)

    for idx, (item_version, response, expected_correct, interaction) in enumerate(batch):
        # 1) 单独测 run_scorer（无 DB）——延迟拆分的「评分器计算」分量
        t_scorer0 = time.perf_counter()
        result: ScoreResult = run_scorer(item_version, response)
        scorer_elapsed = time.perf_counter() - t_scorer0

        # 2) 完整 score_and_record（含 DB 写入）——总延迟
        t_total0 = time.perf_counter()
        outcome = await score_and_record(
            async_session,
            item_version=item_version,
            response=response,
            student_alias_id=student_alias_id,
            scene="practice",
            pack_id=None,  # platform 桶
            duration_ms=5000,
            session_id=session_id,
            now=now,
        )
        total_elapsed = time.perf_counter() - t_total0

        total_latencies.append(total_elapsed)
        scorer_latencies.append(scorer_elapsed)
        db_latencies.append(max(0.0, total_elapsed - scorer_elapsed))
        by_interaction[interaction].append(total_elapsed)

        # 3) 评分准确率：outcome.correct 应与 expected_correct 一致
        if outcome.correct != expected_correct:
            mismatches.append({
                "idx": idx,
                "interaction": interaction,
                "expected": expected_correct,
                "actual": outcome.correct,
            })

    avg = statistics.mean(total_latencies)
    p95 = percentile(total_latencies, 95)
    accuracy = 1.0 - len(mismatches) / TOTAL_CALLS

    # 刷新报告（断言前落盘便于诊断）
    _write_report(
        total_latencies, scorer_latencies, db_latencies,
        by_interaction, accuracy, mismatches, _env_info(), avg, p95,
    )

    # 验收 #1：avg < 10s, p95 < 15s
    assert avg < AVG_THRESHOLD_SEC, (
        f"批改平均延迟 {avg*1000:.2f}ms 超阈值 {AVG_THRESHOLD_SEC*1000:.0f}ms"
    )
    assert p95 < P95_THRESHOLD_SEC, (
        f"批改 p95={p95*1000:.2f}ms 超阈值 {P95_THRESHOLD_SEC*1000:.0f}ms"
    )

    # 验收 #2：3 种交互类型均覆盖
    for iv in INTERACTIONS:
        assert len(by_interaction[iv]) > 0, f"交互类型 {iv} 未被覆盖"

    # 验收 #3：评分准确率应 100%（确定性评分器）
    assert accuracy == 1.0, (
        f"评分准确率 {accuracy*100:.1f}% < 100%；不一致：{mismatches[:3]}"
    )


def test_grading_no_subject_pack_import() -> None:
    """验收 #5：本压测包不 import 任何学科包/学段包."""
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
