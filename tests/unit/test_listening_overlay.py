"""T-W4-025 听力组卷 overlay 单元测试.

验收对照：
  #1 apply_listening_overlay(paper_spec) 注入听力占比（30–40%）与位置（卷首）硬约束。
  #2 听力题自动标记 testlet_id，子题共享同一音频上下文。
  #3 不可行时（听力素材不足）返回明确冲突原因，不静默放松。
  #4 make accept 全绿。
  #5 不 import 学科包/学段包（A5/X6）。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from src.core.assembly.listening_overlay import (
    LISTENING_RATIO_MAX,
    LISTENING_RATIO_MIN,
    ListeningConflict,
    ListeningOverlay,
    ListeningOverlayResult,
    ListeningOverlaySpec,
    apply_listening_overlay,
    mark_listening_testlet,
)
from src.core.assembly.profile import (
    AssemblyProfile,
    ConstraintSet,
    ItemCountRule,
    compile_profile,
)
from src.core.assembly.solver import AssemblyResult
from src.core.assembly.candidates import CandidateItem


# ════════════════════════════════════════════════════════════════════
# fixtures
# ════════════════════════════════════════════════════════════════════


@pytest.fixture
def paper_spec() -> AssemblyProfile:
    """基础组卷 Profile（20 题，练习用途）."""
    return compile_profile(
        profile_id="test-profile",
        profile_version="1.0.0",
        purpose="practice",
        gradeband="M",
        kp_codes=["eng.listen", "eng.vocab", "eng.grammar"],
    )


@pytest.fixture
def overlay_spec() -> ListeningOverlaySpec:
    """默认听力 overlay 配置."""
    return ListeningOverlaySpec(audio_context_ref="audio:bundle-001")


def _make_candidate(
    item_version_id: str,
    *,
    gradeband: str = "M",
    group_id: str | None = None,
) -> CandidateItem:
    """构造测试用候选题."""
    return CandidateItem(
        item_version_id=item_version_id,
        item_id=f"item-{item_version_id}",
        kp_codes=["eng.listen"],
        kp_set_mode="single",
        gradeband=gradeband,  # type: ignore[arg-type]
        interaction_id="single_choice",
    )


# ════════════════════════════════════════════════════════════════════
# 验收 #1：听力占比与位置硬约束注入
# ════════════════════════════════════════════════════════════════════


class TestApplyListeningOverlay:
    """apply_listening_overlay 注入听力约束测试."""

    def test_overlay_feasible_with_enough_listening_items(
        self, paper_spec: AssemblyProfile, overlay_spec: ListeningOverlaySpec
    ) -> None:
        """素材充足 → feasible=True，overlay 含占比范围 + testlet_id."""
        result = apply_listening_overlay(
            paper_spec,
            available_listening_items=10,
            spec=overlay_spec,
        )
        assert result.feasible is True
        assert result.overlay is not None
        assert isinstance(result.overlay, ListeningOverlay)
        assert result.conflicts == []

    def test_overlay_listening_count_range(
        self, paper_spec: AssemblyProfile, overlay_spec: ListeningOverlaySpec
    ) -> None:
        """听力题量范围 = total_items × [30%, 40%]."""
        result = apply_listening_overlay(
            paper_spec,
            available_listening_items=20,
            spec=overlay_spec,
        )
        assert result.overlay is not None
        total = paper_spec.constraints.item_count.max  # 20
        listen_min, listen_max = result.overlay.listening_item_count_range
        # 20 × 0.30 = 6 (ceil), 20 × 0.40 = 8 (floor)
        assert listen_min == 6
        assert listen_max == 8

    def test_overlay_testlet_id_deterministic(
        self, paper_spec: AssemblyProfile, overlay_spec: ListeningOverlaySpec
    ) -> None:
        """相同 audio_context_ref 产生相同 testlet_id（确定性）."""
        r1 = apply_listening_overlay(
            paper_spec, available_listening_items=10, spec=overlay_spec
        )
        r2 = apply_listening_overlay(
            paper_spec, available_listening_items=10, spec=overlay_spec
        )
        assert r1.overlay is not None
        assert r2.overlay is not None
        assert r1.overlay.testlet_id == r2.overlay.testlet_id
        assert r1.overlay.testlet_id.startswith("testlet:listening:")

    def test_overlay_testlet_id_differs_by_audio_context(
        self, paper_spec: AssemblyProfile
    ) -> None:
        """不同 audio_context_ref 产生不同 testlet_id."""
        spec1 = ListeningOverlaySpec(audio_context_ref="audio:A")
        spec2 = ListeningOverlaySpec(audio_context_ref="audio:B")
        r1 = apply_listening_overlay(
            paper_spec, available_listening_items=10, spec=spec1
        )
        r2 = apply_listening_overlay(
            paper_spec, available_listening_items=10, spec=spec2
        )
        assert r1.overlay is not None
        assert r2.overlay is not None
        assert r1.overlay.testlet_id != r2.overlay.testlet_id

    def test_overlay_default_ratio_30_40(
        self, paper_spec: AssemblyProfile, overlay_spec: ListeningOverlaySpec
    ) -> None:
        """默认占比范围 = (0.30, 0.40)."""
        assert overlay_spec.ratio_range == (0.30, 0.40)
        result = apply_listening_overlay(
            paper_spec, available_listening_items=10, spec=overlay_spec
        )
        assert result.overlay is not None
        assert result.overlay.spec.ratio_range == (LISTENING_RATIO_MIN, LISTENING_RATIO_MAX)

    def test_overlay_position_is_first(
        self, paper_spec: AssemblyProfile, overlay_spec: ListeningOverlaySpec
    ) -> None:
        """位置约束 = 'first'（卷首）."""
        assert overlay_spec.position == "first"
        result = apply_listening_overlay(
            paper_spec, available_listening_items=10, spec=overlay_spec
        )
        assert result.overlay is not None
        assert result.overlay.spec.position == "first"

    def test_overlay_custom_ratio_range(
        self, paper_spec: AssemblyProfile
    ) -> None:
        """可自定义占比范围（如 20%–50%）."""
        spec = ListeningOverlaySpec(
            audio_context_ref="audio:X",
            ratio_range=(0.20, 0.50),
        )
        result = apply_listening_overlay(
            paper_spec, available_listening_items=10, spec=spec
        )
        assert result.feasible is True
        assert result.overlay is not None
        total = paper_spec.constraints.item_count.max  # 20
        listen_min, listen_max = result.overlay.listening_item_count_range
        # 20 × 0.20 = 4, 20 × 0.50 = 10
        assert listen_min == 4
        assert listen_max == 10

    def test_overlay_spec_none_raises(self, paper_spec: AssemblyProfile) -> None:
        """spec=None → ValueError."""
        with pytest.raises(ValueError, match="spec 不能为 None"):
            apply_listening_overlay(
                paper_spec, available_listening_items=10, spec=None
            )

    def test_overlay_spec_invalid_ratio_raises(self) -> None:
        """ratio_range 不满足 0 < min < max < 1 → ValueError."""
        with pytest.raises(ValueError, match="ratio_range 非法"):
            ListeningOverlaySpec(
                audio_context_ref="a",
                ratio_range=(0.5, 0.3),  # min > max
            )
        with pytest.raises(ValueError, match="ratio_range 非法"):
            ListeningOverlaySpec(
                audio_context_ref="a",
                ratio_range=(0.0, 0.4),  # min = 0
            )


# ════════════════════════════════════════════════════════════════════
# 验收 #3：不可行时返回冲突原因
# ════════════════════════════════════════════════════════════════════


class TestListeningInfeasibility:
    """听力素材不足时返回冲突原因（不静默放松）."""

    def test_insufficient_listening_items_returns_conflict(
        self, paper_spec: AssemblyProfile, overlay_spec: ListeningOverlaySpec
    ) -> None:
        """听力候选不足 → feasible=False + 冲突原因."""
        # 20 题需至少 6 道听力（30%），只提供 3 道
        result = apply_listening_overlay(
            paper_spec,
            available_listening_items=3,
            spec=overlay_spec,
        )
        assert result.feasible is False
        assert result.overlay is None
        assert len(result.conflicts) >= 1

        conflict = result.conflicts[0]
        assert isinstance(conflict, ListeningConflict)
        assert conflict.constraint_id == "listening_ratio_min"
        assert conflict.required == 6  # ceil(20 * 0.30)
        assert conflict.available == 3
        assert "听力" in conflict.detail or "listening" in conflict.detail.lower()

    def test_zero_listening_items_returns_conflict(
        self, paper_spec: AssemblyProfile, overlay_spec: ListeningOverlaySpec
    ) -> None:
        """无听力候选 → feasible=False."""
        result = apply_listening_overlay(
            paper_spec,
            available_listening_items=0,
            spec=overlay_spec,
        )
        assert result.feasible is False
        assert len(result.conflicts) >= 1
        assert result.conflicts[0].available == 0

    def test_exact_minimum_is_feasible(
        self, paper_spec: AssemblyProfile, overlay_spec: ListeningOverlaySpec
    ) -> None:
        """刚好满足下限 → feasible=True（边界测试）."""
        # 20 × 0.30 = 6，正好提供 6 道
        result = apply_listening_overlay(
            paper_spec,
            available_listening_items=6,
            spec=overlay_spec,
        )
        assert result.feasible is True
        assert result.overlay is not None

    def test_conflict_does_not_silently_relax(
        self, paper_spec: AssemblyProfile, overlay_spec: ListeningOverlaySpec
    ) -> None:
        """冲突时不静默放松（不返回 overlay，不降低占比）."""
        result = apply_listening_overlay(
            paper_spec,
            available_listening_items=2,
            spec=overlay_spec,
        )
        assert result.feasible is False
        assert result.overlay is None
        # 冲突原因含 required（原始需求，未被放松）
        assert all(c.required is not None for c in result.conflicts)
        assert all(c.required >= 6 for c in result.conflicts if c.constraint_id == "listening_ratio_min")


# ════════════════════════════════════════════════════════════════════
# 验收 #2：testlet 标记 + 置卷首
# ════════════════════════════════════════════════════════════════════


class TestMarkListeningTestlet:
    """mark_listening_testlet 标记 testlet + 重排卷首测试."""

    @pytest.fixture
    def assembly_result(self) -> AssemblyResult:
        """构造组卷结果（10 题，其中前 3 题为听力题但不在卷首）."""
        items = []
        # 3 道非听力题在前
        for i in range(3):
            items.append(_make_candidate(f"item-plain-{i}"))
        # 3 道听力题在中间
        for i in range(3):
            items.append(_make_candidate(f"item-listen-{i}"))
        # 4 道非听力题在后
        for i in range(4):
            items.append(_make_candidate(f"item-plain-{i + 3}"))

        return AssemblyResult(
            items=items,
            snapshot_ref="snap-001",
            profile_id="test-profile",
            profile_version="1.0.0",
            purpose="practice",
            seed=42,
            selection_digest="original-digest",
        )

    @pytest.fixture
    def listening_ids(self) -> frozenset[str]:
        return frozenset(
            [f"item-listen-{i}" for i in range(3)]
        )

    def test_listening_items_marked_with_testlet_id(
        self,
        assembly_result: AssemblyResult,
        overlay_spec: ListeningOverlaySpec,
        listening_ids: frozenset[str],
    ) -> None:
        """听力题被标记 testlet_id."""
        overlay = ListeningOverlay(
            testlet_id="testlet:listening:abc123",
            listening_item_count_range=(3, 8),
            spec=overlay_spec,
        )
        result = mark_listening_testlet(
            assembly_result, overlay,
            listening_item_version_ids=listening_ids,
        )
        # 听力题的 group_id = testlet_id
        for item in result.items:
            if item.item_version_id in listening_ids:
                assert item.group_id == "testlet:listening:abc123"
            else:
                assert item.group_id is None

    def test_listening_items_placed_at_beginning(
        self,
        assembly_result: AssemblyResult,
        overlay_spec: ListeningOverlaySpec,
        listening_ids: frozenset[str],
    ) -> None:
        """听力题置卷首（非听力题保持原序）."""
        overlay = ListeningOverlay(
            testlet_id="testlet:listening:abc123",
            listening_item_count_range=(3, 8),
            spec=overlay_spec,
        )
        result = mark_listening_testlet(
            assembly_result, overlay,
            listening_item_version_ids=listening_ids,
        )
        # 前 3 题应为听力题
        for i in range(3):
            assert result.items[i].item_version_id in listening_ids
        # 后 7 题应为非听力题
        for i in range(3, 10):
            assert result.items[i].item_version_id not in listening_ids

    def test_non_listening_items_keep_order(
        self,
        assembly_result: AssemblyResult,
        overlay_spec: ListeningOverlaySpec,
        listening_ids: frozenset[str],
    ) -> None:
        """非听力题保持原始相对顺序."""
        overlay = ListeningOverlay(
            testlet_id="testlet:listening:abc123",
            listening_item_count_range=(3, 8),
            spec=overlay_spec,
        )
        result = mark_listening_testlet(
            assembly_result, overlay,
            listening_item_version_ids=listening_ids,
        )
        # 非听力题原序：plain-0, plain-1, plain-2, plain-3, ... plain-6
        non_listening = [
            item for item in result.items if item.item_version_id not in listening_ids
        ]
        for i, item in enumerate(non_listening):
            expected_id = f"item-plain-{i}"
            assert item.item_version_id == expected_id

    def test_digest_recalculated_after_reorder(
        self,
        assembly_result: AssemblyResult,
        overlay_spec: ListeningOverlaySpec,
        listening_ids: frozenset[str],
    ) -> None:
        """重排后 selection_digest 重新计算（与原 digest 不同）."""
        overlay = ListeningOverlay(
            testlet_id="testlet:listening:abc123",
            listening_item_count_range=(3, 8),
            spec=overlay_spec,
        )
        result = mark_listening_testlet(
            assembly_result, overlay,
            listening_item_version_ids=listening_ids,
        )
        assert result.selection_digest != "original-digest"
        assert len(result.selection_digest) == 64  # sha256 hex

    def test_count_out_of_range_raises(
        self,
        assembly_result: AssemblyResult,
        overlay_spec: ListeningOverlaySpec,
    ) -> None:
        """听力题数量不在 overlay 范围内 → 抛 ValueError（不静默放松）."""
        # overlay 要求 [6, 8]，但只有 3 道听力题
        overlay = ListeningOverlay(
            testlet_id="testlet:listening:abc123",
            listening_item_count_range=(6, 8),
            spec=overlay_spec,
        )
        listening_ids = frozenset(
            [f"item-listen-{i}" for i in range(3)]
        )
        with pytest.raises(ValueError, match="不在 overlay 范围"):
            mark_listening_testlet(
                assembly_result, overlay,
                listening_item_version_ids=listening_ids,
            )


# ════════════════════════════════════════════════════════════════════
# 验收 #5：不 import 学科包/学段包
# ════════════════════════════════════════════════════════════════════


class TestNoSubjectPackImports:
    """listening_overlay 禁止 import 学科包/学段包（A5/X6）."""

    def test_no_subject_pack_imports(self) -> None:
        """listening_overlay.py 不 import 学科包/学段包."""
        fpath = (
            Path(__file__).resolve().parent.parent.parent
            / "src" / "core" / "assembly" / "listening_overlay.py"
        )
        assert fpath.is_file()
        pattern = re.compile(
            r"^\s*(?:from\s+(?:packs|src\.packs)"
            r"|import\s+(?:packs|src\.packs))",
            re.MULTILINE,
        )
        content = fpath.read_text(encoding="utf-8")
        violations = pattern.findall(content)
        assert not violations, (
            f"listening_overlay.py 存在学科包 import（违反 A5）：{violations}"
        )
