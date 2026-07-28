"""T-W4-028 CP-SAT 离线组卷求解器单元测试.

对照任务卡验收标准（逐条可执行）：
1. solve(spec_table, candidate_pool, seed=0) 返回题卷列表或 CpSatInfeasible
   （含冲突约束列表）
2. 可行案例：8 单元格共 12 题，求解成功且每单元格题数与难度合规
3. 不可行案例：题池不足或约束冲突时返回明确冲突原因
4. make accept TASK=T-W4-028 全绿；E2E-5 承载卡（验证卡 T-W4-T03 走完整链路）
5. 不 import 任何学科包/学段包

确定性（R-Z-01）：同 (spec_table, candidate_pool, seed) → 同选题同序（用 selection_digest 比对）。
"""
from __future__ import annotations

from pathlib import Path
import re

import pytest

from src.core.assembly.spec_table import SpecCell, SpecTable
from src.core.assembly.solver.constraints import (
    CpSatInfeasible,
    MeasurementCandidate,
    measurement_candidate_from_serving_row,
)
from src.core.assembly.solver.cpsat_solver import CpSatSolution, solve


# ────────────────────────────────────────────────────────────────────
# 构造辅助
# ────────────────────────────────────────────────────────────────────

def _cand(
    vid: str,
    kp: str,
    cognitive: str,
    p: float,
    *,
    group: str | None = None,
    tpl: str | None = None,
) -> MeasurementCandidate:
    return MeasurementCandidate(
        item_version_id=vid,
        kp_codes=[kp],
        cognitive_level=cognitive,
        p_correct=p,
        group_id=group,
        template_version_id=tpl if tpl is not None else f"tpl-{vid}",
    )


def _cell(
    kp: str,
    cognitive: str,
    count: int,
    *,
    dmin: float = 0.30,
    dmax: float = 0.80,
) -> dict:
    return {
        "content_code": kp,
        "cognitive_level": cognitive,
        "target_count": count,
        "difficulty_min": dmin,
        "difficulty_max": dmax,
    }


def _spec_table_dict_8cells() -> dict:
    """验收 #2：8 单元格共 12 题（4 知识点 × 2 认知层级 = 8 cell）.

    任务卡原文「2 知识点×2 认知层级×难度配比 = 8 单元格共 12 题」——
    实际 schema 中 cell 维度 = (content_code, cognitive_level)，每个 cell 自带
    difficulty 区间。8 cell 由 4 kp × 2 cognitive 构成，12 题 = 8 cell 中
    4 个 cell × 2 题 + 4 个 cell × 1 题。
    """
    cells = [
        # 4 kp × 2 cognitive = 8 cells
        _cell("math.a", "remember", count=2, dmin=0.50, dmax=0.80),
        _cell("math.a", "apply", count=1, dmin=0.30, dmax=0.60),
        _cell("math.b", "remember", count=2, dmin=0.50, dmax=0.80),
        _cell("math.b", "apply", count=1, dmin=0.30, dmax=0.60),
        _cell("math.c", "remember", count=2, dmin=0.50, dmax=0.80),
        _cell("math.c", "apply", count=1, dmin=0.30, dmax=0.60),
        _cell("math.d", "remember", count=2, dmin=0.50, dmax=0.80),
        _cell("math.d", "apply", count=1, dmin=0.30, dmax=0.60),
    ]
    return {
        "spec_table_id": "spec-cpsat-test",
        "spec_table_version": "1.0.0",
        "gradeband": "M",
        "graph_release": "graph-math-2026q1",
        "cells": cells,
    }


def _feasible_pool_8cells() -> list[MeasurementCandidate]:
    """构造足够大的候选池满足 _spec_table_dict_8cells 的 12 题需求.

    每个 cell 给 3 个合格候选（target_count 最多 2，留 1 个余量），
    共 8 cell × 3 = 24 候选。
    """
    pool: list[MeasurementCandidate] = []
    # 为每 cell 准备 3 个合格候选（p_correct 落在 [dmin, dmax]）
    cells = _spec_table_dict_8cells()["cells"]
    for idx, cell in enumerate(cells):
        mid = (cell["difficulty_min"] + cell["difficulty_max"]) / 2
        # 3 个候选 p_correct 略偏移 mid，确保都在区间内
        for j in range(3):
            p = max(0.0, min(1.0, mid + 0.02 * (j - 1)))
            pool.append(_cand(
                f"item-{idx}-{j}",
                cell["content_code"],
                cell["cognitive_level"],
                p,
            ))
    return pool


# ────────────────────────────────────────────────────────────────────
# 验收 #1：solve 返回类型契约
# ────────────────────────────────────────────────────────────────────

def test_solve_returns_solution_on_feasible_case():
    """可行案例：返回 CpSatSolution（含入选候选列表）."""
    st = SpecTable(**_spec_table_dict_8cells())
    pool = _feasible_pool_8cells()
    result = solve(st, pool, seed=0)
    assert isinstance(result, CpSatSolution)
    assert result.is_feasible is True
    assert len(result.selected) == st.total_count == 12


def test_solve_returns_infeasible_on_pool_shortage():
    """不可行案例：题池不足返回 CpSatInfeasible（含冲突约束列表）."""
    st = SpecTable(**_spec_table_dict_8cells())
    # 候选池只够 6 题（不足 12 题）
    pool = _feasible_pool_8cells()[:6]
    result = solve(st, pool, seed=0)
    assert isinstance(result, CpSatInfeasible)
    assert len(result.conflicts) >= 1
    assert result.candidate_pool_size == 6
    assert result.spec_table_total_count == 12
    # 至少一条冲突是 cell_quota 类（含 required/available）
    quota_conflicts = [c for c in result.conflicts if c.constraint_id == "cell_quota"]
    assert len(quota_conflicts) >= 1
    qc = quota_conflicts[0]
    assert qc.required is not None and qc.available is not None
    assert qc.required > qc.available
    assert qc.cell_content_code is not None
    assert qc.cell_cognitive_level is not None


# ────────────────────────────────────────────────────────────────────
# 验收 #2：可行案例 — 8 单元格共 12 题，每 cell 题数与难度合规
# ────────────────────────────────────────────────────────────────────

def test_feasible_solution_satisfies_each_cell_count():
    """每 cell 入选题数 == target_count."""
    st = SpecTable(**_spec_table_dict_8cells())
    pool = _feasible_pool_8cells()
    result = solve(st, pool, seed=0)
    assert isinstance(result, CpSatSolution)

    # 按 cell 统计入选数
    cell_counts: dict[tuple[str, str], int] = {}
    for cell_key_str, vids in result.cell_assignment.items():
        kp, cognitive = cell_key_str.split("/", 1)
        cell_counts[(kp, cognitive)] = len(vids)

    for cell in st.cells:
        key = (cell.content_code, cell.cognitive_level)
        assert cell_counts.get(key, 0) == cell.target_count, (
            f"cell {key} 入选 {cell_counts.get(key, 0)} 题，"
            f"target_count={cell.target_count}"
        )


def test_feasible_solution_satisfies_difficulty_range():
    """每 cell 入选候选的 p_correct ∈ [difficulty_min, difficulty_max]."""
    st = SpecTable(**_spec_table_dict_8cells())
    pool = _feasible_pool_8cells()
    # 构造 item → p_correct 映射
    p_map = {c.item_version_id: c.p_correct for c in pool}

    result = solve(st, pool, seed=0)
    assert isinstance(result, CpSatSolution)

    for cell in st.cells:
        cell_key = f"{cell.content_code}/{cell.cognitive_level}"
        for vid in result.cell_assignment.get(cell_key, []):
            p = p_map[vid]
            assert cell.difficulty_min <= p <= cell.difficulty_max, (
                f"候选 {vid} p_correct={p} 不在 cell {cell_key} 区间 "
                f"[{cell.difficulty_min}, {cell.difficulty_max}]"
            )


def test_feasible_solution_total_count_matches():
    """入选总数 == spec_table.total_count（12）."""
    st = SpecTable(**_spec_table_dict_8cells())
    pool = _feasible_pool_8cells()
    result = solve(st, pool, seed=0)
    assert isinstance(result, CpSatSolution)
    assert len(result.selected) == st.total_count
    # cell_assignment 中所有 vids 之和 == selected 长度
    total_assigned = sum(len(v) for v in result.cell_assignment.values())
    assert total_assigned == len(result.selected) == st.total_count


# ────────────────────────────────────────────────────────────────────
# 验收 #3：不可行案例 — 明确冲突原因
# ────────────────────────────────────────────────────────────────────

def test_infeasible_due_to_specific_cell_shortage():
    """特定 cell 候选不足：冲突原因含 cell 定位与缺额."""
    st = SpecTable(**_spec_table_dict_8cells())
    pool = _feasible_pool_8cells()
    # 删除 cell math.a/remember 的全部 3 个合格候选 → 该 cell 缺额
    pool = [c for c in pool if not (
        "math.a" in c.kp_codes and c.cognitive_level == "remember"
    )]
    result = solve(st, pool, seed=0)
    assert isinstance(result, CpSatInfeasible)
    # 应有 math.a/remember 的冲突条目
    conflict = next(
        (c for c in result.conflicts
         if c.cell_content_code == "math.a"
         and c.cell_cognitive_level == "remember"),
        None,
    )
    assert conflict is not None
    assert conflict.required == 2
    assert conflict.available == 0
    assert "math.a/remember" in conflict.detail or "math.a" in conflict.detail


def test_infeasible_due_to_difficulty_band_mismatch():
    """候选 p_correct 不在 cell 难度区间 → 该 cell 缺额."""
    # 构造 1 个 cell，候选 p_correct 全部偏离区间
    cells = [_cell("math.x", "apply", count=2, dmin=0.70, dmax=0.90)]
    st = SpecTable(
        spec_table_id="spec-diff-mismatch",
        spec_table_version="1.0.0",
        gradeband="M",
        graph_release="g",
        cells=cells,
    )
    # 5 个候选 p_correct 都低于 0.70（不合规）
    pool = [_cand(f"v{i}", "math.x", "apply", p=0.30 + 0.05 * i) for i in range(5)]
    result = solve(st, pool, seed=0)
    assert isinstance(result, CpSatInfeasible)
    conflict = result.conflicts[0]
    assert conflict.cell_content_code == "math.x"
    assert conflict.required == 2
    assert conflict.available == 0  # 没有候选 p_correct ∈ [0.70, 0.90]


def test_infeasible_due_to_exposure_mutex():
    """曝光互斥：合格候选全部被 excluded → cell 缺额."""
    cells = [_cell("math.y", "apply", count=2, dmin=0.40, dmax=0.60)]
    st = SpecTable(
        spec_table_id="spec-exposure",
        spec_table_version="1.0.0",
        gradeband="M",
        graph_release="g",
        cells=cells,
    )
    pool = [_cand(f"v{i}", "math.y", "apply", p=0.50) for i in range(3)]
    # 全部 item_version_id 排除
    excluded = frozenset(c.item_version_id for c in pool)
    result = solve(st, pool, seed=0, excluded_item_version_ids=excluded)
    assert isinstance(result, CpSatInfeasible)
    assert result.conflicts[0].available == 0
    assert result.conflicts[0].required == 2


def test_infeasible_summary_human_readable():
    """CpSatInfeasible.summary() 返回人类可读摘要（错误信息/日志用）."""
    st = SpecTable(**_spec_table_dict_8cells())
    pool = _feasible_pool_8cells()[:3]
    result = solve(st, pool, seed=0)
    assert isinstance(result, CpSatInfeasible)
    s = result.summary()
    assert "CP-SAT 不可行" in s
    assert "条冲突" in s


# ────────────────────────────────────────────────────────────────────
# 确定性（R-Z-01）：同输入同种子同输出
# ────────────────────────────────────────────────────────────────────

def test_determinism_same_seed_same_output():
    """同 (spec_table, candidate_pool, seed) → 同 selection_digest."""
    st = SpecTable(**_spec_table_dict_8cells())
    pool = _feasible_pool_8cells()
    r1 = solve(st, pool, seed=42)
    r2 = solve(st, pool, seed=42)
    assert isinstance(r1, CpSatSolution)
    assert isinstance(r2, CpSatSolution)
    assert r1.selection_digest == r2.selection_digest
    # 入选集合相同（顺序按 cell 分组，应一致）
    assert (
        sorted(c.item_version_id for c in r1.selected)
        == sorted(c.item_version_id for c in r2.selected)
    )


def test_determinism_different_seed_may_differ():
    """不同 seed 可能产生不同选题（CP-SAT 多解时）——非强制，但验证 seed 生效.

    本测试构造多解场景：候选池有冗余（每 cell 3 候选，target_count ≤ 2），
    不同 seed 应有机会选不同子集。若 CP-SAT 恰好同解也不算失败——
    此处仅验证两种 seed 都给出可行解。
    """
    st = SpecTable(**_spec_table_dict_8cells())
    pool = _feasible_pool_8cells()
    r1 = solve(st, pool, seed=1)
    r2 = solve(st, pool, seed=999)
    assert isinstance(r1, CpSatSolution)
    assert isinstance(r2, CpSatSolution)
    assert r1.seed == 1
    assert r2.seed == 999


# ────────────────────────────────────────────────────────────────────
# 题组（testlet）整体入选约束
# ────────────────────────────────────────────────────────────────────

def test_group_integrity_all_or_none():
    """同 group_id 候选要么全入选要么全不入选（R-Z-06 testlet 语义）."""
    # 1 cell 需 2 题；候选池：2 个独立候选 + 1 个 2 题题组
    cells = [_cell("math.g", "apply", count=2, dmin=0.40, dmax=0.60)]
    st = SpecTable(
        spec_table_id="spec-group",
        spec_table_version="1.0.0",
        gradeband="M",
        graph_release="g",
        cells=cells,
    )
    pool = [
        _cand("single-1", "math.g", "apply", p=0.50),
        _cand("single-2", "math.g", "apply", p=0.55),
        _cand("grp-a", "math.g", "apply", p=0.45, group="G1"),
        _cand("grp-b", "math.g", "apply", p=0.55, group="G1"),
    ]
    result = solve(st, pool, seed=0)
    assert isinstance(result, CpSatSolution)
    selected_ids = {c.item_version_id for c in result.selected}
    # 题组 G1 要么全入选要么全不入选
    g1_in = {"grp-a", "grp-b"} & selected_ids
    assert g1_in in (set(), {"grp-a", "grp-b"}), (
        f"题组 G1 必须整体入选/排除，实际入选：{g1_in}"
    )


def test_group_too_large_for_quota_infeasible():
    """题组整体入选约束导致 cell 配额不可达 → 不可行."""
    # 1 cell 需 2 题；候选池只有 1 个 3 题题组（需整体入选，但配额只 2）
    cells = [_cell("math.h", "apply", count=2, dmin=0.40, dmax=0.60)]
    st = SpecTable(
        spec_table_id="spec-group-infeasible",
        spec_table_version="1.0.0",
        gradeband="M",
        graph_release="g",
        cells=cells,
    )
    pool = [
        _cand("grp-1", "math.h", "apply", p=0.50, group="G1"),
        _cand("grp-2", "math.h", "apply", p=0.50, group="G1"),
        _cand("grp-3", "math.h", "apply", p=0.50, group="G1"),
    ]
    result = solve(st, pool, seed=0)
    # 题组 3 题 == cell target 2 题 → 配额无法满足（题组整体入选 = 3 题 > 2）
    assert isinstance(result, CpSatInfeasible)


# ────────────────────────────────────────────────────────────────────
# measurement_candidate_from_serving_row：从 serving 行构造候选
# ────────────────────────────────────────────────────────────────────

def test_measurement_candidate_from_serving_row():
    """从 v_serving_item_version 行 dict 构造 MeasurementCandidate."""
    row = {
        "item_version_id": "iv-1",
        "template_version_id": "tv-1",
        "objective": {
            "kp_set": [{"dimension": "kp", "code": "math.z"}],
            "cognitive_level": "apply",
            "gradeband": "M",
            "graph_release": "g",
        },
        "lineage": {
            "params": {
                "p_correct_prior": 0.55,
                "group_id": "G1",
            },
        },
    }
    c = measurement_candidate_from_serving_row(row)
    assert c.item_version_id == "iv-1"
    assert c.kp_codes == ["math.z"]
    assert c.cognitive_level == "apply"
    assert c.p_correct == 0.55
    assert c.group_id == "G1"
    assert c.template_version_id == "tv-1"


def test_measurement_candidate_rejects_missing_cognitive_level():
    """缺 cognitive_level → ValueError."""
    row = {
        "item_version_id": "iv-2",
        "objective": {
            "kp_set": [{"dimension": "kp", "code": "math.z"}],
            # 缺 cognitive_level
            "gradeband": "M",
            "graph_release": "g",
        },
        "lineage": {"params": {"p_correct_prior": 0.55}},
    }
    with pytest.raises(ValueError, match="cognitive_level"):
        measurement_candidate_from_serving_row(row)


def test_measurement_candidate_rejects_missing_p_correct():
    """缺 p_correct_prior → ValueError."""
    row = {
        "item_version_id": "iv-3",
        "objective": {
            "kp_set": [{"dimension": "kp", "code": "math.z"}],
            "cognitive_level": "apply",
            "gradeband": "M",
            "graph_release": "g",
        },
        "lineage": {"params": {}},  # 缺 p_correct_prior
    }
    with pytest.raises(ValueError, match="p_correct_prior"):
        measurement_candidate_from_serving_row(row)


def test_measurement_candidate_rejects_invalid_cognitive_level():
    """cognitive_level 越域 → ValueError."""
    row = {
        "item_version_id": "iv-4",
        "objective": {
            "kp_set": [{"dimension": "kp", "code": "math.z"}],
            "cognitive_level": "synthesis",  # 旧版 Bloom 名
            "gradeband": "M",
            "graph_release": "g",
        },
        "lineage": {"params": {"p_correct_prior": 0.5}},
    }
    with pytest.raises(ValueError, match="越域"):
        measurement_candidate_from_serving_row(row)


# ────────────────────────────────────────────────────────────────────
# 边界场景
# ────────────────────────────────────────────────────────────────────

def test_solve_empty_pool_with_nonzero_quota_infeasible():
    """空候选池 + 非零 target_count → 不可行（全部 cell 缺额）."""
    cells = [_cell("math.e", "apply", count=1, dmin=0.40, dmax=0.60)]
    st = SpecTable(
        spec_table_id="spec-empty",
        spec_table_version="1.0.0",
        gradeband="M",
        graph_release="g",
        cells=cells,
    )
    result = solve(st, [], seed=0)
    assert isinstance(result, CpSatInfeasible)
    assert result.candidate_pool_size == 0
    assert result.conflicts[0].available == 0


def test_solve_with_template_exposure_mutex():
    """template_version_id 曝光互斥：同母题不同卷."""
    cells = [_cell("math.f", "apply", count=1, dmin=0.40, dmax=0.60)]
    st = SpecTable(
        spec_table_id="spec-template-excl",
        spec_table_version="1.0.0",
        gradeband="M",
        graph_release="g",
        cells=cells,
    )
    # 2 个候选同 template，1 个不同 template
    pool = [
        _cand("v1", "math.f", "apply", p=0.50, tpl="tpl-A"),
        _cand("v2", "math.f", "apply", p=0.55, tpl="tpl-A"),  # 同母题
        _cand("v3", "math.f", "apply", p=0.45, tpl="tpl-B"),
    ]
    # 排除 tpl-A → 仅 v3 可选
    result = solve(
        st, pool, seed=0,
        excluded_template_version_ids=frozenset({"tpl-A"}),
    )
    assert isinstance(result, CpSatSolution)
    assert {c.item_version_id for c in result.selected} == {"v3"}


# ────────────────────────────────────────────────────────────────────
# 验收 #5：宪法 A5/A7 边界——不 import 任何学科包/学段包
# ────────────────────────────────────────────────────────────────────

def test_no_subject_pack_imports_in_solver_modules():
    """src/core/assembly/solver/ 下所有 .py 不 import 任何学科包/学段包."""
    project_root = Path(__file__).resolve().parent.parent.parent
    solver_dir = project_root / "src" / "core" / "assembly" / "solver"
    assert solver_dir.is_dir(), f"目录不存在：{solver_dir}"

    pattern = re.compile(
        r"^\s*(?:from\s+(?:packs|subject_)|import\s+(?:packs|subject_))",
        re.MULTILINE,
    )
    violations: list[str] = []
    for py_file in sorted(solver_dir.rglob("*.py")):
        text = py_file.read_text(encoding="utf-8")
        if pattern.findall(text):
            violations.append(str(py_file.relative_to(project_root)))
    assert not violations, (
        f"src/core/assembly/solver/ 存在学科包/学段包 import（违反 A5/A7）：{violations}"
    )
