"""§4.4 CP-SAT 离线组卷求解器（T-W4-028）.

架构 v2 §4.4「求解」段：离线出口跑完整 CP-SAT（硬约束可行解 + 软目标优化）。
本模块落地 ``solve(spec_table, candidate_pool, seed=0)`` ——将双向细目表 +
候选题池 + 附加约束（曝光互斥 / 题组 / testlet）编译为 CP-SAT 模型，求可行解
或返回 ``CpSatInfeasible``（含冲突约束列表，§4.4 铁律：禁止静默放松）。

约束编译：
- 单元格配额（硬）：每个 SpecCell 的入选题数 == target_count
- 候选-cell 分配（硬）：每个入选候选恰好分配给一个匹配的 cell
- 难度合规（硬）：候选只能分配给 p_correct ∈ [cell.difficulty_min, cell.difficulty_max] 的 cell
- 曝光互斥（硬）：excluded 集合中的 item_version_id / template_version_id 禁止入选
- 题组整体入选（硬）：同 group_id 的候选要么全入选要么全不入选（testlet 语义）

为什么所有约束都硬约束化：测量卷的「双向细目表合规」是测量有效性的统计基础
（每格题数不足则维度估计不可靠），不可降级为软目标。若需软目标化（如「优先
填入 p_correct 接近区间中点的候选」），可在硬约束可行解基础上加 objective
求最大化——v1 不做，留开放项。

确定性（R-Z-01）：CP-SAT ``solver.random_seed = seed``，同输入同种子同输出。
跨进程可复现（CP-SAT 是确定性求解器，给定 seed 与 model 不依赖墙钟）。

不可行处理：CP-SAT 返回 INFEASIBLE 时，独立分析 cell 配额缺口（不依赖 CP-SAT
内部冲突报告，因其 API 不稳定）——遍历每个 cell 统计合格候选数与 target_count
对比，缺额则记 CpSatConflict。无配额缺口但仍不可行时记 generic conflict
（题组约束或曝光互斥冲突）。

宪法 A5/A7：本模块不 import 任何学科包/学段包。
"""
from __future__ import annotations

from typing import Optional, Union

from ortools.sat.python import cp_model
from pydantic import BaseModel, ConfigDict, Field

from src.core.assembly.solver.constraints import (
    CpSatConflict,
    CpSatInfeasible,
    MeasurementCandidate,
)
from src.core.assembly.spec_table import SpecTable


# ════════════════════════════════════════════════════════════════════
# 求解结果
# ════════════════════════════════════════════════════════════════════

class CpSatSolution(BaseModel):
    """CP-SAT 可行解：入选候选列表 + 留档元数据.

    与启发式 ``AssemblyResult`` 同功能但面向 CP-SAT：保留 selection_digest
    供审计比对；不含启发式 adjudications / soft_target_achievement（CP-SAT
    v1 全硬约束，无裁决与软目标）。
    """

    model_config = ConfigDict(extra="forbid")

    selected: list[MeasurementCandidate]
    spec_table_id: str
    spec_table_version: str
    seed: int
    cell_assignment: dict[str, list[str]] = Field(
        default_factory=dict,
        description="cell_key 'content_code/cognitive_level' → 入选 item_version_id 列表",
    )
    selection_digest: str = Field(
        default="",
        description="选题结果指纹（sha256，确定性留档；空字符串表示未计算）",
    )

    @property
    def is_feasible(self) -> bool:
        """可行解标记（与 CpSatInfeasible 区分）."""
        return True


# ════════════════════════════════════════════════════════════════════
# 不可行分析
# ════════════════════════════════════════════════════════════════════

def _eligible_count(
    cell: object,
    pool: list[MeasurementCandidate],
    excluded_item_version_ids: frozenset[str],
    excluded_template_version_ids: frozenset[str],
) -> int:
    """统计可填入 cell 的合格候选数（曝光互斥已过滤）."""
    n = 0
    for c in pool:
        if c.item_version_id in excluded_item_version_ids:
            continue
        if c.template_version_id and c.template_version_id in excluded_template_version_ids:
            continue
        if c.matches_cell(
            cell.content_code, cell.cognitive_level,
            cell.difficulty_min, cell.difficulty_max,
        ):
            n += 1
    return n


def _analyze_infeasibility(
    spec_table: SpecTable,
    candidate_pool: list[MeasurementCandidate],
    seed: int,
    excluded_item_version_ids: frozenset[str],
    excluded_template_version_ids: frozenset[str],
) -> CpSatInfeasible:
    """分析不可行原因：遍历每个 cell 统计合格候选数与 target_count 对比.

    为什么独立分析而非依赖 CP-SAT 内部冲突报告：CP-SAT 的
    ``solver.SufficientAssumptionsForInfeasibility`` 等 API 在不同版本语义
    不稳定（且对 cell 配额这类复合约束不直接）；自研回归分析可控且与
    SpecTable schema 紧耦合。

    无配额缺口但仍不可行 → 记 generic conflict（题组约束或曝光互斥）。
    """
    conflicts: list[CpSatConflict] = []
    for cell in spec_table.cells:
        eligible = _eligible_count(
            cell, candidate_pool,
            excluded_item_version_ids, excluded_template_version_ids,
        )
        if eligible < cell.target_count:
            conflicts.append(
                CpSatConflict(
                    constraint_id="cell_quota",
                    detail=(
                        f"单元格 {cell.content_code}/{cell.cognitive_level} "
                        f"需 {cell.target_count} 题，但合格候选仅 {eligible} 题"
                        f"（p_correct∈[{cell.difficulty_min}, "
                        f"{cell.difficulty_max}]，曝光互斥已过滤）"
                    ),
                    cell_content_code=cell.content_code,
                    cell_cognitive_level=cell.cognitive_level,
                    required=cell.target_count,
                    available=eligible,
                )
            )

    if not conflicts:
        # 配额都满足但仍 INFEASIBLE → 题组整体入选或曝光互斥交叉冲突
        conflicts.append(
            CpSatConflict(
                constraint_id="constraint_conflict",
                detail=(
                    "CP-SAT 不可行但无明显 cell 配额缺口；"
                    "可能为题组整体入选约束（同 group_id 候选不足同时入选）"
                    "或曝光互斥与配额交叉冲突"
                ),
            )
        )

    return CpSatInfeasible(
        conflicts=conflicts,
        candidate_pool_size=len(candidate_pool),
        spec_table_total_count=spec_table.total_count,
        seed=seed,
    )


# ════════════════════════════════════════════════════════════════════
# 模型编译
# ════════════════════════════════════════════════════════════════════

def _build_model(
    spec_table: SpecTable,
    candidate_pool: list[MeasurementCandidate],
    *,
    excluded_item_version_ids: frozenset[str],
    excluded_template_version_ids: frozenset[str],
) -> tuple[cp_model.CpModel, dict[int, cp_model.BoolVar], dict[tuple[int, int], cp_model.BoolVar]]:
    """编译 CP-SAT 模型：决策变量 + 全部硬约束.

    Returns:
        (model, x, y) 三元组——
        - model: 已编译的 CpModel
        - x: {candidate_idx: BoolVar} 入选决策
        - y: {(candidate_idx, cell_idx): BoolVar} 候选-cell 分配决策
    """
    model = cp_model.CpModel()

    # ── 决策变量 ──
    x: dict[int, cp_model.BoolVar] = {
        i: model.NewBoolVar(f"x_{i}") for i in range(len(candidate_pool))
    }

    # ── 候选-cell 匹配预计算 ──
    cells = spec_table.cells
    y: dict[tuple[int, int], cp_model.BoolVar] = {}
    for i, cand in enumerate(candidate_pool):
        for k, cell in enumerate(cells):
            if cand.matches_cell(
                cell.content_code, cell.cognitive_level,
                cell.difficulty_min, cell.difficulty_max,
            ):
                y[i, k] = model.NewBoolVar(f"y_{i}_{k}")

    # ── 曝光互斥：excluded 候选禁止入选 ──
    for i, cand in enumerate(candidate_pool):
        if cand.item_version_id in excluded_item_version_ids:
            model.Add(x[i] == 0)
        if (
            cand.template_version_id
            and cand.template_version_id in excluded_template_version_ids
        ):
            model.Add(x[i] == 0)

    # ── 候选-cell 分配一致性：入选候选恰好分配给一个匹配 cell ──
    for i in range(len(candidate_pool)):
        ys = [y[i, k] for k in range(len(cells)) if (i, k) in y]
        if ys:
            # x[i] == sum(y[i, :])（入选 ⇔ 恰好分配到一个 cell）
            model.Add(sum(ys) == x[i])
        else:
            # 无匹配 cell 的候选禁止入选（难度/cell 不合规）
            model.Add(x[i] == 0)

    # ── 单元格配额（硬）：每 cell 入选题数 == target_count ──
    for k, cell in enumerate(cells):
        ys = [y[i, k] for i in range(len(candidate_pool)) if (i, k) in y]
        model.Add(sum(ys) == cell.target_count)

    # ── 题组整体入选（testlet 语义）：同 group_id 全有或全无 ──
    groups: dict[str, list[int]] = {}
    for i, cand in enumerate(candidate_pool):
        if cand.group_id:
            groups.setdefault(cand.group_id, []).append(i)
    for gid, members in groups.items():
        # 同组所有 x 相等（取第一个为基准）
        base = x[members[0]]
        for j in members[1:]:
            model.Add(x[j] == base)

    return model, x, y


# ════════════════════════════════════════════════════════════════════
# 主入口
# ════════════════════════════════════════════════════════════════════

def solve(
    spec_table: SpecTable,
    candidate_pool: list[MeasurementCandidate],
    *,
    seed: int = 0,
    excluded_item_version_ids: frozenset[str] = frozenset(),
    excluded_template_version_ids: frozenset[str] = frozenset(),
    time_limit_seconds: Optional[float] = None,
) -> Union[CpSatSolution, CpSatInfeasible]:
    """CP-SAT 离线组卷求解.

    Args:
        spec_table: 双向细目表（定义单元格配额与难度区间）。
        candidate_pool: 候选题池（MeasurementCandidate 列表，调用方从 serving
            视图加载后包装）。
        seed: 确定性种子（CP-SAT random_seed；同输入同种子同输出）。
        excluded_item_version_ids: 曝光互斥——item_version_id 集合，禁止入选。
        excluded_template_version_ids: 曝光互斥——template_version_id 集合，
            禁止入选（同母题不同卷，§4.4 R-Z-02）。
        time_limit_seconds: 求解时间上限（None=不限；测量卷离线场景默认不限）。

    Returns:
        CpSatSolution: 可行解，含入选候选列表与 cell 分配留档。
        CpSatInfeasible: 不可行报告，含冲突约束列表（§4.4 铁律：禁止静默放松）。

    Notes:
        - 确定性：CP-SAT random_seed = seed；同 (spec_table, candidate_pool, seed,
          excluded_*) 必同输出。CP-SAT 是确定性求解器，跨进程可复现。
        - 性能：典型测量卷（数十题、数十 cell、数百候选）秒级出解；超大池可设
          time_limit_seconds 上限。
    """
    model, x, y = _build_model(
        spec_table, candidate_pool,
        excluded_item_version_ids=excluded_item_version_ids,
        excluded_template_version_ids=excluded_template_version_ids,
    )

    solver = cp_model.CpSolver()
    solver.random_seed = seed
    if time_limit_seconds is not None:
        solver.parameters.max_time_in_seconds = float(time_limit_seconds)

    status = solver.Solve(model)

    if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        # 提取入选候选 + cell 分配
        cells = spec_table.cells
        selected: list[MeasurementCandidate] = []
        cell_assignment: dict[str, list[str]] = {}
        for k, cell in enumerate(cells):
            cell_key = f"{cell.content_code}/{cell.cognitive_level}"
            cell_assignment[cell_key] = []
            for i in range(len(candidate_pool)):
                if (i, k) in y and solver.Value(y[i, k]) == 1:
                    selected.append(candidate_pool[i])
                    cell_assignment[cell_key].append(
                        candidate_pool[i].item_version_id
                    )

        import hashlib
        digest = hashlib.sha256(
            "|".join(sorted(c.item_version_id for c in selected)).encode("utf-8")
        ).hexdigest()

        return CpSatSolution(
            selected=selected,
            spec_table_id=spec_table.spec_table_id,
            spec_table_version=spec_table.spec_table_version,
            seed=seed,
            cell_assignment=cell_assignment,
            selection_digest=digest,
        )

    # INFEASIBLE 或 UNKNOWN（time_limit 截断）：分析并返回结构化冲突
    return _analyze_infeasibility(
        spec_table, candidate_pool, seed,
        excluded_item_version_ids, excluded_template_version_ids,
    )


__all__ = [
    "CpSatSolution",
    "solve",
]
