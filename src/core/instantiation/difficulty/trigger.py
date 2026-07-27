"""难度重估触发器实现（T-W2-006）.

检测逻辑：比较当前 params 与 baseline_params，
若任何 difficulty_relevant=True 的槽值发生变化 → 触发难度重估事件。

事件 schema：specs/contracts/events/difficulty_reestimate_event.md v1.0.0
事件传输：Redis 任务队列（W2 阶段仅落事件 + 验证 schema）

设计要点：
  1. **学科无关**：只读 spec.slots[*].difficulty_relevant 标志，不关心学科语义。
  2. **确定性**：同一 (template_version, params, baseline_params) 必得同一检测结果。
  3. **PII 安全**：事件不含学生信息（D7），仅含题目参数级数据。
  4. **分场景**：事件含 scene 字段（D5 禁止混估）。

宪法 X6：本模块不 import 任何学科包/学段包。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Literal, Optional
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.core.instantiation.dsl.schema import ItemTemplateSpec

# ────────────────────────────────────────────────────────────────────
# 事件模型（对齐 specs/contracts/events/difficulty_reestimate_event.md）
# ────────────────────────────────────────────────────────────────────

Scene = Literal["practice", "diagnosis", "measurement"]


class DifficultyReestimateEvent(BaseModel):
    """难度重估事件（对齐契约 v1.0.0）.

    验收对照：
        §2 命中时写入 difficulty_reestimate 事件 ✅
    """

    model_config = ConfigDict(extra="forbid")

    event_type: str = Field(
        default="difficulty_reestimate",
        description="事件类型，固定值",
    )
    event_id: str = Field(
        ..., description="事件唯一 id（ULID/UUID 字符串）"
    )
    item_version_id: str = Field(
        ..., description="触发重估的实例 id"
    )
    template_version_id: str = Field(
        ..., description="母题版本 id"
    )
    pack_digest: str = Field(
        ..., description="学科包摘要"
    )
    changed_slots: list[str] = Field(
        ..., description="发生变更的 difficulty_relevant 槽名列表"
    )
    params: dict[str, Any] = Field(
        ..., description="当前实例化参数"
    )
    baseline_params: Optional[dict[str, Any]] = Field(
        default=None, description="基准参数（变更对比基准）"
    )
    scene: Scene = Field(
        ..., description="场景（D5 禁止混估）"
    )
    created_at: str = Field(
        ..., description="事件时间戳（ISO 8601 UTC）"
    )

    @field_validator("event_type")
    @classmethod
    def _validate_event_type(cls, v: str) -> str:
        if v != "difficulty_reestimate":
            raise ValueError(
                f"event_type 必须为 'difficulty_reestimate'，实际为 {v!r}"
            )
        return v

    @field_validator("changed_slots")
    @classmethod
    def _validate_changed_slots(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("changed_slots 不得为空（空列表不应触发事件）")
        return v

    @field_validator("item_version_id", "template_version_id", "pack_digest")
    @classmethod
    def _validate_sha256_prefix(cls, v: str) -> str:
        if not v.startswith("sha256:"):
            raise ValueError(
                f"字段必须以 'sha256:' 开头，实际为 {v!r}"
            )
        return v


# ────────────────────────────────────────────────────────────────────
# 变更检测
# ────────────────────────────────────────────────────────────────────

def _get_spec(template_version: Any) -> ItemTemplateSpec:
    """从 template_version 提取并校验 spec."""
    if hasattr(template_version, "model_dump"):
        tv_dict = template_version.model_dump()  # type: ignore[union-attr]
    elif isinstance(template_version, dict):
        tv_dict = template_version
    else:
        raise ValueError(
            f"template_version 必须为 dict 或 Pydantic 模型，实际为 "
            f"{type(template_version).__name__}"
        )
    spec_dict = tv_dict.get("spec")
    if not isinstance(spec_dict, dict):
        raise ValueError("template_version.spec 必须为 dict")
    return ItemTemplateSpec.model_validate(spec_dict)


def detect_difficulty_change(
    template_version: dict[str, Any] | Any,
    params: dict[str, Any],
    *,
    baseline_params: dict[str, Any] | None = None,
) -> bool:
    """检测 params 是否变更了 difficulty_relevant 槽.

    比较 params 与 baseline_params 中 difficulty_relevant=True 的槽值：
    - 若任一 difficulty_relevant 槽值不同 → 返回 True（命中）
    - 若无 difficulty_relevant 槽、或所有 difficulty_relevant 槽值相同 → False
    - 若 baseline_params 为 None → False（无基准，无法检测变更）

    Args:
        template_version: 母题版本（dict 或 Pydantic 模型）。
        params: 当前实例化参数。
        baseline_params: 基准参数（变更对比基准）。None=无基准。

    Returns:
        True=命中 difficulty_relevant 槽变更；False=未命中。

    验收对照：
        §1 detect_difficulty_change 返回布尔值 ✅
    """
    if baseline_params is None:
        return False

    spec = _get_spec(template_version)

    # 遍历 difficulty_relevant 槽，检测值变更
    for slot_name, slot in spec.slots.items():
        if not slot.difficulty_relevant:
            continue
        # 仅检测两者都提供的槽（缺失视为未变更，避免误报）
        if slot_name not in params or slot_name not in baseline_params:
            continue
        if params[slot_name] != baseline_params[slot_name]:
            return True

    return False


def _get_changed_slots(
    spec: ItemTemplateSpec,
    params: dict[str, Any],
    baseline_params: dict[str, Any],
) -> list[str]:
    """返回所有发生变更的 difficulty_relevant 槽名列表."""
    changed = []
    for slot_name, slot in spec.slots.items():
        if not slot.difficulty_relevant:
            continue
        if slot_name not in params or slot_name not in baseline_params:
            continue
        if params[slot_name] != baseline_params[slot_name]:
            changed.append(slot_name)
    return changed


# ────────────────────────────────────────────────────────────────────
# 事件发布
# ────────────────────────────────────────────────────────────────────

def emit_difficulty_reestimate(
    template_version: dict[str, Any] | Any,
    params: dict[str, Any],
    *,
    item_version_id: str,
    pack_digest: str,
    scene: Scene,
    baseline_params: dict[str, Any] | None = None,
    redis_client: Any = None,
    queue_name: str = "difficulty_reestimate",
    event_id: str | None = None,
    created_at: str | None = None,
) -> DifficultyReestimateEvent:
    """发布难度重估事件到 Redis 队列.

    流程：
      1. 解析 spec 获取 difficulty_relevant 槽定义
      2. 比较 params 与 baseline_params，找出变更的 difficulty_relevant 槽
      3. 若无变更槽 → 抛 ValueError（调用方应先调 detect_difficulty_change）
      4. 构造 DifficultyReestimateEvent（schema 校验）
      5. 若提供 redis_client → RPUSH 到队列；否则仅返回事件（测试用）

    Args:
        template_version: 母题版本。
        params: 当前实例化参数。
        item_version_id: 触发重估的实例 id。
        pack_digest: 学科包摘要。
        scene: 场景（practice/diagnosis/measurement）。
        baseline_params: 基准参数。
        redis_client: Redis 客户端（None=不推队列，仅返回事件）。
        queue_name: Redis 队列名（默认 'difficulty_reestimate'）。
        event_id: 事件 id（None=自动生成 UUID）。
        created_at: 事件时间（None=当前 UTC）。

    Returns:
        DifficultyReestimateEvent 实例（已通过 schema 校验）。

    Raises:
        ValueError: 无变更槽、参数校验失败。

    验收对照：
        §2 命中时写入 difficulty_reestimate 事件 ✅
        §4 不 import 学科包 ✅
    """
    spec = _get_spec(template_version)

    if baseline_params is None:
        raise ValueError(
            "baseline_params 为 None，无法检测变更；"
            "请先调 detect_difficulty_change 确认有变更"
        )

    changed_slots = _get_changed_slots(spec, params, baseline_params)
    if not changed_slots:
        raise ValueError(
            "未检测到 difficulty_relevant 槽变更，不应发布事件"
        )

    # 提取 template_version_id
    if hasattr(template_version, "model_dump"):
        tv_dict = template_version.model_dump()  # type: ignore[union-attr]
    elif isinstance(template_version, dict):
        tv_dict = template_version
    else:
        raise ValueError("template_version 类型不支持的")

    template_version_id = tv_dict.get("template_version_id", "")

    event = DifficultyReestimateEvent(
        event_id=event_id or str(uuid4()),
        item_version_id=item_version_id,
        template_version_id=template_version_id,
        pack_digest=pack_digest,
        changed_slots=changed_slots,
        params=params,
        baseline_params=baseline_params,
        scene=scene,
        created_at=created_at or datetime.now(timezone.utc).isoformat(),
    )

    # 推入 Redis 队列（若提供客户端）
    if redis_client is not None:
        redis_client.rpush(queue_name, event.model_dump_json())

    return event


__all__ = [
    "DifficultyReestimateEvent",
    "detect_difficulty_change",
    "emit_difficulty_reestimate",
]
