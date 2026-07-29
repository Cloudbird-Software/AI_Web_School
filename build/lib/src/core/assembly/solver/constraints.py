"""§4.4 CP-SAT 候选与约束模型（T-W4-028）.

定义 CP-SAT 求解器消费的候选题模型与不可行报告结构。

为什么独立 MeasurementCandidate 而非复用 CandidateItem：
- CandidateItem（src/core/assembly/candidates.py）服务于在线练习/诊断启发式，
  字段集面向曝光互斥与目标正确率区间，未含 cognitive_level；
- 双向细目表的第二维是认知层级，CP-SAT 编译单元格配额时必须知道每题的
  cognitive_level 才能匹配到 cell（架构 §4.4「内容×认知×题量×难度」）；
- 在 CandidateItem 加列会改 W3 既有契约（owner=src/core/assembly，本任务
  owner=src/core/assembly/solver 不可越界改 candidates.py），且在线热路径
  不需 cognitive_level——为离线测量卷加字段属过度耦合；
- MeasurementCandidate 在本子包内独立定义，调用方（T-W4-029 测量卷产出）
  从 serving 视图加载 objective.cognitive_level 后构造传入。

宪法 A5/A7：本模块不 import 任何学科包/学段包。
"""
from __future__ import annotations

from typing import Any, Mapping, Optional

from pydantic import BaseModel, ConfigDict, Field

# 认知层级六值（与 Objective.cognitive_level / SpecCell.cognitive_level 同集）
_COGNITIVE_LEVELS: frozenset[str] = frozenset(
    {"remember", "understand", "apply", "analyze", "evaluate", "create"}
)


class MeasurementCandidate(BaseModel):
    """CP-SAT 求解器消费的测量卷候选题.

    Attributes:
        item_version_id: 题 version id（与 CandidateItem 同口径）。
        kp_codes: 知识点编码列表（可多元素；与 cell.content_code 任一匹配即可填入）。
        cognitive_level: 认知层级（Bloom 六级，与 SpecCell.cognitive_level 同集）。
        p_correct: 难度指数（p_correct 口径，[0.0, 1.0]，越大越易；与
            SpecCell.difficulty_min/max 同口径）。
        group_id: 题组 id（同组题作为整体入选/排除；None=孤立题）。
        template_version_id: 母题版本 id（曝光互斥依据；None 表示无母题）。
    """

    model_config = ConfigDict(extra="forbid")

    item_version_id: str = Field(min_length=1)
    kp_codes: list[str] = Field(min_length=1)
    cognitive_level: str
    p_correct: float = Field(ge=0.0, le=1.0)
    group_id: Optional[str] = None
    template_version_id: Optional[str] = None

    def matches_cell(
        self, content_code: str, cognitive_level: str,
        difficulty_min: float, difficulty_max: float,
    ) -> bool:
        """该候选能否填入指定 cell.

        匹配条件：
        1. content_code 在 self.kp_codes 中（任一匹配即可）
        2. cognitive_level 严格相等
        3. p_correct ∈ [difficulty_min, difficulty_max]（闭区间）

        闭区间与 SpecCell 校验一致（difficulty_min == difficulty_max 合法）。
        """
        return (
            content_code in self.kp_codes
            and self.cognitive_level == cognitive_level
            and difficulty_min <= self.p_correct <= difficulty_max
        )


def measurement_candidate_from_serving_row(row: Mapping[str, Any]) -> MeasurementCandidate:
    """从 v_serving_item_version 行（dict/Mapping）构建测量卷候选.

    与 ``candidate_from_serving_row`` 同源但额外抽取 objective.cognitive_level
    和 lineage.params.p_correct_prior。

    Args:
        row: v_serving_item_version 行（至少含 item_version_id / objective /
            lineage；与 candidates.candidate_from_serving_row 同口径）。

    Returns:
        MeasurementCandidate 实例。

    Raises:
        ValueError: 缺 cognitive_level 或 p_correct_prior。
    """
    objective = row.get("objective") or {}
    kp_set = objective.get("kp_set") or []
    kp_codes = [str(k.get("code")) for k in kp_set if k.get("code")]
    if not kp_codes:
        raise ValueError(
            f"item_version {row.get('item_version_id')} 的 objective.kp_set 为空，无法组卷"
        )

    cognitive_level = objective.get("cognitive_level")
    if cognitive_level is None:
        raise ValueError(
            f"item_version {row.get('item_version_id')} 缺 objective.cognitive_level"
        )
    if cognitive_level not in _COGNITIVE_LEVELS:
        raise ValueError(
            f"item_version {row.get('item_version_id')} cognitive_level "
            f"{cognitive_level!r} 越域；合法域 {sorted(_COGNITIVE_LEVELS)}"
        )

    lineage = row.get("lineage") or {}
    params = lineage.get("params") or {}
    p_prior = params.get("p_correct_prior")
    if p_prior is None:
        raise ValueError(
            f"item_version {row.get('item_version_id')} 缺 lineage.params.p_correct_prior"
        )

    return MeasurementCandidate(
        item_version_id=str(row["item_version_id"]),
        kp_codes=kp_codes,
        cognitive_level=cognitive_level,
        p_correct=float(p_prior),
        group_id=params.get("group_id"),
        template_version_id=(
            str(row["template_version_id"]) if row.get("template_version_id") else None
        ),
    )


# ════════════════════════════════════════════════════════════════════
# 不可行报告（架构 §4.4 铁律：不可行必须返回冲突原因，禁止静默放松）
# ════════════════════════════════════════════════════════════════════

class CpSatConflict(BaseModel):
    """单条 CP-SAT 不可行冲突原因（架构 §4.4 铁律）.

    与 src/core/assembly/solver.py ConflictReason 同结构但独立定义——避免
    CP-SAT 子包依赖启发式 solver 的模型（解耦）。
    """

    model_config = ConfigDict(extra="forbid")

    constraint_id: str
    detail: str
    cell_content_code: Optional[str] = None
    cell_cognitive_level: Optional[str] = None
    required: Optional[int] = None
    available: Optional[int] = None


class CpSatInfeasible(BaseModel):
    """CP-SAT 不可行报告：含全部冲突约束.

    返回时机：CP-SAT 求解器返回 INFEASIBLE 时，分析 cell 配额缺口与
    曝光/题组约束冲突，返回结构化报告。调用方据此决定升级给人类或
    调整 spec_table/题池。
    """

    model_config = ConfigDict(extra="forbid")

    conflicts: list[CpSatConflict] = Field(min_length=1)
    candidate_pool_size: int = Field(ge=0)
    spec_table_total_count: int = Field(ge=1)
    seed: int

    def summary(self) -> str:
        """人类可读摘要（错误信息/日志用）."""
        lines = [f"CP-SAT 不可行（{len(self.conflicts)} 条冲突）："]
        for c in self.conflicts:
            lines.append(f"  - [{c.constraint_id}] {c.detail}")
        return "\n".join(lines)


__all__ = [
    "MeasurementCandidate",
    "measurement_candidate_from_serving_row",
    "CpSatConflict",
    "CpSatInfeasible",
]
