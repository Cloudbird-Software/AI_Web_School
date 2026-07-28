"""§4.4 测量卷产出（T-W4-029）.

将 CP-SAT 求解结果（CpSatSolution）组装为 MeasurementPaper——一份可渲染、
可审计的测量卷对象，承载细目表映射、题序、作答说明与合规校验。

为什么独立 MeasurementPaper 而非复用 W3 paper/paper_item ORM：
- W3 paper（T-W2-037）面向在线练习/诊断的卷追溯（卷码/QR/曝光账本），
  字段集围绕「已发布卷的持久化与扫码溯源」；
- 测量卷是离线产出物，核心是「双向细目表合规」——每单元格题数×难度区间
  必须与 spec_table 一致（测量有效性的统计基础），合规偏差为 0 才可签发；
- 在线 paper 加细目表列会改 W3 既有契约（owner=src/core/render 的 paper
  模型 + 卷追溯契约），本任务 owner=src/core/assembly 不可越界；
- MeasurementPaper 在 assembly 域内独立定义，渲染适配（measurement_adapter）
  由 render 域消费，保持装配/渲染职责分离。

合规校验（验收 #2）：verify_compliance 对照 spec_table 逐单元格校验
- 题数偏差：actual_count vs target_count（|差| 累加为 total_count_deviation）
- 难度合规：每题 p_correct ∈ [cell.difficulty_min, difficulty_max]
CP-SAT 可行解保证偏差为 0；本函数独立校验以防产出链路下游篡改/漂移。

宪法 A5/A7：本模块不 import 任何学科包/学段包。
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from src.core.assembly.solver.cpsat_solver import CpSatSolution
from src.core.assembly.spec_table import SpecTable


# ════════════════════════════════════════════════════════════════════
# 细目表单元格映射
# ════════════════════════════════════════════════════════════════════

class MeasurementCellMapping(BaseModel):
    """细目表单元格映射：spec cell → 入选题列表 + 难度留档.

    一个 cell = (content_code, cognitive_level) 二元组，对应 SpecTable 一个
    SpecCell。actual_count 为 CP-SAT 实际填入该 cell 的题数；item_p_corrects
    与 item_version_ids 同序，留档每题难度指数供合规校验与审计。
    """

    model_config = ConfigDict(extra="forbid")

    content_code: str
    cognitive_level: str
    target_count: int = Field(ge=0)
    actual_count: int = Field(ge=0)
    difficulty_min: float = Field(ge=0.0, le=1.0)
    difficulty_max: float = Field(ge=0.0, le=1.0)
    item_version_ids: list[str] = Field(default_factory=list)
    item_p_corrects: list[float] = Field(default_factory=list)


# ════════════════════════════════════════════════════════════════════
# 合规校验报告
# ════════════════════════════════════════════════════════════════════

class ComplianceViolation(BaseModel):
    """单条合规偏差（verify_compliance 产出）."""

    model_config = ConfigDict(extra="forbid")

    cell_key: str
    kind: Literal["count_mismatch", "difficulty_out_of_range", "cell_missing"]
    detail: str
    expected: str
    actual: str


class ComplianceReport(BaseModel):
    """细目表合规校验报告.

    is_compliant=True 当且仅当 violations 为空（题数与难度全合规）；
    total_count_deviation 为各 cell |actual-target| 之和（0 = 题数完全一致）。
    """

    model_config = ConfigDict(extra="forbid")

    is_compliant: bool
    total_count_deviation: int = Field(ge=0)
    violations: list[ComplianceViolation] = Field(default_factory=list)


# ════════════════════════════════════════════════════════════════════
# 测量卷
# ════════════════════════════════════════════════════════════════════

class MeasurementPaper(BaseModel):
    """测量卷：CP-SAT 解 + 细目表映射 + 题序 + 作答说明.

    Attributes:
        spec_table_id / spec_table_version: 溯源到 SpecTable（D1 版本化）。
        seed: CP-SAT 确定性种子（R-Z-01 留档）。
        cell_mappings: 细目表每 cell 的映射（按 spec_table.cells 顺序）。
        ordered_item_version_ids: 卷内题序（按 cell 顺序、cell 内按求解器返回序），
            渲染时按此序分配题号 1..N（题号对齐）。
        answer_instructions: 作答说明（卷首页印刷）。
        selection_digest: 来自 CpSatSolution.selection_digest，固化选题结果供审计。
        item_p_correct: item_version_id → p_correct，合规校验与审计用。
    """

    model_config = ConfigDict(extra="forbid")

    spec_table_id: str
    spec_table_version: str
    seed: int
    cell_mappings: list[MeasurementCellMapping] = Field(min_length=1)
    ordered_item_version_ids: list[str] = Field(default_factory=list)
    answer_instructions: str
    selection_digest: str
    item_p_correct: dict[str, float] = Field(default_factory=dict)


# ════════════════════════════════════════════════════════════════════
# 作答说明默认文案
# ════════════════════════════════════════════════════════════════════

def _default_answer_instructions(total_count: int) -> str:
    """测量卷作答说明默认文案.

    为什么提供默认：测量卷作答说明高度模板化（题量/作答卡/翻页限制），
    调用方通常无需自定义；特殊版式可传入覆盖。文案含「翻页无效」提示，
    与渲染层 ProhibitionMarker 呼应（验收 #3 禁止标记）。
    """
    return (
        f"本测量卷共 {total_count} 题。请仔细阅读每题要求后作答："
        "选择题答案填涂在作答卡对应题号处，填空题答案写在题内空位；"
        "每题作答完毕请检查，考试期间翻页无效，禁止交头接耳。"
    )


# ════════════════════════════════════════════════════════════════════
# 主入口
# ════════════════════════════════════════════════════════════════════

def build_measurement_paper(
    solution: CpSatSolution,
    spec_table: SpecTable,
    *,
    answer_instructions: str | None = None,
) -> MeasurementPaper:
    """将 CP-SAT 可行解组装为测量卷.

    参数:
        solution: CP-SAT 求解器返回的可行解（CpSatSolution），含 cell_assignment
            与 selected 候选列表。
        spec_table: 双向细目表（定义 cell 配额与难度区间）。
        answer_instructions: 作答说明自定义文案（None=用默认文案）。

    返回:
        MeasurementPaper（cell_mappings 按 spec_table.cells 顺序，题序按 cell
        顺序聚合）。

    异常:
        ValueError: solution 非 CpSatSolution（不可行解不能组卷）。
    """
    if not getattr(solution, "is_feasible", False):
        raise ValueError(
            "不可行解不能组卷：CP-SAT 返回 CpSatInfeasible，请先调整 spec_table/"
            "候选池后重试求解（§4.4 铁律：禁止静默放松）"
        )

    # item_version_id → p_correct（从 selected 候选建索引，供合规校验留档）
    p_correct_map: dict[str, float] = {
        c.item_version_id: c.p_correct for c in solution.selected
    }

    cell_mappings: list[MeasurementCellMapping] = []
    ordered_item_version_ids: list[str] = []
    for cell in spec_table.cells:
        cell_key = f"{cell.content_code}/{cell.cognitive_level}"
        vids = list(solution.cell_assignment.get(cell_key, []))
        cell_mappings.append(
            MeasurementCellMapping(
                content_code=cell.content_code,
                cognitive_level=cell.cognitive_level,
                target_count=cell.target_count,
                actual_count=len(vids),
                difficulty_min=cell.difficulty_min,
                difficulty_max=cell.difficulty_max,
                item_version_ids=vids,
                item_p_corrects=[p_correct_map[v] for v in vids],
            )
        )
        ordered_item_version_ids.extend(vids)

    instructions = answer_instructions or _default_answer_instructions(
        spec_table.total_count
    )

    return MeasurementPaper(
        spec_table_id=spec_table.spec_table_id,
        spec_table_version=spec_table.spec_table_version,
        seed=solution.seed,
        cell_mappings=cell_mappings,
        ordered_item_version_ids=ordered_item_version_ids,
        answer_instructions=instructions,
        selection_digest=solution.selection_digest,
        item_p_correct=p_correct_map,
    )


def verify_compliance(
    paper: MeasurementPaper,
    spec_table: SpecTable,
) -> ComplianceReport:
    """细目表合规校验：逐 cell 校验题数与难度（验收 #2）.

    校验项：
    1. 题数：每 cell actual_count == target_count（偏差累加为 total_count_deviation）
    2. 难度：每题 p_correct ∈ [cell.difficulty_min, difficulty_max]
    3. 覆盖：spec_table 每 cell 在产出卷中均有映射（缺失记 cell_missing）

    CP-SAT 可行解经 build_measurement_paper 产出后，本函数应返回 is_compliant=True、
    total_count_deviation=0；独立校验以防产出链路下游篡改/漂移。

    参数:
        paper: 待校验测量卷。
        spec_table: 对照细目表。

    返回:
        ComplianceReport（is_compliant / total_count_deviation / violations）。
    """
    mapping_by_key: dict[str, MeasurementCellMapping] = {
        f"{m.content_code}/{m.cognitive_level}": m for m in paper.cell_mappings
    }

    violations: list[ComplianceViolation] = []
    total_count_deviation = 0

    for cell in spec_table.cells:
        key = f"{cell.content_code}/{cell.cognitive_level}"
        mapping = mapping_by_key.get(key)
        if mapping is None:
            violations.append(
                ComplianceViolation(
                    cell_key=key,
                    kind="cell_missing",
                    detail=f"细目表单元格 {key} 在产出卷中缺失",
                    expected=f"target_count={cell.target_count}",
                    actual="缺失",
                )
            )
            total_count_deviation += cell.target_count
            continue

        # 题数校验
        if mapping.actual_count != cell.target_count:
            diff = abs(mapping.actual_count - cell.target_count)
            total_count_deviation += diff
            violations.append(
                ComplianceViolation(
                    cell_key=key,
                    kind="count_mismatch",
                    detail=(
                        f"单元格 {key} 题数偏差：目标 {cell.target_count}，"
                        f"实际 {mapping.actual_count}（偏差 {diff}）"
                    ),
                    expected=str(cell.target_count),
                    actual=str(mapping.actual_count),
                )
            )

        # 难度校验
        for vid, p in zip(mapping.item_version_ids, mapping.item_p_corrects):
            if not (cell.difficulty_min <= p <= cell.difficulty_max):
                violations.append(
                    ComplianceViolation(
                        cell_key=key,
                        kind="difficulty_out_of_range",
                        detail=(
                            f"题 {vid} 的 p_correct={p} 不在单元格 {key} 难度区间 "
                            f"[{cell.difficulty_min}, {cell.difficulty_max}]"
                        ),
                        expected=f"[{cell.difficulty_min}, {cell.difficulty_max}]",
                        actual=str(p),
                    )
                )

    return ComplianceReport(
        is_compliant=(len(violations) == 0),
        total_count_deviation=total_count_deviation,
        violations=violations,
    )


__all__ = [
    "MeasurementCellMapping",
    "ComplianceViolation",
    "ComplianceReport",
    "MeasurementPaper",
    "build_measurement_paper",
    "verify_compliance",
]
