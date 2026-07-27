"""T-W3-assembly S1/S2 组卷求解器单元测试（纯函数，无 DB）.

对照任务卡验收：
1. 约束满足：题量/知识点配比/目标正确率区间/序列梯度单调/题组≤6
2. 不可行返回结构化冲突原因（禁止静默放松）
3. 确定性重放：同（快照+Profile版本+种子）→ 同选题同序
4. 曝光互斥：同母题不同卷、跨期不重复（excluded_* 入参）
5. 诊断硬约束：孤立题配额/多点关系声明核验
"""
from __future__ import annotations

import pytest

from src.core.assembly import (
    AssemblyProfile,
    CandidateItem,
    InfeasibleError,
    assemble,
    compile_profile,
    diagnosis_profile,
)


# ────────────────────────────────────────────────────────────────────
# 构造辅助
# ────────────────────────────────────────────────────────────────────

def _mk(
    vid: str,
    kp: list[str],
    *,
    p: float | None = 0.6,
    mode: str = "single",
    tpl: str | None = None,
    purposes: list[str] | None = None,
    gradeband: str = "M",
    mix_tag: str | None = None,
    group_id: str | None = None,
) -> CandidateItem:
    kwargs: dict = dict(
        item_version_id=vid,
        item_id=f"item-{vid}",
        template_version_id=tpl if tpl is not None else f"tpl-{vid}",
        kp_codes=kp,
        kp_set_mode=mode,
        gradeband=gradeband,
        interaction_id="single_choice",
        p_correct_prior=p,
        mix_tag=mix_tag,
        group_id=group_id,
    )
    if purposes is not None:
        kwargs["allowed_purposes"] = purposes
    return CandidateItem(**kwargs)


def _practice_profile(kps: list[str], count: tuple[int, int] = (6, 10)) -> AssemblyProfile:
    return compile_profile(
        profile_id="practice",
        profile_version="1.0.0",
        purpose="practice",
        gradeband="M",
        kp_codes=kps,
        purpose_overlay={"item_count_range": list(count)},
    )


# ────────────────────────────────────────────────────────────────────
# 约束满足
# ────────────────────────────────────────────────────────────────────

def test_practice_assemble_satisfies_constraints() -> None:
    """练习组卷：题量达标、每知识点配额满足、梯度由易到难单调."""
    prof = _practice_profile(["math.a", "math.b"], count=(6, 10))
    pool = [
        _mk(f"{kp}{i}", [kp], p=0.40 + 0.05 * i)
        for kp in ["math.a", "math.b"]
        for i in range(5)
    ]
    res = assemble(prof, pool, seed=7, snapshot_ref="snap-1")
    assert len(res.items) >= 6
    assert len(res.items) <= 10
    for kp in ["math.a", "math.b"]:
        assert sum(1 for it in res.items if kp in it.kp_codes) >= 1
    ps = [it.p_correct_prior for it in res.items]
    assert ps == sorted(ps, reverse=True), "序列梯度必须单调（由易到难）"
    assert res.seed == 7 and res.snapshot_ref == "snap-1"
    assert res.profile_version == "1.0.0"


def test_gradient_none_prior_goes_last() -> None:
    """无正确率先验的题在梯度排序中排末尾（排序仍确定性）."""
    prof = compile_profile(
        profile_id="p",
        profile_version="1.0.0",
        purpose="practice",
        gradeband="M",
        kp_codes=["math.a"],
        purpose_overlay={"item_count_range": [3, 5]},
    )
    pool = [
        _mk("a", ["math.a"], p=0.5),
        _mk("b", ["math.a"], p=None),
        _mk("c", ["math.a"], p=0.8),
    ]
    res = assemble(prof, pool, seed=1, snapshot_ref="s")
    assert res.items[-1].item_version_id == "b"
    assert [it.p_correct_prior for it in res.items[:2]] == [0.8, 0.5]


def test_target_p_correct_range_filters_candidates() -> None:
    """目标正确率区间：区间外（含保守加宽后）淘汰；加宽生效."""
    prof = compile_profile(
        profile_id="p",
        profile_version="1.0.0",
        purpose="practice",
        gradeband="M",
        kp_codes=["math.a"],
        purpose_overlay={
            "item_count_range": [2, 4],
            "difficulty_target": {
                "target_p_correct_range": [0.70, 0.90],
                "uncertainty_margin": 0.10,
            },
        },
    )
    pool = [
        _mk("in1", ["math.a"], p=0.75),   # 区间内
        _mk("in2", ["math.a"], p=0.62),   # 区间外但加宽 [0.60,1.00] 内
        _mk("out", ["math.a"], p=0.30),   # 加宽后仍区间外
    ]
    res = assemble(prof, pool, seed=1, snapshot_ref="s")
    ids = {it.item_version_id for it in res.items}
    assert ids == {"in1", "in2"}

    with pytest.raises(InfeasibleError) as exc_info:
        assemble(prof, [_mk("out", ["math.a"], p=0.30)], seed=1, snapshot_ref="s")
    report = exc_info.value.report
    assert report.drop_reasons.get("p_correct_out_of_range") == 1


def test_missing_prior_dropped_when_range_required() -> None:
    """Profile 要求正确率区间而题无先验：淘汰并记录原因（不静默放行）."""
    prof = compile_profile(
        profile_id="p",
        profile_version="1.0.0",
        purpose="practice",
        gradeband="M",
        kp_codes=["math.a"],
        purpose_overlay={
            "item_count_range": [1, 2],
            "difficulty_target": {"target_p_correct_range": [0.5, 0.9]},
        },
    )
    with pytest.raises(InfeasibleError) as exc_info:
        assemble(prof, [_mk("x", ["math.a"], p=None)], seed=1, snapshot_ref="s")
    assert exc_info.value.report.drop_reasons.get("missing_p_correct_prior") == 1


def test_group_selected_as_unit_and_max_six_enforced() -> None:
    """题组整体入选（不拆组）；题组 >6 题报结构化冲突（R-Z-06）."""
    prof = _practice_profile(["math.a"], count=(3, 6))
    group_members = [
        _mk(f"g{i}", ["math.a"], group_id="grp-1", p=0.6) for i in range(3)
    ]
    res = assemble(prof, group_members, seed=1, snapshot_ref="s")
    assert {it.item_version_id for it in res.items} == {"g0", "g1", "g2"}

    too_big = [
        _mk(f"b{i}", ["math.a"], group_id="grp-big", p=0.6) for i in range(7)
    ]
    with pytest.raises(InfeasibleError) as exc_info:
        assemble(prof, too_big, seed=1, snapshot_ref="s")
    conflicts = exc_info.value.report.conflicts
    assert any(c.constraint_id == "max_items_per_group" for c in conflicts)


# ────────────────────────────────────────────────────────────────────
# 不可行：结构化冲突原因（禁止静默放松）
# ────────────────────────────────────────────────────────────────────

def test_infeasible_kp_quota_structured_report() -> None:
    """知识点配额不足 → InfeasibleError.report 含约束 id/知识点/需求/可用量."""
    prof = diagnosis_profile(
        profile_id="diag",
        profile_version="1.0.0",
        gradeband="M",
        kp_codes=["math.a", "math.b"],
        item_count_range=(6, 20),
    )
    pool = [
        _mk(f"a{i}", ["math.a"], p=0.5) for i in range(3)
    ] + [
        _mk("b0", ["math.b"], p=0.5),  # math.b 只有 1 题孤立题，需 ≥3
    ]
    with pytest.raises(InfeasibleError) as exc_info:
        assemble(prof, pool, seed=1, snapshot_ref="snap-x")
    report = exc_info.value.report
    assert report.snapshot_ref == "snap-x"
    assert report.profile_id == "diag"
    assert report.purpose == "diagnosis"
    assert report.pool_size == 4
    quota_conflicts = [c for c in report.conflicts if c.constraint_id == "kp_quota_isolated"]
    assert len(quota_conflicts) == 1
    assert quota_conflicts[0].kp_code == "math.b"
    assert quota_conflicts[0].required == 3
    assert quota_conflicts[0].available == 1


def test_infeasible_item_count_unreachable() -> None:
    """题量下限不可达 → 报 item_count 冲突，不静默减量."""
    prof = _practice_profile(["math.a"], count=(5, 8))
    pool = [_mk(f"a{i}", ["math.a"], p=0.5) for i in range(3)]
    with pytest.raises(InfeasibleError) as exc_info:
        assemble(prof, pool, seed=1, snapshot_ref="s")
    conflicts = exc_info.value.report.conflicts
    assert any(c.constraint_id == "item_count" for c in conflicts)


# ────────────────────────────────────────────────────────────────────
# 确定性重放
# ────────────────────────────────────────────────────────────────────

def test_deterministic_replay_same_seed() -> None:
    """同（快照+Profile版本+种子）→ selection_digest 与题序完全一致."""
    prof = _practice_profile(["math.a", "math.b"], count=(6, 8))
    pool = [
        _mk(f"{kp}{i}", [kp], p=0.4 + 0.04 * i)
        for kp in ["math.a", "math.b"]
        for i in range(6)
    ]
    r1 = assemble(prof, pool, seed=42, snapshot_ref="snap")
    r2 = assemble(prof, pool, seed=42, snapshot_ref="snap")
    assert r1.selection_digest == r2.selection_digest
    assert [i.item_version_id for i in r1.items] == [i.item_version_id for i in r2.items]


def test_different_seed_may_change_selection() -> None:
    """不同种子 → 选题/顺序不同（固定种子对，验证后永久确定）."""
    prof = _practice_profile(["math.a"], count=(4, 6))
    pool = [_mk(f"a{i}", ["math.a"], p=0.5) for i in range(10)]
    r1 = assemble(prof, pool, seed=1, snapshot_ref="s")
    r2 = assemble(prof, pool, seed=2, snapshot_ref="s")
    assert r1.selection_digest != r2.selection_digest


# ────────────────────────────────────────────────────────────────────
# 曝光互斥（R-Z-02）
# ────────────────────────────────────────────────────────────────────

def test_same_template_mutex_within_paper() -> None:
    """同母题不同卷：同卷内同 template_version_id 的实例至多一个."""
    prof = _practice_profile(["math.a"], count=(2, 4))
    pool = [
        _mk("v1", ["math.a"], tpl="tpl-X", p=0.6),
        _mk("v2", ["math.a"], tpl="tpl-X", p=0.6),
        _mk("v3", ["math.a"], tpl="tpl-Y", p=0.6),
    ]
    res = assemble(prof, pool, seed=1, snapshot_ref="s")
    tpls = [it.template_version_id for it in res.items]
    assert len(tpls) == len(set(tpls)), "同卷出现同母题两个实例"


def test_cross_period_exclusion_via_exposure_sets() -> None:
    """跨期不重复：excluded_item/template 集合中的候选全部淘汰并计数."""
    prof = _practice_profile(["math.a"], count=(1, 4))
    pool = [
        _mk("v1", ["math.a"], tpl="tpl-X", p=0.6),
        _mk("v2", ["math.a"], tpl="tpl-Y", p=0.6),
        _mk("v3", ["math.a"], tpl="tpl-Z", p=0.6),
    ]
    res = assemble(
        prof,
        pool,
        seed=1,
        snapshot_ref="s",
        excluded_item_version_ids=frozenset({"v1"}),
        excluded_template_version_ids=frozenset({"tpl-Y"}),
    )
    ids = {it.item_version_id for it in res.items}
    assert ids == {"v3"}

    with pytest.raises(InfeasibleError) as exc_info:
        assemble(
            prof,
            pool,
            seed=1,
            snapshot_ref="s",
            excluded_item_version_ids=frozenset({"v1", "v2"}),
            excluded_template_version_ids=frozenset({"tpl-Z"}),
        )
    drops = exc_info.value.report.drop_reasons
    assert drops.get("exposed_item") == 2
    assert drops.get("exposed_template") == 1


# ────────────────────────────────────────────────────────────────────
# 诊断硬约束（R-Z-03）
# ────────────────────────────────────────────────────────────────────

def test_diagnosis_isolated_quota_excludes_multi_kp_items() -> None:
    """诊断孤立题配额：多点题（all_required/compensatory）只佐证不定位，不计入配额."""
    prof = diagnosis_profile(
        profile_id="diag",
        profile_version="1.0.0",
        gradeband="M",
        kp_codes=["math.a"],
        item_count_range=(3, 10),
    )
    pool = [
        _mk("iso1", ["math.a"], p=0.5),
        _mk("iso2", ["math.a"], p=0.5),
        # 多点题声明 all_required：合法但不算孤立题
        _mk("multi1", ["math.a", "math.b"], mode="all_required", p=0.5),
    ]
    with pytest.raises(InfeasibleError) as exc_info:
        assemble(prof, pool, seed=1, snapshot_ref="s")
    conflicts = exc_info.value.report.conflicts
    assert any(
        c.constraint_id == "kp_quota_isolated" and c.available == 2
        for c in conflicts
    )


def test_diagnosis_relation_declaration_check() -> None:
    """多点关系声明核验：挂多知识点却声明 single → 淘汰并记录原因."""
    prof = diagnosis_profile(
        profile_id="diag",
        profile_version="1.0.0",
        gradeband="M",
        kp_codes=["math.a"],
        item_count_range=(3, 10),
    )
    bad = _mk("bad", ["math.a", "math.b"], mode="single", p=0.5)
    with pytest.raises(InfeasibleError) as exc_info:
        assemble(prof, [bad], seed=1, snapshot_ref="s")
    drops = exc_info.value.report.drop_reasons
    assert drops.get("relation_declaration_invalid") == 1


def test_diagnosis_soft_target_achievement_recorded() -> None:
    """「约20题×每点≥3」软目标化裁决：组卷成功且超出量留档."""
    kps = [f"math.{c}" for c in "abcdefg"]
    prof = diagnosis_profile(
        profile_id="diag",
        profile_version="1.0.0",
        gradeband="M",
        kp_codes=kps,
        item_count_range=(20, 20),
    )
    pool = [_mk(f"{kp}{i}", [kp], p=0.5) for kp in kps for i in range(3)]
    res = assemble(prof, pool, seed=1, snapshot_ref="s")
    assert len(res.items) == 21
    assert res.soft_target_achievement["item_count"] == {
        "soft_max": 20,
        "actual": 21,
        "exceeded_by": 1,
    }
    # 裁决理由随结果留档（可审计）
    assert any(a.decision == "soft_target" for a in res.adjudications)


def test_purpose_license_filters_candidates() -> None:
    """用途许可：未许可 diagnosis 的题在诊断组卷中淘汰并计数."""
    prof = diagnosis_profile(
        profile_id="diag",
        profile_version="1.0.0",
        gradeband="M",
        kp_codes=["math.a"],
        item_count_range=(3, 10),
    )
    pool = [
        _mk("ok1", ["math.a"], p=0.5),
        _mk("ok2", ["math.a"], p=0.5),
        _mk("ok3", ["math.a"], p=0.5),
        _mk("practice-only", ["math.a"], p=0.5, purposes=["practice"]),
    ]
    res = assemble(prof, pool, seed=1, snapshot_ref="s")
    ids = {it.item_version_id for it in res.items}
    assert "practice-only" not in ids
