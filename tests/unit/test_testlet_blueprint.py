"""T-W4-015 题组蓝图编排 + ItemGroup 装配单元测试.

验收对照：
  #1 assemble_testlet 返回题组结构，含子题列表、题序、共享上下文标记。
  #2 子题数 2-6，超限报错；知识点不重复（或明确标记重复意图）。
  #3 testlet 标记写入 item_group（契约偏差：ItemVersion 无 testlet_id/group_index 列）。
  #4 make accept 全绿。
  #5 不 import 学科包/学段包。

契约偏差说明（验收 #3）：
  ItemVersion/ItemTemplate 契约冻结，不含 testlet_id/group_index 字段。
  testlet 标记由 item_group 表承载：item_group.testlet=true，
  item_version_ids 数组下标即 group_index。
"""
from __future__ import annotations

import inspect

import pytest

from src.core.content.item_group_assembler import (
    ItemGroupAssemblerError,
    assemble_item_group,
    blueprint_lineage,
)
from src.core.content.testlet_blueprint import (
    ItemSpec,
    TestletBlueprint,
    TestletBlueprintError,
    assemble_testlet,
    blueprint_to_group_index_map,
)
from src.core.models.item_group import ItemGroup


# ────────────────────────────────────────────────────────────────────
# 辅助构造
# ────────────────────────────────────────────────────────────────────


def _spec(
    spec_id: str,
    kp_codes: list[str] | None = None,
    *,
    interaction_type: str = "single_choice",
    scoring_method: str = "exact_match",
    allow_kp_overlap: bool = False,
) -> ItemSpec:
    if kp_codes is None:
        kp_codes = [f"kp.{spec_id}"]
    return ItemSpec(
        spec_id=spec_id,
        kp_codes=kp_codes,
        interaction_type=interaction_type,
        scoring_method=scoring_method,
        allow_kp_overlap=allow_kp_overlap,
    )


def _three_specs() -> list[ItemSpec]:
    return [
        _spec("q1", ["read.main_idea"]),
        _spec("q2", ["read.detail"]),
        _spec("q3", ["read.inference"]),
    ]


# ════════════════════════════════════════════════════════════════════
# 验收 #1：assemble_testlet 返回题组结构
# ════════════════════════════════════════════════════════════════════


class TestAssembleTestletBasic:
    """assemble_testlet 基本功能：返回含子题列表、题序、共享上下文."""

    def test_returns_testlet_blueprint(self):
        """返回 TestletBlueprint 结构."""
        bp = assemble_testlet("pass-001", _three_specs())
        assert isinstance(bp, TestletBlueprint)
        assert bp.passage_id == "pass-001"
        assert len(bp.item_specs) == 3

    def test_preserves_spec_order(self):
        """子题顺序保持传入顺序（题序）."""
        specs = _three_specs()
        bp = assemble_testlet("pass-002", specs)
        assert [s.spec_id for s in bp.item_specs] == ["q1", "q2", "q3"]

    def test_shared_context_defaults_to_passage_id(self):
        """未传 shared_context 时默认含 passage_id."""
        bp = assemble_testlet("pass-003", _three_specs())
        assert bp.shared_context["passage_id"] == "pass-003"

    def test_shared_context_custom(self):
        """自定义 shared_context 透传."""
        ctx = {"passage_id": "pass-004", "genre": "narrative"}
        bp = assemble_testlet("pass-004", _three_specs(), shared_context=ctx)
        assert bp.shared_context == ctx

    def test_ordered_flag(self):
        """ordered 标记透传."""
        bp = assemble_testlet("pass-005", _three_specs(), ordered=False)
        assert bp.ordered is False

    def test_empty_passage_id_rejected(self):
        """passage_id 为空时报错."""
        with pytest.raises(TestletBlueprintError, match="passage_id"):
            assemble_testlet("", _three_specs())


# ════════════════════════════════════════════════════════════════════
# 验收 #2：子题数 2-6 + 知识点重复检查
# ════════════════════════════════════════════════════════════════════


class TestAssembleTestletCountLimits:
    """子题数限制（R-Z-06 ≤6，最少 2）."""

    def test_single_item_rejected(self):
        """1 道子题 < 下限 2 → 报错."""
        with pytest.raises(TestletBlueprintError, match="少于下限"):
            assemble_testlet("pass-010", [_spec("q1")])

    def test_two_items_accepted(self):
        """2 道子题 = 下限 → 通过."""
        bp = assemble_testlet("pass-011", [_spec("q1"), _spec("q2")])
        assert len(bp.item_specs) == 2

    def test_six_items_accepted(self):
        """6 道子题 = 上限 → 通过."""
        specs = [_spec(f"q{i}") for i in range(6)]
        bp = assemble_testlet("pass-012", specs)
        assert len(bp.item_specs) == 6

    def test_seven_items_rejected(self):
        """7 道子题 > 上限 6 → 报错（R-Z-06）."""
        specs = [_spec(f"q{i}") for i in range(7)]
        with pytest.raises(TestletBlueprintError, match="超过上限"):
            assemble_testlet("pass-013", specs)


class TestAssembleTestletKpOverlap:
    """知识点重复检查."""

    def test_unique_kps_accepted(self):
        """子题间知识点不重复 → 通过."""
        bp = assemble_testlet("pass-020", _three_specs())
        assert bp.kp_overlap_notes == []

    def test_duplicate_kp_without_flag_rejected(self):
        """知识点重复但未标记 allow_kp_overlap → 报错."""
        specs = [
            _spec("q1", ["kp.a", "kp.b"]),
            _spec("q2", ["kp.b", "kp.c"]),  # kp.b 重复
        ]
        with pytest.raises(TestletBlueprintError, match="kp.b"):
            assemble_testlet("pass-021", specs)

    def test_duplicate_kp_with_flag_accepted(self):
        """知识点重复且全部标记 allow_kp_overlap → 通过 + 记录说明."""
        specs = [
            _spec("q1", ["kp.a", "kp.b"], allow_kp_overlap=True),
            _spec("q2", ["kp.b", "kp.c"], allow_kp_overlap=True),
        ]
        bp = assemble_testlet("pass-022", specs)
        assert len(bp.kp_overlap_notes) == 1
        assert "kp.b" in bp.kp_overlap_notes[0]

    def test_partial_flag_rejected(self):
        """重复知识点只在一部分子题标记 allow → 报错."""
        specs = [
            _spec("q1", ["kp.a", "kp.b"], allow_kp_overlap=True),
            _spec("q2", ["kp.b", "kp.c"], allow_kp_overlap=False),
        ]
        with pytest.raises(TestletBlueprintError, match="kp.b"):
            assemble_testlet("pass-023", specs)

    def test_empty_kp_codes_rejected(self):
        """子题 kp_codes 为空 → 报错."""
        specs = [_spec("q1", []), _spec("q2", ["kp.a"])]
        with pytest.raises(TestletBlueprintError, match="kp_codes 为空"):
            assemble_testlet("pass-024", specs)


class TestAssembleTestletSpecIdUnique:
    """spec_id 唯一性."""

    def test_duplicate_spec_id_rejected(self):
        """spec_id 重复 → 报错."""
        specs = [
            _spec("q1", ["kp.a"]),
            _spec("q1", ["kp.b"]),  # 重复 spec_id
        ]
        with pytest.raises(TestletBlueprintError, match="spec_id 重复"):
            assemble_testlet("pass-030", specs)


# ════════════════════════════════════════════════════════════════════
# 验收 #3（辅助）：blueprint_to_group_index_map
# ════════════════════════════════════════════════════════════════════


class TestGroupIndexMap:
    """group_index 映射（契约偏差：数组下标替代 ItemVersion.group_index）."""

    def test_group_index_map(self):
        """spec_id → group_index 映射正确（数组下标）."""
        bp = assemble_testlet("pass-040", _three_specs())
        idx_map = blueprint_to_group_index_map(bp)
        assert idx_map == {"q1": 0, "q2": 1, "q3": 2}

    def test_group_index_reflects_order(self):
        """group_index 反映 item_specs 顺序."""
        specs = [_spec("z"), _spec("a"), _spec("m")]
        bp = assemble_testlet("pass-041", specs)
        idx_map = blueprint_to_group_index_map(bp)
        assert idx_map == {"z": 0, "a": 1, "m": 2}


# ════════════════════════════════════════════════════════════════════
# 验收 #3：assemble_item_group（TestletBlueprint → ItemGroup ORM）
# ════════════════════════════════════════════════════════════════════


class TestAssembleItemGroup:
    """题组 ORM 装配：testlet 标记写入 item_group（契约偏差）."""

    def test_produces_item_group_orm(self):
        """装配产出 ItemGroup ORM 对象."""
        bp = assemble_testlet("pass-050", _three_specs())
        mapping = {"q1": "iv-1", "q2": "iv-2", "q3": "iv-3"}
        group = assemble_item_group(bp, mapping)
        assert isinstance(group, ItemGroup)

    def test_testlet_flag_true(self):
        """testlet=True（题组蓝图产出的均为 testlet 单元）."""
        bp = assemble_testlet("pass-051", _three_specs())
        group = assemble_item_group(bp, {"q1": "iv-1", "q2": "iv-2", "q3": "iv-3"})
        assert group.testlet is True

    def test_item_version_ids_order_matches_blueprint(self):
        """item_version_ids 顺序 = 蓝图 item_specs 顺序（group_index 下标）."""
        bp = assemble_testlet("pass-052", _three_specs())
        group = assemble_item_group(
            bp, {"q1": "iv-a", "q2": "iv-b", "q3": "iv-c"}
        )
        assert group.item_version_ids == ["iv-a", "iv-b", "iv-c"]

    def test_ordered_flag_from_blueprint(self):
        """ordered 从蓝图透传."""
        bp = assemble_testlet("pass-053", _three_specs(), ordered=False)
        group = assemble_item_group(bp, {"q1": "iv-1", "q2": "iv-2", "q3": "iv-3"})
        assert group.ordered is False

    def test_material_version_id_optional(self):
        """material_version_id 可为 None（passage-based testlet）."""
        bp = assemble_testlet("pass-054", _three_specs())
        group = assemble_item_group(bp, {"q1": "iv-1", "q2": "iv-2", "q3": "iv-3"})
        assert group.material_version_id is None

    def test_material_version_id_set(self):
        """material_version_id 可显式设置."""
        bp = assemble_testlet("pass-055", _three_specs())
        group = assemble_item_group(
            bp,
            {"q1": "iv-1", "q2": "iv-2", "q3": "iv-3"},
            material_version_id="mv-001",
        )
        assert group.material_version_id == "mv-001"

    def test_custom_item_group_id(self):
        """自定义 item_group_id."""
        bp = assemble_testlet("pass-056", _three_specs())
        group = assemble_item_group(
            bp,
            {"q1": "iv-1", "q2": "iv-2", "q3": "iv-3"},
            item_group_id="ig-custom-001",
        )
        assert group.item_group_id == "ig-custom-001"

    def test_auto_generated_id_has_prefix(self):
        """自动生成的 item_group_id 有 ig_ 前缀."""
        bp = assemble_testlet("pass-057", _three_specs())
        group = assemble_item_group(bp, {"q1": "iv-1", "q2": "iv-2", "q3": "iv-3"})
        assert group.item_group_id.startswith("ig_")

    def test_missing_spec_id_mapping_rejected(self):
        """spec_id 在映射中缺失 → 报错."""
        bp = assemble_testlet("pass-058", _three_specs())
        with pytest.raises(ItemGroupAssemblerError, match="缺失"):
            assemble_item_group(bp, {"q1": "iv-1", "q2": "iv-2"})  # 缺 q3

    def test_extra_mapping_rejected(self):
        """映射含蓝图外 spec_id → 报错."""
        bp = assemble_testlet("pass-059", _three_specs())
        with pytest.raises(ItemGroupAssemblerError, match="蓝图外"):
            assemble_item_group(
                bp,
                {"q1": "iv-1", "q2": "iv-2", "q3": "iv-3", "q4": "iv-4"},
            )

    def test_empty_version_id_rejected(self):
        """item_version_id 为空字符串 → 报错."""
        bp = assemble_testlet("pass-060", _three_specs())
        with pytest.raises(ItemGroupAssemblerError, match="为空"):
            assemble_item_group(
                bp, {"q1": "iv-1", "q2": "", "q3": "iv-3"}
            )


class TestBlueprintLineage:
    """blueprint_lineage：题组谱系快照."""

    def test_lineage_structure(self):
        """谱系结构含 group_index_map + item_version_ids."""
        bp = assemble_testlet("pass-070", _three_specs())
        mapping = {"q1": "iv-1", "q2": "iv-2", "q3": "iv-3"}
        lin = blueprint_lineage(bp, mapping, passage_id="pass-070")
        assert lin["passage_id"] == "pass-070"
        assert lin["testlet"] is True
        assert lin["ordered"] is True
        assert lin["group_index_map"] == {"q1": 0, "q2": 1, "q3": 2}
        assert lin["item_version_ids"] == ["iv-1", "iv-2", "iv-3"]


# ════════════════════════════════════════════════════════════════════
# 验收 #5：不 import 学科包/学段包
# ════════════════════════════════════════════════════════════════════


class TestNoSubjectPackImport:
    """testlet_blueprint + item_group_assembler 不 import 学科包/学段包（A5/X6）."""

    def test_no_subject_pack_import(self):
        """两个模块源码不含学科包/学段包 import."""
        from src.core.content import (
            item_group_assembler,
            testlet_blueprint,
        )

        forbidden = [
            "subject_packs",
            "grade_band_packs",
            "from src.packs",
            "import src.packs",
        ]
        for mod in (testlet_blueprint, item_group_assembler):
            source = inspect.getsource(mod)
            for token in forbidden:
                assert token not in source, (
                    f"{mod.__name__} 不得 import {token}"
                )
