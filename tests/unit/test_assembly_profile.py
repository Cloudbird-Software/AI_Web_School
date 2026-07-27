"""T-W3-assembly S1/S2 约束集四维编译单元测试.

对照任务卡验收：
1. 约束集编译：题量/知识点配比/目标正确率区间/序列梯度/曝光互斥/题组≤6
2. Profile 版本化 + 确定性指纹
3. 冲突检测与预置优先级裁决（约20题×每点≥3 → 软目标化并记录理由）
4. 诊断 Profile：孤立题强制/每点≥3/多点关系核验默认开启
5. 禁止静默放松的对偶：不允许软目标化时编译期即抛 ProfileConflictError
"""
from __future__ import annotations

import pytest

from src.core.assembly import (
    ProfileConflictError,
    compile_profile,
    diagnosis_profile,
)


# ────────────────────────────────────────────────────────────────────
# 基础编译
# ────────────────────────────────────────────────────────────────────

def test_compile_practice_defaults() -> None:
    """练习用途：每知识点默认 ≥1 题，题量取 overlay，无裁决."""
    prof = compile_profile(
        profile_id="practice-weekly",
        profile_version="1.0.0",
        purpose="practice",
        gradeband="M",
        kp_codes=["math.a", "math.b"],
        purpose_overlay={"item_count_range": [10, 15]},
    )
    c = prof.constraints
    assert c.item_count.min == 10 and c.item_count.max == 15
    assert not c.item_count.soft
    assert [(q.kp_code, q.min_count, q.isolated_only) for q in c.kp_quotas] == [
        ("math.a", 1, False),
        ("math.b", 1, False),
    ]
    assert c.gradient_monotone is True
    assert c.exposure_mutex_same_template is True
    assert c.exposure_mutex_cross_period is True
    assert c.max_items_per_group == 6
    assert not c.require_isolated_items
    assert prof.adjudications == []


def test_overlay_merge_priority_gradeband_wins() -> None:
    """四维合并优先级：gradeband_overlay > purpose_overlay > subject_overlay > base."""
    prof = compile_profile(
        profile_id="p",
        profile_version="1.0.0",
        purpose="practice",
        gradeband="L",
        kp_codes=["math.a"],
        base={"item_count_range": [10, 20]},
        subject_overlay={"item_count_range": [8, 15]},
        purpose_overlay={"item_count_range": [5, 12]},
        gradeband_overlay={"item_count_range": [4, 8]},
    )
    assert (prof.constraints.item_count.min, prof.constraints.item_count.max) == (4, 8)


def test_subject_overlay_assembly_constraints() -> None:
    """学科 overlay 的 assembly_constraints 维度被编译进约束集（曝光互斥/梯度/配比）."""
    subject_overlay = {
        "overlay_id": "subject-math",
        "overlay_version": "1.0.0",
        "assembly_constraints": {
            "require_gradient_monotone": True,
            "exposure_mutex": {
                "same_template_different_paper": True,
                "cross_period_repeat": False,  # 不允许跨期重复 = 互斥开
            },
            "content_mix": {
                "new_learning_ratio": [0.4, 0.6],
                "review_ratio": [0.2, 0.4],
            },
        },
    }
    prof = compile_profile(
        profile_id="p",
        profile_version="1.0.0",
        purpose="practice",
        gradeband="M",
        kp_codes=["math.a"],
        subject_overlay=subject_overlay,
        purpose_overlay={"item_count_range": [10, 15]},
    )
    c = prof.constraints
    assert c.exposure_mutex_same_template is True
    assert c.exposure_mutex_cross_period is True
    assert c.content_mix is not None
    assert c.content_mix.ratios["new"] == (0.4, 0.6)
    assert c.content_mix.ratios["review"] == (0.2, 0.4)
    assert prof.overlay_refs["subject"] == "subject-math@1.0.0"


def test_target_p_correct_range_compiled() -> None:
    """目标正确率区间 + 冷启动保守宽度进入约束集."""
    prof = compile_profile(
        profile_id="p",
        profile_version="1.0.0",
        purpose="practice",
        gradeband="M",
        kp_codes=["math.a"],
        purpose_overlay={
            "item_count_range": [8, 15],
            "difficulty_target": {
                "target_p_correct_range": [0.70, 0.90],
                "uncertainty_margin": 0.05,
            },
        },
    )
    assert prof.constraints.target_p_correct_range == (0.70, 0.90)
    assert prof.constraints.p_correct_uncertainty_margin == 0.05


# ────────────────────────────────────────────────────────────────────
# 诊断 Profile（R-Z-03）
# ────────────────────────────────────────────────────────────────────

def test_diagnosis_profile_hard_constraints() -> None:
    """诊断 Profile：孤立题强制、每点≥3、多点关系核验默认开启."""
    prof = diagnosis_profile(
        profile_id="diag-unit5",
        profile_version="1.0.0",
        gradeband="M",
        kp_codes=["math.a", "math.b"],
        item_count_range=(20, 20),
    )
    c = prof.constraints
    assert prof.purpose == "diagnosis"
    assert c.require_isolated_items is True
    assert c.multi_point_relation_check is True
    assert all(q.isolated_only and q.min_count == 3 for q in c.kp_quotas)
    # 2 点 × 3 = 6 ≤ 20，无冲突
    assert prof.adjudications == []
    assert not c.item_count.soft


def test_diagnosis_conflict_soft_target_adjudication() -> None:
    """已知冲突：7 知识点 × 每点≥3 = 21 > 约20题 → 题量上限软目标化并留档.

    架构评审报告 §344 路径①；裁决发生在编译期，理由必须可审计。
    """
    prof = diagnosis_profile(
        profile_id="diag-unit9",
        profile_version="1.0.0",
        gradeband="M",
        kp_codes=[f"math.{c}" for c in "abcdefg"],
        item_count_range=(20, 20),
    )
    c = prof.constraints
    assert c.item_count.soft is True
    assert c.item_count.max == 20  # 软目标原值保留（记录超出量用）
    assert c.item_count.min == 21  # 下限上调至配额合计（消除自相矛盾）

    decisions = {a.conflict_id: a for a in prof.adjudications}
    assert "item_count_vs_kp_quota" in decisions
    adj = decisions["item_count_vs_kp_quota"]
    assert adj.decision == "soft_target"
    assert adj.constraint_a == "item_count.max"
    assert adj.constraint_b == "kp_quotas.min_count"
    assert "R-Z-03" in adj.reason
    assert "21" in adj.reason


def test_compile_conflict_hard_mode_raises() -> None:
    """不允许软目标化（严格模式）：同一冲突在编译期抛 ProfileConflictError."""
    with pytest.raises(ProfileConflictError) as exc_info:
        compile_profile(
            profile_id="p",
            profile_version="1.0.0",
            purpose="diagnosis",
            gradeband="M",
            kp_codes=[f"math.{c}" for c in "abcdefg"],
            purpose_overlay={"item_count_range": [20, 20]},
            allow_item_count_soft=False,
        )
    assert exc_info.value.conflict_id == "item_count_vs_kp_quota"


# ────────────────────────────────────────────────────────────────────
# 版本化与确定性指纹
# ────────────────────────────────────────────────────────────────────

def test_profile_digest_deterministic_and_version_sensitive() -> None:
    """Profile 指纹：同内容必同指纹；版本/约束变化 → 指纹变化."""
    kwargs = dict(
        profile_id="diag",
        gradeband="M",
        kp_codes=["math.a", "math.b"],
        item_count_range=(20, 20),
    )
    p1 = diagnosis_profile(profile_version="1.0.0", **kwargs)
    p2 = diagnosis_profile(profile_version="1.0.0", **kwargs)
    p3 = diagnosis_profile(profile_version="1.0.1", **kwargs)
    assert p1.digest() == p2.digest()
    assert p1.digest() != p3.digest()
