"""T-W4-010 单题全生命周期 AI 成本归集单元测试.

验收对照：
  #1 aggregate_cost(item_revision_id) 返回分阶段成本（起草/验证/评分/重判/其他）
  #2 支持 aggregate_cost_by_dimension 按学科/学段/生产线汇总
  #3 与 T-W4-008 台账一致性：台账总和 = 归集总和
  #4 make accept 全绿
  #5 不 import 学科包；学科维度通过参数注入

测试隔离：用 Ledger(tmp_path) 注入，不污染开发库。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Callable

import pytest

from src.core.ai.cost.item_lifecycle_cost import (
    STAGES,
    aggregate_cost,
    aggregate_cost_by_dimension,
    aggregate_cost_stages_by_dimension,
    total_cost,
)
from src.core.ai.ledger.ledger import Ledger, record_call, set_default_ledger


@pytest.fixture
def isolated_ledger(tmp_path: Path) -> Ledger:
    """每个测试用独立 tmp_path 台账."""
    ledger = Ledger(tmp_path / "ai_ledger.jsonl")
    set_default_ledger(ledger)
    yield ledger
    set_default_ledger(None)


def _seed_lifecycle(ledger: Ledger, item_rev: str) -> dict[str, float]:
    """灌入单题全生命周期 5 阶段调用，返回 {stage: expected_cost}."""
    expected: dict[str, float] = {}
    # draft: deepseek-reasoner, 1000 in / 800 out
    record_call(
        task_level="L2", task_name="draft_passage", provider="deepseek",
        model="deepseek-reasoner", prompt="draft", token_in=1000, token_out=800,
        duration_ms=5000.0, task_stage="draft", artifact_ref=item_rev,
    )
    expected["draft"] = (0.004 * 1000 / 1000 + 0.016 * 800 / 1000)
    # instantiate: deepseek-chat, 200 in / 100 out
    record_call(
        task_level="L1", task_name="instantiate", provider="deepseek",
        model="deepseek-chat", prompt="inst", token_in=200, token_out=100,
        duration_ms=500.0, task_stage="instantiate", artifact_ref=item_rev,
    )
    expected["instantiate"] = (0.001 * 200 / 1000 + 0.002 * 100 / 1000)
    # validate: deepseek-chat, 300 in / 50 out
    record_call(
        task_level="L1", task_name="validate", provider="deepseek",
        model="deepseek-chat", prompt="val", token_in=300, token_out=50,
        duration_ms=300.0, task_stage="validate", artifact_ref=item_rev,
    )
    expected["validate"] = (0.001 * 300 / 1000 + 0.002 * 50 / 1000)
    # score: deepseek-chat, 400 in / 80 out
    record_call(
        task_level="L1", task_name="score", provider="deepseek",
        model="deepseek-chat", prompt="score", token_in=400, token_out=80,
        duration_ms=400.0, task_stage="score", artifact_ref=item_rev,
    )
    expected["score"] = (0.001 * 400 / 1000 + 0.002 * 80 / 1000)
    # rescore: deepseek-reasoner, 400 in / 90 out
    record_call(
        task_level="L2", task_name="rescore", provider="deepseek",
        model="deepseek-reasoner", prompt="rescore", token_in=400, token_out=90,
        duration_ms=600.0, task_stage="rescore", artifact_ref=item_rev,
    )
    expected["rescore"] = (0.004 * 400 / 1000 + 0.016 * 90 / 1000)
    return {k: round(v, 6) for k, v in expected.items()}


# ── 验收 #1：aggregate_cost 分阶段 ─────────────────────────────────

def test_aggregate_cost_by_stage(isolated_ledger: Ledger) -> None:
    """aggregate_cost 返回各阶段成本，与灌入数据逐阶段对齐."""
    item_rev = "item_revision:lifecycle-001"
    expected = _seed_lifecycle(isolated_ledger, item_rev)

    cost = aggregate_cost(item_rev, isolated_ledger)
    # 返回包含全部 STAGES
    assert set(cost.keys()) == set(STAGES)
    # 各阶段成本对齐（draft/instantiate/validate/score/rescore 有值，other=0）
    for stage, expected_val in expected.items():
        assert cost[stage] == pytest.approx(expected_val, rel=1e-6), (
            f"stage={stage} expected={expected_val} actual={cost[stage]}"
        )
    assert cost["other"] == 0.0, "未灌入 other 阶段调用，应为 0"


def test_aggregate_cost_empty_item(isolated_ledger: Ledger) -> None:
    """无调用的 item_revision 返回全 0 成本."""
    cost = aggregate_cost("item_revision:nonexistent", isolated_ledger)
    assert set(cost.keys()) == set(STAGES)
    assert all(v == 0.0 for v in cost.values())


def test_aggregate_cost_unknown_stage_bucketed_to_other(
    isolated_ledger: Ledger,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """task_stage 不在已知阶段时归入 other 桶（兜底，防御 TaskStage 未来扩展）.

    为什么用 monkeypatch：TaskStage 是 Pydantic Literal，写入时即校验，
    真实台账不会有非法 task_stage。兜底逻辑防御未来 TaskStage 扩展而 STAGES
    未同步的场景——用伪造 entry 模拟该场景。
    """
    from types import SimpleNamespace

    # 灌一条合法 other 记录
    record_call(
        task_level="L1", task_name="ad_hoc", provider="deepseek",
        model="deepseek-chat", prompt="p", token_in=100, token_out=50,
        duration_ms=100.0, task_stage="other",
        artifact_ref="item_revision:x",
    )
    # 伪造一条 task_stage="future_stage" 的 entry（绕过 Pydantic 校验）
    fake_entry = SimpleNamespace(task_stage="future_stage", cost_cny=0.05)
    original_query = isolated_ledger.query_by_artifact

    def _mock_query(artifact_ref: str) -> list:
        return original_query(artifact_ref) + [fake_entry]

    monkeypatch.setattr(isolated_ledger, "query_by_artifact", _mock_query)

    cost = aggregate_cost("item_revision:x", isolated_ledger)
    # future_stage 被归入 other
    assert cost["other"] > 0.05  # 至少含伪造的 0.05
    # 已知阶段为 0（除了 other）
    for stage in ("draft", "instantiate", "validate", "score", "rescore"):
        assert cost[stage] == 0.0


# ── 验收 #3：台账总和 = 归集总和 ───────────────────────────────────

def test_total_cost_equals_ledger_sum(isolated_ledger: Ledger) -> None:
    """total_cost（独立求和 ledger）= aggregate_cost 各阶段合计（验收 #3）."""
    item_rev = "item_revision:consistency-001"
    _seed_lifecycle(isolated_ledger, item_rev)

    tc = total_cost(item_rev, isolated_ledger)
    stage_sum = sum(aggregate_cost(item_rev, isolated_ledger).values())
    assert tc == pytest.approx(stage_sum, rel=1e-9), (
        f"total_cost={tc} != stage_sum={stage_sum}"
    )

    # 交叉验证：直接从台账求和
    ledger_entries = isolated_ledger.query_by_artifact(item_rev)
    direct_sum = round(sum(e.cost_cny for e in ledger_entries), 6)
    assert tc == pytest.approx(direct_sum, rel=1e-9)


# ── 验收 #2：按维度（学科/学段/生产线）批量汇总 ────────────────────

def test_aggregate_cost_by_dimension_subject(
    isolated_ledger: Ledger,
) -> None:
    """按学科维度汇总：dimension_extractor 返回学科名."""
    # 数学题 2 道，语文题 1 道
    _seed_lifecycle(isolated_ledger, "item_revision:math-001")
    _seed_lifecycle(isolated_ledger, "item_revision:math-002")
    _seed_lifecycle(isolated_ledger, "item_revision:chinese-001")

    # 调用方注入学科维度提取（本包不 import 学科包，A5）
    subject_of: Callable[[str], str] = lambda ir: "数学" if "math" in ir else "语文"

    totals = aggregate_cost_by_dimension(
        ["item_revision:math-001", "item_revision:math-002",
         "item_revision:chinese-001"],
        subject_of,
        isolated_ledger,
    )
    assert set(totals.keys()) == {"数学", "语文"}
    # 数学 = 2 道题，语文 = 1 道题
    one_item_cost = total_cost("item_revision:math-001", isolated_ledger)
    assert totals["数学"] == pytest.approx(one_item_cost * 2, rel=1e-6)
    assert totals["语文"] == pytest.approx(one_item_cost, rel=1e-6)


def test_aggregate_cost_by_dimension_grade_band(
    isolated_ledger: Ledger,
) -> None:
    """按学段维度汇总：dimension_extractor 返回学段."""
    _seed_lifecycle(isolated_ledger, "item_revision:low-001")
    _seed_lifecycle(isolated_ledger, "item_revision:high-001")
    _seed_lifecycle(isolated_ledger, "item_revision:high-002")

    grade_of: Callable[[str], str] = lambda ir: "L" if "low" in ir else "H"
    totals = aggregate_cost_by_dimension(
        ["item_revision:low-001", "item_revision:high-001",
         "item_revision:high-002"],
        grade_of,
        isolated_ledger,
    )
    one_cost = total_cost("item_revision:low-001", isolated_ledger)
    assert totals["L"] == pytest.approx(one_cost, rel=1e-6)
    assert totals["H"] == pytest.approx(one_cost * 2, rel=1e-6)


def test_aggregate_cost_by_dimension_pipeline(
    isolated_ledger: Ledger,
) -> None:
    """按生产线维度汇总：C/D 线."""
    _seed_lifecycle(isolated_ledger, "item_revision:C-001")
    _seed_lifecycle(isolated_ledger, "item_revision:D-001")
    _seed_lifecycle(isolated_ledger, "item_revision:D-002")

    # 维度提取：item_revision:C-001 → "C"（按 ":<字母>-" 模式提取生产线标记）
    pipeline_of: Callable[[str], str] = lambda ir: "C" if ":C-" in ir else "D"
    totals = aggregate_cost_by_dimension(
        ["item_revision:C-001", "item_revision:D-001", "item_revision:D-002"],
        pipeline_of,
        isolated_ledger,
    )
    one_cost = total_cost("item_revision:C-001", isolated_ledger)
    assert totals["C"] == pytest.approx(one_cost, rel=1e-6)
    assert totals["D"] == pytest.approx(one_cost * 2, rel=1e-6)


def test_aggregate_cost_stages_by_dimension_matrix(
    isolated_ledger: Ledger,
) -> None:
    """维度 × 阶段 矩阵汇总：每个学科下各阶段成本明细."""
    _seed_lifecycle(isolated_ledger, "item_revision:math-001")
    _seed_lifecycle(isolated_ledger, "item_revision:chinese-001")

    subject_of: Callable[[str], str] = lambda ir: "数学" if "math" in ir else "语文"
    matrix = aggregate_cost_stages_by_dimension(
        ["item_revision:math-001", "item_revision:chinese-001"],
        subject_of,
        isolated_ledger,
    )
    assert set(matrix.keys()) == {"数学", "语文"}
    for subject in ("数学", "语文"):
        assert set(matrix[subject].keys()) == set(STAGES)
        # 每个学科各阶段成本 = 单题各阶段成本
        single = aggregate_cost("item_revision:math-001", isolated_ledger)
        if subject == "数学":
            for stage in STAGES:
                assert matrix["数学"][stage] == pytest.approx(single[stage], rel=1e-6)


# ── 验收 #5：不 import 学科包/学段包 ───────────────────────────────

def test_no_subject_pack_imports_in_cost() -> None:
    """src/core/ai/cost/ 禁止 import 学科包/学段包（宪法 A5/X6）."""
    cost_dir = (
        Path(__file__).resolve().parent.parent.parent
        / "src"
        / "core"
        / "ai"
        / "cost"
    )
    assert cost_dir.is_dir()
    pattern = re.compile(
        r"^\s*(?:from\s+(?:packs|src\.packs)"
        r"|import\s+(?:packs|src\.packs))",
        re.MULTILINE,
    )
    violations: list[str] = []
    for py_file in sorted(cost_dir.rglob("*.py")):
        text = py_file.read_text(encoding="utf-8")
        if pattern.findall(text):
            violations.append(str(py_file.relative_to(cost_dir)))
    assert not violations, f"ai/cost 存在学科包 import（违反 A5）：{violations}"


# ── STAGES 常量完整性 ─────────────────────────────────────────────

def test_stages_constant() -> None:
    """STAGES 覆盖六阶段，other 在最后（兜底）."""
    assert STAGES == ("draft", "instantiate", "validate", "score", "rescore", "other")
    assert STAGES[-1] == "other", "other 必须在最后（兜底桶）"
