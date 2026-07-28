"""T-W4-036 低段组卷 overlay 与会话约束（架构 v2 §5.3 / §4.8）.

核心域学段约束政策：
- L 段：题量 ≤10、会话时长 ≤15 分钟、形态=闯关（架构 §5.3 / §4.8）
- M 段：题量 ≤20、时长 ≤60 分钟、形态=常规
- H 段：题量 ≤30、时长 ≤60 分钟、形态=常规

`apply_gradeband_overlay` 把学段约束注入 paper_spec 并检测不可行冲突
（如请求 20 题低段卷 → 返回明确冲突原因）。

宪法 A5：本模块不 import 任何学科包/学段包；学段约束政策是核心域常量
（与 src/core/session/service.py::GRADEBAND_TIME_LIMIT_SEC 同源），
学段包 config.yaml 可通过 ``overlay`` 参数注入覆盖（核心不感知包文件位置，
仅按约定键 max_items / session_duration_max_min / session_form_game 消费）。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

# ════════════════════════════════════════════════════════════════════
# 核心域学段约束政策
# ════════════════════════════════════════════════════════════════════
# 时长上限与 session/service.GRADEBAND_TIME_LIMIT_SEC 同源（L=15min、M/H=60min）。
# 题量上限：L≤10（架构 §5.3 低段保护）；M/H 取平台默认上界。
GRADEBAND_CONSTRAINTS: dict[str, dict[str, Any]] = {
    "L": {"max_items": 10, "time_limit_min": 15, "session_form": "game"},
    "M": {"max_items": 20, "time_limit_min": 60, "session_form": "standard"},
    "H": {"max_items": 30, "time_limit_min": 60, "session_form": "standard"},
}
VALID_GRADEBANDS: frozenset[str] = frozenset(GRADEBAND_CONSTRAINTS.keys())


@dataclass(frozen=True)
class GradeBandOverlayResult:
    """apply_gradeband_overlay 返回结果.

    Attributes:
        paper_spec: 注入学段约束后的 paper_spec（含 time_limit_min /
            session_form / max_items / gradeband 字段）。
        feasible: 学段约束是否可行（题量/时长未超学段上限）。
        conflict: 不可行时的明确冲突原因（可行时为 None）。
        overlay_applied: 实际生效的学段约束 dict（审计用）。
    """

    paper_spec: dict[str, Any]
    feasible: bool
    conflict: Optional[str]
    overlay_applied: dict[str, Any]


class GradeBandConflictError(ValueError):
    """学段约束不可行（调用方显式要求 raise_on_conflict=True 时抛出）."""


def _resolve_constraints(
    grade_band: str, overlay: Optional[dict[str, Any]]
) -> dict[str, Any]:
    """合并核心默认约束与 pack 注入 overlay（pack 覆盖核心默认）.

    pack config.yaml 的字段名（max_items / session_duration_max_min /
    session_form_game）映射到核心约束键（max_items / time_limit_min /
    session_form）——核心不 import 学段包，只按约定键消费 overlay dict。
    """
    base = dict(GRADEBAND_CONSTRAINTS[grade_band])
    if overlay:
        if "max_items" in overlay:
            base["max_items"] = int(overlay["max_items"])
        if "session_duration_max_min" in overlay:
            base["time_limit_min"] = int(overlay["session_duration_max_min"])
        if "session_form_game" in overlay:
            base["session_form"] = "game" if overlay["session_form_game"] else "standard"
    return base


def _extract_item_count(paper_spec: dict[str, Any]) -> Optional[int]:
    """从 paper_spec 取题量（兼容多种声明形态）.

    兼容：item_count（int）/ items（list，取长度）/ item_count_range（取上界）。
    """
    if "item_count" in paper_spec:
        return int(paper_spec["item_count"])
    items = paper_spec.get("items")
    if isinstance(items, list):
        return len(items)
    rng = paper_spec.get("item_count_range")
    if isinstance(rng, (list, tuple)) and rng:
        return int(rng[-1])  # 上界作为题量上限校验依据
    return None


def build_gradeband_overlay(
    grade_band: str, *, overlay: Optional[dict[str, Any]] = None
) -> dict[str, Any]:
    """生成 compile_profile 用的 gradeband_overlay dict.

    返回的 dict 符合 src/core/assembly/profile.py::compile_profile 的
    gradeband_overlay 参数约定（item_count_range / time_limit_max_minutes /
    session_form），作为四维编译的学段维度注入。
    """
    if grade_band not in VALID_GRADEBANDS:
        raise ValueError(
            f"grade_band 必须 ∈ {sorted(VALID_GRADEBANDS)}，实际 {grade_band!r}"
        )
    c = _resolve_constraints(grade_band, overlay)
    return {
        "overlay_id": f"gradeband-{grade_band.lower()}",
        "overlay_version": "1.0.0",
        "item_count_range": [1, c["max_items"]],
        "time_limit_max_minutes": c["time_limit_min"],
        "session_form": c["session_form"],
    }


def apply_gradeband_overlay(
    paper_spec: dict[str, Any],
    grade_band: str,
    *,
    overlay: Optional[dict[str, Any]] = None,
    raise_on_conflict: bool = False,
) -> GradeBandOverlayResult:
    """注入学段约束到 paper_spec，并检测不可行冲突.

    Args:
        paper_spec: 组卷规格 dict，可含 item_count / items / item_count_range /
            time_limit_min / session_form。
        grade_band: 学段（L/M/H）。
        overlay: 可选 pack config dict（学段包 config.yaml 加载后注入），
            覆盖核心默认约束。核心不 import 学段包，由调用方加载注入。
        raise_on_conflict: True 时不可行抛 GradeBandConflictError；
            False（默认）时通过 result.conflict 返回。

    Returns:
        GradeBandOverlayResult（feasible / conflict / 注入后的 paper_spec）。

    Notes:
        L 段注入：max_items=10、time_limit_min=15、session_form="game"。
        不可行示例：请求 20 题低段卷 → conflict="L 段题量上限 10，请求 20 超出"。
    """
    if grade_band not in VALID_GRADEBANDS:
        raise ValueError(
            f"grade_band 必须 ∈ {sorted(VALID_GRADEBANDS)}，实际 {grade_band!r}"
        )

    constraints = _resolve_constraints(grade_band, overlay)
    overlaid = dict(paper_spec)
    conflicts: list[str] = []

    # 题量校验：请求题量超学段上限 → 不可行
    item_count = _extract_item_count(paper_spec)
    if item_count is not None and item_count > constraints["max_items"]:
        conflicts.append(
            f"{grade_band} 段题量上限 {constraints['max_items']}，"
            f"请求 {item_count} 超出"
        )

    # 时长校验：paper_spec 显式声明时长且超学段上限 → 不可行
    spec_time = paper_spec.get("time_limit_min")
    if spec_time is not None and int(spec_time) > constraints["time_limit_min"]:
        conflicts.append(
            f"{grade_band} 段时长上限 {constraints['time_limit_min']} 分钟，"
            f"请求 {spec_time} 超出"
        )

    # 注入学段约束（无论是否冲突都注入，便于调用方看到目标约束）
    overlaid["gradeband"] = grade_band
    overlaid["max_items"] = constraints["max_items"]
    overlaid["time_limit_min"] = constraints["time_limit_min"]
    overlaid["session_form"] = constraints["session_form"]

    conflict = "; ".join(conflicts) if conflicts else None
    if conflict and raise_on_conflict:
        raise GradeBandConflictError(conflict)

    return GradeBandOverlayResult(
        paper_spec=overlaid,
        feasible=not conflicts,
        conflict=conflict,
        overlay_applied=constraints,
    )


__all__ = [
    "GRADEBAND_CONSTRAINTS",
    "VALID_GRADEBANDS",
    "GradeBandOverlayResult",
    "GradeBandConflictError",
    "apply_gradeband_overlay",
    "build_gradeband_overlay",
]
