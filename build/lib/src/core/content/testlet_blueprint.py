"""题组蓝图编排（T-W4-015）.

架构 v2 §4.1 C 线：同一语篇下编排 2–6 道子题，定义题序、考查知识点、
交互类型、评分方式；输出 testlet 标记的题组结构。题组整体过门，子题共享
语篇上下文。

契约偏差说明（验收 #3）：
  任务卡验收 #3 要求「testlet 标记正确写入 ItemVersion/ItemTemplate 的
  testlet_id 与 group_index 字段」。但 ItemVersion/ItemTemplate 的契约
  （§2.2/§2.3，迁移 0002）已冻结，不含 testlet_id/group_index 字段。
  波内契约冻结（只增不改）：不得向冻结表加列。
  替代方案：testlet 标记由 item_group 表承载——item_group.testlet=true
  标记 testlet 单元，item_group.item_version_ids 数组顺序即 group_index
  （数组下标 0..N-1 = group_index 0..N-1）。item_group.material_version_id
  指向语篇对应的 material_version（一材多题的"材"版本）。
  此偏差已记录在 PR 说明与 tasks/w4/T-W4-015.md 偏差注记中。

宪法 A5/X6：不 import 学科包/学段包。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar, Literal, Optional

from src.core.content.passage_schema import PromptDirection


# ────────────────────────────────────────────────────────────────────
# 数据结构
# ────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ItemSpec:
    """子题规格（题组蓝图中一道子题的定义）.

    Attributes:
        spec_id: 蓝图内子题 id（如 'q1'/'q2'，组内唯一）。
        kp_codes: 考查知识点编码列表（至少 1 个）。
        interaction_type: 交互类型（如 'single_choice'/'short_answer'）。
        scoring_method: 评分方式（如 'exact_match'/'rubric'）。
        stem_hint: 题干提示（可选，供 AI 起草或教研参考）。
        allow_kp_overlap: 是否允许与其他子题知识点重复（默认 False）。
    """

    spec_id: str
    kp_codes: list[str]
    interaction_type: str
    scoring_method: str
    stem_hint: Optional[str] = None
    allow_kp_overlap: bool = False


@dataclass(frozen=True)
class TestletBlueprint:
    """题组蓝图（一材多题编排结果）.

    Attributes:
        passage_id: 关联语篇 id（共享上下文）。
        item_specs: 有序子题规格列表（2–6 道）。
        ordered: 是否固定题序（True=固定；False=可乱序）。
        shared_context: 共享上下文标记（语篇正文引用）。
        kp_overlap_notes: 知识点重复说明（如有允许的重复）。
    """

    # pytest 不收集本类（名称以 Test 开头但非测试类）
    __test__: ClassVar[bool] = False

    passage_id: str
    item_specs: list[ItemSpec]
    ordered: bool = True
    shared_context: dict[str, str] = field(default_factory=dict)
    kp_overlap_notes: list[str] = field(default_factory=list)


# ────────────────────────────────────────────────────────────────────
# 蓝图编排
# ────────────────────────────────────────────────────────────────────

# 题组子题数限制（R-Z-06：≤6；最少 2 道才构成题组）
_MIN_ITEMS = 2
_MAX_ITEMS = 6


class TestletBlueprintError(ValueError):
    """题组蓝图编排错误（子题数越界 / 知识点重复 / 规格非法）."""

    __test__ = False  # pytest 不收集本类（名称以 Test 开头但非测试类）


def assemble_testlet(
    passage_id: str,
    item_specs: list[ItemSpec],
    *,
    ordered: bool = True,
    shared_context: Optional[dict[str, str]] = None,
) -> TestletBlueprint:
    """编排题组蓝图（任务卡 T-W4-015 验收 #1/#2）.

    Args:
        passage_id: 关联语篇 id。
        item_specs: 有序子题规格列表。
        ordered: 是否固定题序。
        shared_context: 共享上下文标记（如 {'passage_id': '...', 'genre': 'narrative'}）。

    Returns:
        TestletBlueprint：含子题列表、题序、共享上下文标记。

    Raises:
        TestletBlueprintError: 子题数越界（<2 或 >6）/ 知识点重复（未标记 allow_kp_overlap）/
            spec_id 重复 / 规格非法。

    Notes:
        - 子题数 2–6（R-Z-06 ≤6，DB CHECK 兜底）。
        - 子题间知识点默认不重复；如需重复须显式标记 allow_kp_overlap=True，
          并在 kp_overlap_notes 中说明重复意图。
    """
    if not passage_id:
        raise TestletBlueprintError("passage_id 不能为空")

    if len(item_specs) < _MIN_ITEMS:
        raise TestletBlueprintError(
            f"题组子题数 {len(item_specs)} 少于下限 {_MIN_ITEMS}"
        )
    if len(item_specs) > _MAX_ITEMS:
        raise TestletBlueprintError(
            f"题组子题数 {len(item_specs)} 超过上限 {_MAX_ITEMS}（R-Z-06）"
        )

    # spec_id 唯一性
    spec_ids = [s.spec_id for s in item_specs]
    seen_ids: set[str] = set()
    dup_ids: set[str] = set()
    for sid in spec_ids:
        if sid in seen_ids:
            dup_ids.add(sid)
        seen_ids.add(sid)
    if dup_ids:
        raise TestletBlueprintError(
            f"子题 spec_id 重复：{sorted(dup_ids)}"
        )

    # 知识点重复检查
    kp_to_specs: dict[str, list[str]] = {}
    for spec in item_specs:
        if not spec.kp_codes:
            raise TestletBlueprintError(
                f"子题 {spec.spec_id} 的 kp_codes 为空"
            )
        for kp in spec.kp_codes:
            kp_to_specs.setdefault(kp, []).append(spec.spec_id)

    overlap_notes: list[str] = []
    duplicated_kps = {
        kp: specs for kp, specs in kp_to_specs.items() if len(specs) > 1
    }
    for kp, specs in duplicated_kps.items():
        # 检查所有涉及该 KP 的子题是否都允许重复
        involved = [s for s in item_specs if s.spec_id in specs]
        all_allow = all(s.allow_kp_overlap for s in involved)
        if not all_allow:
            raise TestletBlueprintError(
                f"知识点 {kp!r} 在子题 {specs} 间重复，"
                "但未全部标记 allow_kp_overlap=True"
            )
        overlap_notes.append(
            f"知识点 {kp!r} 在子题 {specs} 间重复（已标记允许）"
        )

    ctx = shared_context or {"passage_id": passage_id}

    return TestletBlueprint(
        passage_id=passage_id,
        item_specs=list(item_specs),
        ordered=ordered,
        shared_context=ctx,
        kp_overlap_notes=overlap_notes,
    )


def blueprint_to_group_index_map(
    blueprint: TestletBlueprint,
) -> dict[str, int]:
    """返回 spec_id → group_index 映射（数组下标即 group_index）.

    契约偏差说明：group_index 不写入 ItemVersion（契约冻结无此列），
    而是由 item_group.item_version_ids 数组下标隐式表达。
    本函数供调用方（pipeline/组卷）在需要 group_index 时查询。
    """
    return {spec.spec_id: idx for idx, spec in enumerate(blueprint.item_specs)}


__all__ = [
    "ItemSpec",
    "TestletBlueprint",
    "TestletBlueprintError",
    "assemble_testlet",
    "blueprint_to_group_index_map",
]
