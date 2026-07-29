"""题组 ORM 装配器（T-W4-015）.

将 TestletBlueprint（纯数据结构）+ spec_id→item_version_id 映射 转换为
ItemGroup ORM 行，供 c_line_pipeline（T-W4-016）与组卷消费。

契约偏差说明（验收 #3）：
  ItemVersion/ItemTemplate 契约冻结，不含 testlet_id/group_index 字段。
  testlet 标记由 item_group 表承载：
  - item_group.testlet=true 标记 testlet 单元
  - item_group.item_version_ids 数组下标即 group_index（0..N-1）
  - item_group.ordered 控制是否固定题序
  - item_group.material_version_id 指向语篇对应的素材版本（passage-based
    testlet 可为 None，语篇关联通过 item_version.lineage 追溯）

为什么本模块只产 ORM 对象不落库：装配是纯内存转换，落库由 writer.py 的
publish_item_group 统一走写入服务（铁律 2：禁止绕过写入服务直写）。
保持装配与落库分离便于单测（不触 DB）与复用（pipeline/组卷共用）。

宪法 A5/X6：不 import 学科包/学段包。
"""
from __future__ import annotations

import ulid
from typing import Optional

from src.core.models.item_group import ItemGroup
from src.core.content.testlet_blueprint import TestletBlueprint


# ────────────────────────────────────────────────────────────────────
# 异常
# ────────────────────────────────────────────────────────────────────


class ItemGroupAssemblerError(ValueError):
    """题组装配错误（spec_id 缺映射 / 数量不匹配 / 蓝图未编排）."""


# ────────────────────────────────────────────────────────────────────
# 装配主入口
# ────────────────────────────────────────────────────────────────────


def assemble_item_group(
    blueprint: TestletBlueprint,
    spec_id_to_version_id: dict[str, str],
    *,
    material_version_id: Optional[str] = None,
    item_group_id: Optional[str] = None,
) -> ItemGroup:
    """将题组蓝图 + spec_id→version_id 映射 转换为 ItemGroup ORM（任务卡 T-W4-015 验收 #3）.

    装配规则：
    1. 按 blueprint.item_specs 顺序，将每个 spec_id 映射到 item_version_id。
    2. 所有 spec_id 必须在 spec_id_to_version_id 中有对应项，否则报错。
    3. item_version_ids 数组顺序 = blueprint.item_specs 顺序（即 group_index 下标）。
    4. testlet=True（题组蓝图产出的均为 testlet 单元）。
    5. ordered 取 blueprint.ordered。
    6. material_version_id 由调用方提供（passage-based testlet 可为 None）。

    为什么不在本函数内落库：装配是纯内存转换；落库走 writer.publish_item_group
    （铁律 2：写入服务统一路径）。分离便于单测（不触 DB）与调用方控制事务边界。

    Args:
        blueprint: 已编排的题组蓝图（含 2-6 子题规格与题序）。
        spec_id_to_version_id: spec_id → item_version_id 映射（由 pipeline 创建
            item_versions 后提供；每个 spec_id 必须有对应 item_version_id）。
        material_version_id: 关联素材版本 id（可选；passage-based testlet 可为 None，
            语篇关联通过 item_version.lineage 追溯）。
        item_group_id: 自定义题组 id（None 时自动生成 ULID）。

    Returns:
        ItemGroup ORM 对象（testlet=True，未落库；调用方负责 db.add + commit）。

    Raises:
        ItemGroupAssemblerError: spec_id 缺映射 / 数量不匹配 / 映射含多余条目。
    """
    # 1. 校验映射完整性：每个 spec_id 必须有对应 item_version_id
    blueprint_spec_ids = [s.spec_id for s in blueprint.item_specs]
    missing_specs = [
        sid for sid in blueprint_spec_ids if sid not in spec_id_to_version_id
    ]
    if missing_specs:
        raise ItemGroupAssemblerError(
            f"以下 spec_id 在 spec_id_to_version_id 中缺失：{missing_specs}"
        )

    # 2. 校验无多余映射（映射中的 spec_id 不在蓝图内 → 调用方逻辑错误）
    extra_specs = [
        sid for sid in spec_id_to_version_id if sid not in set(blueprint_spec_ids)
    ]
    if extra_specs:
        raise ItemGroupAssemblerError(
            f"spec_id_to_version_id 含蓝图外 spec_id：{extra_specs}"
        )

    # 3. 按蓝图顺序构建 item_version_ids（数组下标即 group_index）
    item_version_ids = [
        spec_id_to_version_id[sid] for sid in blueprint_spec_ids
    ]

    # 4. 校验 item_version_id 非空
    empty_versions = [
        sid for sid in blueprint_spec_ids
        if not spec_id_to_version_id.get(sid)
    ]
    if empty_versions:
        raise ItemGroupAssemblerError(
            f"以下 spec_id 的 item_version_id 为空：{empty_versions}"
        )

    # 5. 构造 ItemGroup ORM（testlet=True，契约偏差：testlet 标记由本表承载）
    group_id = item_group_id or ("ig_" + str(ulid.new()))
    return ItemGroup(
        item_group_id=group_id,
        material_version_id=material_version_id,
        item_version_ids=item_version_ids,
        ordered=blueprint.ordered,
        testlet=True,  # 题组蓝图产出的均为 testlet 单元
    )


def blueprint_lineage(
    blueprint: TestletBlueprint,
    spec_id_to_version_id: dict[str, str],
    *,
    passage_id: Optional[str] = None,
) -> dict:
    """构建题组谱系快照（供 pipeline 落 item_version.lineage 或审计留档）.

    谱系结构：
    - passage_id：关联语篇 id（如有）。
    - testlet：固定 True（题组蓝图产出）。
    - ordered：是否固定题序。
    - group_index_map：spec_id → group_index（数组下标，契约偏差替代字段）。
    - item_version_ids：有序 item_version_id 列表（与 group_index 对齐）。

    为什么单独提供谱系函数：item_version.lineage 是 JSONB，testlet 信息需嵌入
    其中以便追溯（ItemVersion 无 testlet_id/group_index 列，契约偏差）。
    """
    group_index_map = {
        spec.spec_id: idx
        for idx, spec in enumerate(blueprint.item_specs)
    }
    return {
        "passage_id": passage_id,
        "testlet": True,
        "ordered": blueprint.ordered,
        "group_index_map": group_index_map,
        "item_version_ids": [
            spec_id_to_version_id.get(spec.spec_id)
            for spec in blueprint.item_specs
        ],
    }


__all__ = [
    "ItemGroupAssemblerError",
    "assemble_item_group",
    "blueprint_lineage",
]
