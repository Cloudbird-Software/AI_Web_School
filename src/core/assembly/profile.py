"""§4.4 组卷约束集四维编译（T-W3-assembly S1/S2）.

架构 v2 §4.4：AssemblyProfile = base ∪ subject_overlay ∪ purpose_overlay
∪ gradeband_overlay，四维均为版本化配置，编译时做冲突检测。

本模块落地：
- ConstraintSet：编译后的机器可执行约束集（题量/知识点配比/目标正确率区间/
  序列梯度单调/曝光互斥/题组≤6，R-Z-02）。
- compile_profile：四维合并 + 冲突检测 + 按预置优先级裁决（Adjudication 留档）。
- diagnosis_profile：诊断用途 Profile 工厂（孤立题强制、每知识点≥3、
  多点关系声明核验，R-Z-03）。

已知冲突的预置裁决（架构评审报告 §344 路径①）：「约 20 题」×「每知识点≥3」
在知识点多的单元不可同时满足 → 每知识点最低题量是硬约束（R-Z-03，诊断归因的
统计基础），题量上限软目标化并记录理由。裁决发生在编译期而非求解期——
求解器看到的 ConstraintSet 已无冲突，禁止求解期静默放松（§4.4）。

宪法 A5/A7：本模块不 import 任何学科包/学段包；学科 overlay 以 dict 传入
（调用方从包内 yaml 加载，核心域不感知文件位置与学科语义）。
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

Purpose = Literal["practice", "diagnosis", "measurement"]
Gradeband = Literal["L", "M", "H"]

# 架构 v2 §4.4：题组 ≤6（R-Z-06，DB 层 ck_ig_max_six_items 兜底）
MAX_ITEMS_PER_GROUP = 6

# 冷启动降级的保守宽度默认值（§4.4：无学生/cohort 数据时以纯先验区间+
# 保守宽度代入约束求解，数据回流后按周收紧——收紧动作在 S8 数据域，
# 本字段只是宽度的版本化配置）
DEFAULT_P_CORRECT_MARGIN = 0.10


# ════════════════════════════════════════════════════════════════════
# 约束集子模型
# ════════════════════════════════════════════════════════════════════

class ItemCountRule(BaseModel):
    """题量约束.

    soft=True 表示该上限已被编译期裁决为软目标（如「约 20 题」）：
    求解器可超出，但必须在结果的 soft_target_achievement 中记录超出量。
    min 永远是硬约束（题量不足=卷不成立）。
    """

    model_config = ConfigDict(extra="forbid")

    min: int = Field(ge=1)
    max: int = Field(ge=1)
    soft: bool = False


class KpQuota(BaseModel):
    """知识点配比约束：某知识点在卷中的最低题量.

    isolated_only=True（诊断）：只统计孤立题（单知识点、kp_set_mode='single'），
    多点题只佐证不定位，不计入该配额（§4.5：定位必须由孤立题完成）。
    """

    model_config = ConfigDict(extra="forbid")

    kp_code: str
    min_count: int = Field(ge=1)
    isolated_only: bool = False


class ContentMixRule(BaseModel):
    """内容配比软目标（新学/复习/易混淆交错，R-Z-02）.

    v1 为软目标：候选池标签不足时不判不可行，在结果中记录达成率；
    硬约束化需等业务明确配比违例的处置策略（留档开放项）。
    """

    model_config = ConfigDict(extra="forbid")

    # tag（'new'/'review'/'confusable'）→ 目标占比区间 [lo, hi]
    ratios: dict[str, tuple[float, float]]


class ConstraintSet(BaseModel):
    """编译后的机器可执行约束集（R-Z-02 全量 + R-Z-03 诊断扩展）."""

    model_config = ConfigDict(extra="forbid")

    item_count: ItemCountRule
    kp_quotas: list[KpQuota] = Field(default_factory=list)
    # 目标正确率区间（None=不约束）；配合 margin 做冷启动保守加宽
    target_p_correct_range: Optional[tuple[float, float]] = None
    p_correct_uncertainty_margin: float = DEFAULT_P_CORRECT_MARGIN
    # 序列梯度单调：输出序列按预测正确率降序（由易到难）
    gradient_monotone: bool = True
    # 曝光互斥（R-Z-02）：同母题不同卷 / 跨期不重复
    exposure_mutex_same_template: bool = True
    exposure_mutex_cross_period: bool = True
    # 题组 ≤6（R-Z-06）
    max_items_per_group: int = Field(default=MAX_ITEMS_PER_GROUP, le=MAX_ITEMS_PER_GROUP)
    # 内容配比软目标
    content_mix: Optional[ContentMixRule] = None
    # ── 诊断硬约束（R-Z-03）──
    require_isolated_items: bool = False
    multi_point_relation_check: bool = False


class Adjudication(BaseModel):
    """编译期冲突裁决记录（§4.4：按预置优先级裁决并记录理由）."""

    model_config = ConfigDict(extra="forbid")

    conflict_id: str
    constraint_a: str
    constraint_b: str
    decision: str
    reason: str


class AssemblyProfile(BaseModel):
    """版本化组卷 Profile（确定性三要素之一：快照+Profile版本+种子）."""

    model_config = ConfigDict(extra="forbid")

    profile_id: str
    profile_version: str
    purpose: Purpose
    gradeband: Gradeband
    constraints: ConstraintSet
    adjudications: list[Adjudication] = Field(default_factory=list)
    # 四维来源留档（审计用）：subject/purpose/gradeband overlay 的 id@version
    overlay_refs: dict[str, str] = Field(default_factory=dict)

    def digest(self) -> str:
        """Profile 内容指纹（确定性：同内容必同指纹）."""
        canonical = json.dumps(
            self.model_dump(mode="json"), sort_keys=True, ensure_ascii=False
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class ProfileConflictError(ValueError):
    """编译期不可裁决冲突（调用方显式禁止软目标化时抛出）."""

    def __init__(self, conflict_id: str, detail: str) -> None:
        self.conflict_id = conflict_id
        self.detail = detail
        super().__init__(f"[{conflict_id}] {detail}")


# ════════════════════════════════════════════════════════════════════
# 四维编译
# ════════════════════════════════════════════════════════════════════

def _overlay_get(overlay: Optional[dict[str, Any]], *path: str) -> Any:
    """从 overlay dict 按路径取值（不存在返回 None）."""
    node: Any = overlay
    for key in path:
        if not isinstance(node, dict) or key not in node:
            return None
        node = node[key]
    return node


def compile_profile(
    *,
    profile_id: str,
    profile_version: str,
    purpose: Purpose,
    gradeband: Gradeband,
    kp_codes: list[str],
    base: Optional[dict[str, Any]] = None,
    subject_overlay: Optional[dict[str, Any]] = None,
    purpose_overlay: Optional[dict[str, Any]] = None,
    gradeband_overlay: Optional[dict[str, Any]] = None,
    min_items_per_kp: Optional[int] = None,
    allow_item_count_soft: bool = True,
) -> AssemblyProfile:
    """四维编译：合并 base/subject/purpose/gradeband overlay 为约束集并裁决冲突.

    参数:
        kp_codes: 本次组卷的知识点范围（快照内容；快照 id 由调用方传给求解器）
        base/subject_overlay/purpose_overlay/gradeband_overlay: 四维版本化配置
            （dict 形式；subject_overlay 即学科包 assembly-overlays yaml 的内容）
        min_items_per_kp: 每知识点最低题量覆盖（None=按用途默认：诊断 3，其他 1）
        allow_item_count_soft: 冲突时是否允许把题量上限裁决为软目标
            （False 时冲突抛 ProfileConflictError——测量等场景的严格模式）

    合并优先级（高覆盖低）：gradeband_overlay > purpose_overlay >
    subject_overlay > base。这是架构 §4.4「四维编译」的默认顺序：
    学段参数最贴近学生安全（低段时长/题量保护），优先级最高。
    """
    overlays = [base or {}, subject_overlay or {}, purpose_overlay or {}, gradeband_overlay or {}]

    # ── 题量（取最高优先级定义了 item_count_range 的维度）──
    # 正向遍历 overlays（低→高优先级），高优先级后定义者覆盖低优先级
    count_range: Optional[list[int]] = None
    for ov in overlays:
        count_range = _overlay_get(ov, "item_count_range") or count_range
    if count_range is None:
        # 平台默认：练习卷 10–20 题（无 overlay 时的保守默认）
        count_range = [10, 20]
    item_count = ItemCountRule(min=int(count_range[0]), max=int(count_range[1]))

    # ── 知识点配比 ──
    if min_items_per_kp is None:
        min_items_per_kp = 3 if purpose == "diagnosis" else 1
    isolated_only = purpose == "diagnosis"
    kp_quotas = [
        KpQuota(kp_code=code, min_count=min_items_per_kp, isolated_only=isolated_only)
        for code in kp_codes
    ]

    # ── 目标正确率区间 ──
    p_range: Optional[tuple[float, float]] = None
    margin = DEFAULT_P_CORRECT_MARGIN
    for ov in overlays:
        rng = _overlay_get(ov, "difficulty_target", "target_p_correct_range")
        if rng is not None:
            p_range = (float(rng[0]), float(rng[1]))
        m = _overlay_get(ov, "difficulty_target", "uncertainty_margin")
        if m is not None:
            margin = float(m)

    # ── 通用开关（subject_overlay 的 assembly_constraints 维度）──
    gradient = True
    same_template = True
    cross_period = True
    content_mix: Optional[ContentMixRule] = None
    for ov in overlays:
        g = _overlay_get(ov, "assembly_constraints", "require_gradient_monotone")
        if g is not None:
            gradient = bool(g)
        st = _overlay_get(ov, "assembly_constraints", "exposure_mutex", "same_template_different_paper")
        if st is not None:
            same_template = bool(st)
        cp = _overlay_get(ov, "assembly_constraints", "exposure_mutex", "cross_period_repeat")
        if cp is not None:
            # yaml 语义：cross_period_repeat=False 表示「不允许跨期重复」=互斥开
            cross_period = not bool(cp)
        mix = _overlay_get(ov, "assembly_constraints", "content_mix")
        if isinstance(mix, dict):
            ratios: dict[str, tuple[float, float]] = {}
            for key, tag in (("new_learning_ratio", "new"), ("review_ratio", "review"), ("confusable_ratio", "confusable")):
                if key in mix:
                    ratios[tag] = (float(mix[key][0]), float(mix[key][1]))
            if ratios:
                content_mix = ContentMixRule(ratios=ratios)

    # ── 诊断硬约束（R-Z-03；purpose_overlay 可显式覆盖，默认随用途开启）──
    require_isolated = purpose == "diagnosis"
    relation_check = purpose == "diagnosis"
    iso = _overlay_get(purpose_overlay, "isolation_rules", "require_isolated_items")
    if iso is not None:
        require_isolated = bool(iso)
    rel = _overlay_get(purpose_overlay, "isolation_rules", "multi_point_relation_check")
    if rel is not None:
        relation_check = bool(rel)

    constraints = ConstraintSet(
        item_count=item_count,
        kp_quotas=kp_quotas,
        target_p_correct_range=p_range,
        p_correct_uncertainty_margin=margin,
        gradient_monotone=gradient,
        exposure_mutex_same_template=same_template,
        exposure_mutex_cross_period=cross_period,
        content_mix=content_mix,
        require_isolated_items=require_isolated,
        multi_point_relation_check=relation_check,
    )

    # ── 冲突检测与裁决（§4.4）──
    adjudications: list[Adjudication] = []
    min_required = sum(q.min_count for q in kp_quotas)
    if min_required > constraints.item_count.max:
        conflict_id = "item_count_vs_kp_quota"
        detail = (
            f"知识点最低题量合计 {min_required}（{len(kp_quotas)} 点 × "
            f"每点≥{min_items_per_kp}）超出题量上限 {constraints.item_count.max}"
        )
        if not allow_item_count_soft:
            raise ProfileConflictError(conflict_id, detail)
        # 预置优先级：每知识点最低题量（R-Z-03，诊断归因统计基础）> 题量上限
        # （评审报告 §344 路径①：「约 20 题」改软目标）
        constraints.item_count = constraints.item_count.model_copy(update={"soft": True})
        adjudications.append(
            Adjudication(
                conflict_id=conflict_id,
                constraint_a="item_count.max",
                constraint_b="kp_quotas.min_count",
                decision="soft_target",
                reason=(
                    f"{detail}；按预置优先级裁决：每知识点最低题量为硬约束"
                    f"（R-Z-03），题量上限软目标化（架构评审报告路径①），"
                    f"超出量将在组卷结果 soft_target_achievement 中记录"
                ),
            )
        )
    # min 也不得低于知识点配额合计（否则配额永远不可行且无解说不清）
    if constraints.item_count.min < min_required:
        constraints.item_count = constraints.item_count.model_copy(
            update={"min": min_required}
        )
        adjudications.append(
            Adjudication(
                conflict_id="item_count_min_raised",
                constraint_a="item_count.min",
                constraint_b="kp_quotas.min_count",
                decision="raise_min",
                reason=(
                    f"题量下限上调至知识点最低题量合计 {min_required}，"
                    f"保证配额约束在数学上可达（非放松，是消除自相矛盾）"
                ),
            )
        )

    overlay_refs: dict[str, str] = {}
    for name, ov in (("subject", subject_overlay), ("purpose", purpose_overlay), ("gradeband", gradeband_overlay)):
        if ov and ov.get("overlay_id"):
            overlay_refs[name] = f"{ov['overlay_id']}@{ov.get('overlay_version', '?')}"

    return AssemblyProfile(
        profile_id=profile_id,
        profile_version=profile_version,
        purpose=purpose,
        gradeband=gradeband,
        constraints=constraints,
        adjudications=adjudications,
        overlay_refs=overlay_refs,
    )


def diagnosis_profile(
    *,
    profile_id: str,
    profile_version: str,
    gradeband: Gradeband,
    kp_codes: list[str],
    item_count_range: tuple[int, int] = (20, 20),
    min_items_per_isolated_kp: int = 3,
    target_p_correct_range: Optional[tuple[float, float]] = (0.30, 0.85),
    subject_overlay: Optional[dict[str, Any]] = None,
    gradeband_overlay: Optional[dict[str, Any]] = None,
) -> AssemblyProfile:
    """诊断 Profile 工厂（R-Z-03 三硬约束 + 已知冲突软目标化裁决）.

    - 孤立题强制存在：require_isolated_items=True，kp 配额 isolated_only=True
    - 每知识点最低题量 ≥3（min_items_per_isolated_kp）
    - 多点关系声明核验：multi_point_relation_check=True
    - 「约 20 题」×「每点≥3」已知冲突：编译期按预置优先级把题量上限
      软目标化并记录理由（架构评审报告路径①）
    """
    purpose_overlay: dict[str, Any] = {
        "overlay_id": f"{profile_id}-purpose",
        "overlay_version": profile_version,
        "item_count_range": list(item_count_range),
        "isolation_rules": {
            "require_isolated_items": True,
            "multi_point_relation_check": True,
        },
    }
    if target_p_correct_range is not None:
        purpose_overlay["difficulty_target"] = {
            "target_p_correct_range": list(target_p_correct_range),
        }
    return compile_profile(
        profile_id=profile_id,
        profile_version=profile_version,
        purpose="diagnosis",
        gradeband=gradeband,
        kp_codes=kp_codes,
        subject_overlay=subject_overlay,
        purpose_overlay=purpose_overlay,
        gradeband_overlay=gradeband_overlay,
        min_items_per_kp=min_items_per_isolated_kp,
        allow_item_count_soft=True,
    )


__all__ = [
    "Purpose",
    "Gradeband",
    "MAX_ITEMS_PER_GROUP",
    "DEFAULT_P_CORRECT_MARGIN",
    "ItemCountRule",
    "KpQuota",
    "ContentMixRule",
    "ConstraintSet",
    "Adjudication",
    "AssemblyProfile",
    "ProfileConflictError",
    "compile_profile",
    "diagnosis_profile",
]
