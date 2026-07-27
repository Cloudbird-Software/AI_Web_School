"""§4.4 组卷求解器 v1：候选预算装填启发式（T-W3-assembly S1/S2）.

架构 v2 §4.4：在线出口毫秒级「候选预算装填+加权启发式修补」；
离线 CP-SAT 是 W4 非目标。同一引擎同一题库，仅 Profile 与时间预算不同。

确定性（R-Z-01）：给定（快照 id, Profile 版本, 种子）结果唯一——
- 候选顺序 = sha256(seed:item_version_id) 稳定哈希排序（非 random.shuffle，
  不依赖 PYTHONHASHSEED，跨进程可复现）；
- 贪心装填无时间依赖、无外部状态；
- selection_digest 固化选题结果，供审计比对重放。

不可行处理（§4.4 铁律：禁止静默放松）：任何硬约束不满足 →
抛 InfeasibleError，携带结构化 ConflictReport（每条冲突含约束 id/
知识点/需求量/可用量），缺口报告直接馈送覆盖缺口盘点。

序列梯度单调：输出按预测正确率降序（由易到难）；无先验的题排末尾，
同值按稳定哈希决胜——确定性优先于梯度语义。
（先修闭包参与的梯度排序依赖 kp_closure 快照，v1 留给调用方在
快照维度保证范围单调，见遗留问题。）

宪法 A5/A7：本模块不 import 任何学科包/学段包。
"""
from __future__ import annotations

import hashlib
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from src.core.assembly.candidates import CandidateItem
from src.core.assembly.profile import Adjudication, AssemblyProfile


# ════════════════════════════════════════════════════════════════════
# 冲突报告（不可行的结构化输出）
# ════════════════════════════════════════════════════════════════════

class ConflictReason(BaseModel):
    """单条冲突原因（禁止静默放松的载体）."""

    model_config = ConfigDict(extra="forbid")

    constraint_id: str
    detail: str
    kp_code: Optional[str] = None
    required: Optional[int] = None
    available: Optional[int] = None


class ConflictReport(BaseModel):
    """不可行报告：组卷输入三要素 + 全部冲突 + 池规模（馈送缺口盘点）."""

    model_config = ConfigDict(extra="forbid")

    snapshot_ref: str
    profile_id: str
    profile_version: str
    purpose: str
    pool_size: int
    eligible_size: int
    drop_reasons: dict[str, int] = Field(default_factory=dict)
    conflicts: list[ConflictReason]


class InfeasibleError(ValueError):
    """硬约束不可行。report 为结构化冲突原因（§4.4：必须返回冲突原因）."""

    def __init__(self, report: ConflictReport) -> None:
        self.report = report
        summary = "; ".join(c.detail for c in report.conflicts)
        super().__init__(f"组卷不可行（{len(report.conflicts)} 条冲突）：{summary}")


# ════════════════════════════════════════════════════════════════════
# 组卷结果
# ════════════════════════════════════════════════════════════════════

class AssemblyResult(BaseModel):
    """组卷结果：已排序选题 + 确定性留档 + 软目标达成情况."""

    model_config = ConfigDict(extra="forbid")

    items: list[CandidateItem]
    snapshot_ref: str
    profile_id: str
    profile_version: str
    purpose: str
    seed: int
    adjudications: list[Adjudication] = Field(default_factory=list)
    soft_target_achievement: dict[str, object] = Field(default_factory=dict)
    selection_digest: str


# ════════════════════════════════════════════════════════════════════
# 内部结构：选择单元（题组整体入选/排除）
# ════════════════════════════════════════════════════════════════════

class _Unit:
    """选择单元：单题或整个题组（题组≤6，R-Z-06）."""

    __slots__ = ("members", "sort_key", "kp_codes", "template_version_ids", "is_isolated")

    def __init__(self, members: list[CandidateItem], sort_key: str) -> None:
        self.members = members
        self.sort_key = sort_key
        kps: list[str] = []
        for m in members:
            for c in m.kp_codes:
                if c not in kps:
                    kps.append(c)
        self.kp_codes = kps
        self.template_version_ids = {
            m.template_version_id for m in members if m.template_version_id
        }
        self.is_isolated = len(members) == 1 and members[0].is_isolated

    @property
    def size(self) -> int:
        return len(self.members)


def _stable_key(seed: int, item_version_id: str) -> str:
    """确定性排序键：sha256(seed:id)。跨进程可复现（不依赖 hash()）."""
    return hashlib.sha256(f"{seed}:{item_version_id}".encode("utf-8")).hexdigest()


# ════════════════════════════════════════════════════════════════════
# 候选筛选（学段×用途许可×曝光历史×目标正确率×诊断关系核验）
# ════════════════════════════════════════════════════════════════════

def _filter_eligible(
    profile: AssemblyProfile,
    candidates: list[CandidateItem],
    kp_scope: set[str],
    excluded_item_version_ids: frozenset[str],
    excluded_template_version_ids: frozenset[str],
) -> tuple[list[CandidateItem], dict[str, int]]:
    """候选筛选，返回（合格候选, 淘汰原因计数）.

    淘汰原因计数进 ConflictReport.drop_reasons——缺口盘点的原料。
    """
    c = profile.constraints
    eligible: list[CandidateItem] = []
    drops: dict[str, int] = {}

    def drop(reason: str) -> None:
        drops[reason] = drops.get(reason, 0) + 1

    widened: Optional[tuple[float, float]] = None
    if c.target_p_correct_range is not None:
        lo, hi = c.target_p_correct_range
        m = c.p_correct_uncertainty_margin
        # 冷启动降级（§4.4）：纯先验区间 + 保守宽度
        widened = (max(0.0, lo - m), min(1.0, hi + m))

    for item in candidates:
        if item.gradeband != profile.gradeband:
            drop("gradeband_mismatch")
            continue
        if not any(k in kp_scope for k in item.kp_codes):
            drop("kp_out_of_scope")
            continue
        if profile.purpose not in item.allowed_purposes:
            drop("purpose_not_licensed")
            continue
        if c.exposure_mutex_cross_period and item.item_version_id in excluded_item_version_ids:
            drop("exposed_item")
            continue
        if (
            c.exposure_mutex_same_template
            and item.template_version_id
            and item.template_version_id in excluded_template_version_ids
        ):
            drop("exposed_template")
            continue
        if widened is not None:
            if item.p_correct_prior is None:
                # 无先验且 Profile 要求正确率区间：无法代入约束，淘汰并记录
                drop("missing_p_correct_prior")
                continue
            if not (widened[0] <= item.p_correct_prior <= widened[1]):
                drop("p_correct_out_of_range")
                continue
        if c.multi_point_relation_check and not item.is_isolated:
            # R-Z-03 多点关系声明核验：多点题必须显式声明
            # all_required / compensatory；声明 single 而挂多点是自相矛盾
            if item.kp_set_mode not in ("all_required", "compensatory"):
                drop("relation_declaration_invalid")
                continue
        eligible.append(item)
    return eligible, drops


# ════════════════════════════════════════════════════════════════════
# 主入口
# ════════════════════════════════════════════════════════════════════

def assemble(
    profile: AssemblyProfile,
    candidates: list[CandidateItem],
    *,
    seed: int,
    snapshot_ref: str,
    excluded_item_version_ids: frozenset[str] = frozenset(),
    excluded_template_version_ids: frozenset[str] = frozenset(),
) -> AssemblyResult:
    """确定性预算装填组卷.

    参数:
        profile: 编译后的版本化 Profile（compile_profile / diagnosis_profile 产物）
        candidates: 候选池（load_candidates 或手工构造；须为同一次快照内容）
        seed: 确定性种子
        snapshot_ref: 内容快照引用（确定性三要素之一，留档）
        excluded_item_version_ids / excluded_template_version_ids:
            曝光集（曝光账本查询结果；学生轨或周队列轨）

    返回:
        AssemblyResult（items 已按梯度单调排序）

    异常:
        InfeasibleError: 硬约束不可行，report 含结构化冲突原因（禁止静默放松）
    """
    c = profile.constraints
    kp_scope = {q.kp_code for q in c.kp_quotas}

    eligible, drops = _filter_eligible(
        profile, candidates, kp_scope,
        excluded_item_version_ids, excluded_template_version_ids,
    )

    # ── 组单元（题组整体；单题自成单元），确定性排序 ──
    groups: dict[str, list[CandidateItem]] = {}
    singles: list[CandidateItem] = []
    for item in eligible:
        if item.group_id:
            groups.setdefault(item.group_id, []).append(item)
        else:
            singles.append(item)

    conflicts: list[ConflictReason] = []
    units: list[_Unit] = []
    for item in singles:
        units.append(_Unit([item], _stable_key(seed, item.item_version_id)))
    for gid, members in groups.items():
        members = sorted(members, key=lambda m: _stable_key(seed, m.item_version_id))
        if len(members) > c.max_items_per_group:
            conflicts.append(
                ConflictReason(
                    constraint_id="max_items_per_group",
                    detail=(
                        f"题组 {gid} 含 {len(members)} 题，"
                        f"超过题组上限 {c.max_items_per_group}（R-Z-06）"
                    ),
                    required=c.max_items_per_group,
                    available=len(members),
                )
            )
            continue
        units.append(_Unit(members, _stable_key(seed, gid)))
    units.sort(key=lambda u: u.sort_key)

    # ── 装填 ──
    selected: list[_Unit] = []
    used_templates: set[str] = set()

    def template_ok(unit: _Unit) -> bool:
        if not c.exposure_mutex_same_template:
            return True
        # R-Z-02 同母题不同卷：同卷内同母题实例至多一个
        return not (unit.template_version_ids & used_templates)

    def take(unit: _Unit) -> None:
        selected.append(unit)
        used_templates.update(unit.template_version_ids)

    def kp_count(kp_code: str, isolated_only: bool) -> int:
        n = 0
        for u in selected:
            if isolated_only:
                if u.is_isolated and u.kp_codes == [kp_code]:
                    n += u.size
            elif kp_code in u.kp_codes:
                n += u.size
        return n

    def total_items() -> int:
        return sum(u.size for u in selected)

    # 阶段 A：知识点配额定题（诊断：孤立题配额）
    for quota in c.kp_quotas:
        deficit = quota.min_count - kp_count(quota.kp_code, quota.isolated_only)
        for unit in units:
            if deficit <= 0:
                break
            if unit in selected:
                continue
            if quota.isolated_only:
                if not (unit.is_isolated and unit.kp_codes == [quota.kp_code]):
                    continue
            elif quota.kp_code not in unit.kp_codes:
                continue
            if not template_ok(unit):
                continue
            take(unit)
            deficit = quota.min_count - kp_count(quota.kp_code, quota.isolated_only)
        if deficit > 0:
            conflicts.append(
                ConflictReason(
                    constraint_id=(
                        "kp_quota_isolated" if quota.isolated_only else "kp_quota"
                    ),
                    detail=(
                        f"知识点 {quota.kp_code} 需要"
                        f"{'孤立题' if quota.isolated_only else '题'}≥{quota.min_count}，"
                        f"合格池仅可提供 {kp_count(quota.kp_code, quota.isolated_only)}"
                    ),
                    kp_code=quota.kp_code,
                    required=quota.min_count,
                    available=kp_count(quota.kp_code, quota.isolated_only),
                )
            )

    # 阶段 B：题量下限装填（内容配比软目标加权：配比缺口大的标签优先）
    def mix_deficit(tag: Optional[str]) -> float:
        if c.content_mix is None or tag is None:
            return 0.0
        target = c.content_mix.ratios.get(tag)
        if target is None:
            return 0.0
        total = total_items()
        current = sum(
            u.size for u in selected if any(m.mix_tag == tag for m in u.members)
        )
        # 缺口 = 目标下限×(当前总数+1) − 已有；>0 表示该标签欠账
        return target[0] * (total + 1) - current

    while total_items() < c.item_count.min:
        best: Optional[_Unit] = None
        best_score = 0.0
        for unit in units:
            if unit in selected or not template_ok(unit):
                continue
            if not c.item_count.soft and total_items() + unit.size > c.item_count.max:
                continue
            score = max((mix_deficit(m.mix_tag) for m in unit.members), default=0.0)
            if best is None or score > best_score:
                best = unit
                best_score = score
        if best is None:
            conflicts.append(
                ConflictReason(
                    constraint_id="item_count",
                    detail=(
                        f"题量下限 {c.item_count.min} 不可达：合格池 "
                        f"{len(eligible)} 题，已装填 {total_items()}，"
                        f"剩余候选受题量上限/同母题互斥约束不可用"
                    ),
                    required=c.item_count.min,
                    available=total_items(),
                )
            )
            break
        take(best)

    # 阶段 C：硬上限校验（soft 上限超出不判不可行，记录达成情况）
    achievement: dict[str, object] = {}
    if c.item_count.soft and total_items() > c.item_count.max:
        achievement["item_count"] = {
            "soft_max": c.item_count.max,
            "actual": total_items(),
            "exceeded_by": total_items() - c.item_count.max,
        }

    if conflicts:
        raise InfeasibleError(
            ConflictReport(
                snapshot_ref=snapshot_ref,
                profile_id=profile.profile_id,
                profile_version=profile.profile_version,
                purpose=profile.purpose,
                pool_size=len(candidates),
                eligible_size=len(eligible),
                drop_reasons=drops,
                conflicts=conflicts,
            )
        )

    # ── 序列梯度单调：预测正确率降序（由易到难），无先验排末尾 ──
    ordered_items: list[CandidateItem] = []
    if c.gradient_monotone:
        flat = [m for u in selected for m in u.members]
        flat.sort(
            key=lambda m: (
                m.p_correct_prior is None,
                -(m.p_correct_prior if m.p_correct_prior is not None else 0.0),
                _stable_key(seed, m.item_version_id),
            )
        )
        ordered_items = flat
    else:
        ordered_items = [m for u in selected for m in u.members]

    digest = hashlib.sha256(
        "|".join(m.item_version_id for m in ordered_items).encode("utf-8")
    ).hexdigest()

    return AssemblyResult(
        items=ordered_items,
        snapshot_ref=snapshot_ref,
        profile_id=profile.profile_id,
        profile_version=profile.profile_version,
        purpose=profile.purpose,
        seed=seed,
        adjudications=list(profile.adjudications),
        soft_target_achievement=achievement,
        selection_digest=digest,
    )


__all__ = [
    "ConflictReason",
    "ConflictReport",
    "InfeasibleError",
    "AssemblyResult",
    "assemble",
]
