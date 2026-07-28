"""T-W4-025 听力组卷 overlay：占比 30–40% / 置卷首 / testlet 标记（架构 v2 §4.4 / §4.6 / S5）.

英语试卷听力题约束 overlay：
- 占比硬约束：听力题占卷面总题量的 30–40%（ADR §4.6，S5 overlay 清单）。
- 位置硬约束：听力题置卷首（模拟考试听力先行语义）。
- testlet 标记：听力子题共享同一 testlet_id + 音频上下文（一材多题形态）。

为什么不修改 ConstraintSet：ConstraintSet 是波内冻结契约（extra='forbid'），
听力约束是学科级 overlay（英语听力线专属），不应侵入核心域约束模型。
overlay 以独立模型返回，由组卷编排层（T-W4-026 端到端）在装配后应用：
1. 求解器按 base Profile 选题（含听力候选）。
2. overlay 校验听力占比 + 标记 testlet + 重排卷首。
3. 不可行（听力素材不足）→ 返回冲突原因，不静默放松（§4.4 铁律）。

宪法 A5/X6：不 import 学科包/学段包；overlay 是核心域通用约束，
不感知「英语」语义（任何学科有听力需求均可复用）。
"""
from __future__ import annotations

import hashlib
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.core.assembly.profile import AssemblyProfile
from src.core.assembly.solver import AssemblyResult


# ════════════════════════════════════════════════════════════════════
# 常量
# ════════════════════════════════════════════════════════════════════

# 听力占比硬约束范围（ADR §4.6：30–40%）
LISTENING_RATIO_MIN: float = 0.30
LISTENING_RATIO_MAX: float = 0.40

# 听力题位置：置卷首
LISTENING_POSITION: Literal["first"] = "first"


# ════════════════════════════════════════════════════════════════════
# overlay 配置
# ════════════════════════════════════════════════════════════════════


class ListeningOverlaySpec(BaseModel):
    """听力 overlay 配置（可自定义参数，默认 30–40% / 置卷首）.

    - ratio_range：听力题占比范围 [min, max]（默认 0.30–0.40）。
    - position：听力题位置（'first'=卷首，当前唯一支持值）。
    - audio_context_ref：共享音频上下文引用（如 audio_id 或 paper 级音频 bundle id）。
    - max_duration_minutes：听力时长上限（分钟，学段配置；None=不限制）。
    """

    model_config = ConfigDict(extra="forbid")

    ratio_range: tuple[float, float] = (LISTENING_RATIO_MIN, LISTENING_RATIO_MAX)
    position: Literal["first"] = "first"
    audio_context_ref: str = Field(min_length=1, description="共享音频上下文引用")
    max_duration_minutes: Optional[int] = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _validate_ratio(self) -> "ListeningOverlaySpec":
        lo, hi = self.ratio_range
        if not (0.0 < lo < hi < 1.0):
            raise ValueError(
                f"ratio_range 非法：({lo}, {hi})，需满足 0 < min < max < 1"
            )
        return self


# ════════════════════════════════════════════════════════════════════
# 冲突与结果
# ════════════════════════════════════════════════════════════════════


class ListeningConflict(BaseModel):
    """听力 overlay 冲突原因（不可行时返回，禁止静默放松）."""

    model_config = ConfigDict(extra="forbid")

    constraint_id: str
    detail: str
    required: Optional[int] = None
    available: Optional[int] = None


class ListeningOverlay(BaseModel):
    """听力组卷 overlay（apply_listening_overlay 产物）.

    - testlet_id：听力题组 testlet 标识（子题共享同一音频上下文）。
    - listening_item_count_range：听力题量范围 [min, max]（由总题量×占比计算）。
    - spec：原始 overlay 配置（ratio/position/audio_context_ref 等）。
    """

    model_config = ConfigDict(extra="forbid")

    testlet_id: str
    listening_item_count_range: tuple[int, int]
    spec: ListeningOverlaySpec


class ListeningOverlayResult(BaseModel):
    """听力 overlay 应用结果.

    - feasible=True：profile 可追加听力约束，overlay 含 testlet/占比范围。
    - feasible=False：conflicts 非空，不可行原因结构化（不静默放松）。
    """

    model_config = ConfigDict(extra="forbid")

    profile: AssemblyProfile
    overlay: Optional[ListeningOverlay] = None
    conflicts: list[ListeningConflict] = Field(default_factory=list)
    feasible: bool

    @model_validator(mode="after")
    def _validate_consistency(self) -> "ListeningOverlayResult":
        if self.feasible and self.overlay is None:
            raise ValueError("feasible=True 时 overlay 不能为 None")
        if not self.feasible and not self.conflicts:
            raise ValueError("feasible=False 时 conflicts 不能为空")
        return self


# ════════════════════════════════════════════════════════════════════
# 公共入口
# ════════════════════════════════════════════════════════════════════


def _compute_testlet_id(audio_context_ref: str) -> str:
    """生成确定性 testlet_id（基于音频上下文哈希）.

    为什么用哈希而非随机：确定性——同一音频上下文得同一 testlet_id，
    便于审计回溯与重放（R-Z-01 确定性要求）。
    """
    digest = hashlib.sha256(
        f"listening:{audio_context_ref}".encode("utf-8")
    ).hexdigest()[:16]
    return f"testlet:listening:{digest}"


def _compute_listening_count_range(
    total_items: int, ratio_range: tuple[float, float]
) -> tuple[int, int]:
    """根据总题量与占比范围计算听力题量 [min, max].

    向上取整 min（保证听力占比下限），向下取整 max（不超占比上限）。
    为什么 ceil min / floor max：保证 [min, max] 内的任何值都落在
    [ratio_min, ratio_max] 区间内（保守不越界）。
    """
    import math

    lo = math.ceil(total_items * ratio_range[0])
    hi = math.floor(total_items * ratio_range[1])
    # 边界保护：至少 1 题（total_items > 0 时）
    if lo < 1:
        lo = 1
    if hi < lo:
        hi = lo
    return lo, hi


def apply_listening_overlay(
    paper_spec: AssemblyProfile,
    *,
    available_listening_items: int,
    spec: Optional[ListeningOverlaySpec] = None,
) -> ListeningOverlayResult:
    """在约束集中注入听力占比与位置硬约束（验收 #1/#2/#3）.

    流程：
    1. 从 paper_spec.constraints.item_count 取总题量。
    2. 按 ratio_range 计算听力题量 [min, max]。
    3. 校验可行：available_listening_items >= 听力题量 min。
       不可行 → 返回 conflicts（不静默放松）。
    4. 生成 testlet_id（基于 audio_context_ref 哈希）。
    5. 返回 ListeningOverlayResult（overlay + feasible=True）。

    为什么不直接修改 ConstraintSet：ConstraintSet 是波内冻结契约
    （extra='forbid'），听力约束是 overlay 层（不侵入核心约束模型）。
    overlay 由组卷编排层在装配后应用（标记 testlet + 重排卷首）。

    Args:
        paper_spec: 编译后的组卷 Profile（含 item_count 约束）。
        available_listening_items: 可用的听力候选题数量（≥0）。
        spec: overlay 配置（None → 需提供 audio_context_ref，否则用默认）。

    Returns:
        ListeningOverlayResult：
        - feasible=True：overlay 含 testlet_id + 听力题量范围。
        - feasible=False：conflicts 含不可行原因。

    Raises:
        ValueError: spec 为 None 且未提供 audio_context_ref。
    """
    if spec is None:
        raise ValueError(
            "spec 不能为 None（需提供 audio_context_ref）"
        )

    total_items = paper_spec.constraints.item_count.max
    ratio_min, ratio_max = spec.ratio_range
    listen_min, listen_max = _compute_listening_count_range(
        total_items, spec.ratio_range
    )

    conflicts: list[ListeningConflict] = []

    # ── 可行性校验：听力素材是否充足 ──
    if available_listening_items < listen_min:
        conflicts.append(
            ListeningConflict(
                constraint_id="listening_ratio_min",
                detail=(
                    f"听力题占比下限 {ratio_min:.0%} × 总题量 {total_items} "
                    f"= 至少 {listen_min} 道听力题，"
                    f"但可用听力候选仅 {available_listening_items} 道"
                ),
                required=listen_min,
                available=available_listening_items,
            )
        )

    # ── 可行性校验：听力题量不超过总题量 ──
    if listen_max > total_items:
        conflicts.append(
            ListeningConflict(
                constraint_id="listening_ratio_max_exceeds_total",
                detail=(
                    f"听力题占比上限 {ratio_max:.0%} × 总题量 {total_items} "
                    f"= {listen_max} 道，超过总题量 {total_items}"
                ),
                required=total_items,
                available=listen_max,
            )
        )

    if conflicts:
        return ListeningOverlayResult(
            profile=paper_spec,
            overlay=None,
            conflicts=conflicts,
            feasible=False,
        )

    # ── 生成 overlay ──
    testlet_id = _compute_testlet_id(spec.audio_context_ref)
    overlay = ListeningOverlay(
        testlet_id=testlet_id,
        listening_item_count_range=(listen_min, listen_max),
        spec=spec,
    )

    return ListeningOverlayResult(
        profile=paper_spec,
        overlay=overlay,
        conflicts=[],
        feasible=True,
    )


# ════════════════════════════════════════════════════════════════════
# 装配后处理：标记 testlet + 重排卷首
# ════════════════════════════════════════════════════════════════════


def mark_listening_testlet(
    result: AssemblyResult,
    overlay: ListeningOverlay,
    *,
    listening_item_version_ids: frozenset[str],
) -> AssemblyResult:
    """在组卷结果中标记听力题 testlet_id + 确保置卷首（验收 #2）.

    流程：
    1. 对 result.items 中的听力题（item_version_id ∈ listening_set）
       设置 group_id = overlay.testlet_id（标记 testlet）。
    2. 重排序：听力题置卷首，非听力题保持原序。
    3. 校验听力占比在 [min, max] 范围内（不满足 → 抛异常，不静默放松）。
    4. 重新计算 selection_digest（排序变化后）。

    为什么在后处理而非求解期：求解器是通用预算装填，不感知「听力」语义；
    overlay 在求解后应用，保持求解器学科无关（A5）。

    Args:
        result: 组卷结果（assemble 产物）。
        overlay: 听力 overlay（含 testlet_id + 占比范围）。
        listening_item_version_ids: 听力题的 item_version_id 集合。

    Returns:
        修改后的 AssemblyResult（听力题标记 testlet + 置卷首）。

    Raises:
        ValueError: 听力题数量不在 overlay 指定范围内。
    """
    import hashlib

    listen_min, listen_max = overlay.listening_item_count_range

    # 标记 testlet
    marked_items = []
    listening_items = []
    non_listening_items = []
    for item in result.items:
        if item.item_version_id in listening_item_version_ids:
            # 标记 testlet_id（复制 item 并设置 group_id）
            marked = item.model_copy(update={"group_id": overlay.testlet_id})
            listening_items.append(marked)
        else:
            non_listening_items.append(item)

    # 校验占比
    listen_count = len(listening_items)
    if listen_count < listen_min or listen_count > listen_max:
        raise ValueError(
            f"听力题数量 {listen_count} 不在 overlay 范围 "
            f"[{listen_min}, {listen_max}]（禁止静默放松）"
        )

    # 重排：听力置卷首，非听力保持原序
    reordered = listening_items + non_listening_items

    # 重新计算 digest（排序变化）
    new_digest = hashlib.sha256(
        "|".join(m.item_version_id for m in reordered).encode("utf-8")
    ).hexdigest()

    return result.model_copy(
        update={
            "items": reordered,
            "selection_digest": new_digest,
        }
    )


__all__ = [
    "LISTENING_RATIO_MIN",
    "LISTENING_RATIO_MAX",
    "LISTENING_POSITION",
    "ListeningOverlaySpec",
    "ListeningConflict",
    "ListeningOverlay",
    "ListeningOverlayResult",
    "apply_listening_overlay",
    "mark_listening_testlet",
]
