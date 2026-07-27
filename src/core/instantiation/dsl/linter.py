"""母题 DSL Linter（T-W2-001）。

对母题 spec 做静态校验，输出结构化错误（验收 §2）。覆盖四类必检：
  1. 必填块缺失（六大块：objective/slots/variation_axes/presentation/
     answer_program/distractor_rules）
  2. slot 类型不在 ALLOWED_SLOT_TYPES
  3. variation_axis 引用不存在的 slot
  4. difficulty_relevant 不是 boolean

并叠加 Pydantic 全量结构校验（extra='forbid'、字段类型、必填项），
把 ValidationError 转成结构化错误，使 Linter 单次调用即可收集全部问题。

宪法 X6：本模块不 import 任何学科包/学段包。
"""
from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, ValidationError

from src.core.instantiation.dsl.schema import (
    ALLOWED_SLOT_TYPES,
    ItemTemplateSpec,
)


# 六大必填块（架构 v2 §4.1）
_REQUIRED_BLOCKS: tuple[str, ...] = (
    "objective",
    "slots",
    "variation_axes",
    "presentation",
    "answer_program",
    "distractor_rules",
)


class LintError(BaseModel):
    """单条 Lint 错误。

    code 为机器可判定的错误码（snake_case），便于上游分类处理；
    path 为出错的 JSON 路径（如 'slots.a.type'）；message 供人类阅读。
    """

    # 用 Pydantic 模型而非 dataclass：与 schema.py 一致，且自带校验
    # 为什么不 frozen：LintError 是收集产物，无需不可变保证
    code: str
    path: str = ""
    message: str


class LintResult(BaseModel):
    """Lint 结果：valid + errors 列表。

    valid=True 当且仅当 errors 为空。
    """

    valid: bool
    errors: list[LintError] = []

    @classmethod
    def ok(cls) -> "LintResult":
        """构造通过结果。"""
        return cls(valid=True, errors=[])

    @classmethod
    def fail(cls, errors: list[LintError]) -> "LintResult":
        """构造失败结果（valid=False，errors 非空）。"""
        return cls(valid=False, errors=errors)


def _pydantic_errors(err: ValidationError) -> list[LintError]:
    """把 Pydantic ValidationError 转成 LintError 列表。

    为什么单独转换而非直接抛：Linter 契约要求收集所有错误一次性返回，
    而非在第一个错误处抛出（验收 §2：输出结构化错误）。
    """
    out: list[LintError] = []
    for e in err.errors():
        # JSON 路径拼接（loc 元素为字段名或下标）
        path = ".".join(str(p) for p in e.get("loc", ()))
        # 错误码映射：Pydantic error "type" → 本模块 code
        pyd_type = e.get("type", "unknown")
        code_map = {
            "missing": "missing_field",
            "extra_forbidden": "extra_field_forbidden",
            "literal_error": "invalid_enum_value",
            "bool_parsing": "invalid_difficulty_relevant_type",
            "bool_type": "invalid_difficulty_relevant_type",
            "value_error": "invalid_value",
        }
        code = code_map.get(pyd_type, "schema_violation")
        msg = e.get("msg", "schema 校验失败")
        ctx = e.get("ctx", {})
        if ctx:
            msg = f"{msg} (ctx={ctx})"
        out.append(LintError(code=code, path=path, message=msg))
    return out


def _check_required_blocks(spec: Any) -> Optional[list[LintError]]:
    """检查六大块是否齐全。返回错误列表或 None（齐全）。

    为什么单独做：Pydantic 在缺块时会报 missing_field，但错误信息散在字段上；
    本检查在顶层明确指出"缺失块名"，便于教研定位。
    """
    if not isinstance(spec, dict):
        return None  # 非 dict 由 Pydantic 兜底
    missing = [b for b in _REQUIRED_BLOCKS if b not in spec]
    if not missing:
        return None
    return [
        LintError(
            code="missing_block",
            path=".",
            message=f"缺少必填块：{', '.join(missing)}",
        )
    ]


def _check_slot_types(spec: Any) -> Optional[list[LintError]]:
    """检查 slots 中每个槽的 type 是否在 ALLOWED_SLOT_TYPES 内。"""
    if not isinstance(spec, dict):
        return None
    slots = spec.get("slots")
    if not isinstance(slots, dict):
        return None  # 由 Pydantic 兜底
    errs: list[LintError] = []
    for slot_name, slot_def in slots.items():
        if not isinstance(slot_def, dict):
            continue
        stype = slot_def.get("type")
        if stype is None:
            continue  # 缺 type 由 Pydantic 兜底
        if stype not in ALLOWED_SLOT_TYPES:
            allowed = ", ".join(sorted(ALLOWED_SLOT_TYPES))
            errs.append(
                LintError(
                    code="invalid_slot_type",
                    path=f"slots.{slot_name}.type",
                    message=(
                        f"槽类型 {stype!r} 不在允许列表 "
                        f"({allowed})"
                    ),
                )
            )
    return errs or None


def _check_variation_axis_slots(spec: Any) -> Optional[list[LintError]]:
    """检查 variation_axes 中引用的槽名是否都存在于 slots 块。"""
    if not isinstance(spec, dict):
        return None
    slots = spec.get("slots")
    va = spec.get("variation_axes")
    if not isinstance(slots, dict) or not isinstance(va, dict):
        return None
    slot_names = set(slots.keys())
    axes = va.get("axes")
    if not isinstance(axes, list):
        return None
    errs: list[LintError] = []
    for i, axis in enumerate(axes):
        if not isinstance(axis, dict):
            continue
        axis_id = axis.get("axis_id", f"[{i}]")
        ref_slots = axis.get("slots")
        if not isinstance(ref_slots, list):
            continue
        for ref in ref_slots:
            if ref not in slot_names:
                errs.append(
                    LintError(
                        code="dangling_variation_slot",
                        path=f"variation_axes.axes[{i}].slots",
                        message=(
                            f"变式轴 {axis_id!r} 引用了不存在的槽 {ref!r}"
                        ),
                    )
                )
    return errs or None


def _check_difficulty_relevant(spec: Any) -> Optional[list[LintError]]:
    """检查每个槽的 difficulty_relevant 是否为布尔。

    为什么单独做：Pydantic 会把非布尔值报为 bool_parsing 错误，
    但这里要明确指向"哪个槽"并给出可读 message，且不阻断其他检查。
    """
    if not isinstance(spec, dict):
        return None
    slots = spec.get("slots")
    if not isinstance(slots, dict):
        return None
    errs: list[LintError] = []
    for slot_name, slot_def in slots.items():
        if not isinstance(slot_def, dict):
            continue
        if "difficulty_relevant" not in slot_def:
            continue  # 缺字段由 Pydantic 兜底
        val = slot_def["difficulty_relevant"]
        # 严格布尔：Python bool 是 int 子类，须先排除 int
        if isinstance(val, bool):
            continue
        errs.append(
            LintError(
                code="invalid_difficulty_relevant_type",
                path=f"slots.{slot_name}.difficulty_relevant",
                message=(
                    f"difficulty_relevant 必须为 boolean，"
                    f"实际为 {type(val).__name__}={val!r}"
                ),
            )
        )
    return errs or None


def lint(spec: Any) -> LintResult:
    """对母题 spec 做静态校验，返回结构化结果。

    Args:
        spec: 母题 spec dict（六大块）。也可传任意类型，非 dict 会报错。

    Returns:
        LintResult：valid=True 当且仅当无任何错误；errors 为全部问题列表。

    为什么先做命名检查再叠 Pydantic：
        命名检查覆盖验收 §2 的四类必检并给出明确 code/path；
        Pydantic 补全 extra/enum/类型等结构性问题。两类错误去重后合并。
    """
    errors: list[LintError] = []

    # ── 阶段 1：验收 §2 四类必检（手动收集，不短路） ──
    for checker in (
        _check_required_blocks,
        _check_slot_types,
        _check_variation_axis_slots,
        _check_difficulty_relevant,
    ):
        found = checker(spec)
        if found:
            errors.extend(found)

    # ── 阶段 2：Pydantic 全量结构校验（extra/enum/类型等） ──
    try:
        ItemTemplateSpec.model_validate(spec)
    except ValidationError as e:
        pyd_errs = _pydantic_errors(e)
        # 去重：与阶段 1 已报的 (code, path) 重叠则跳过
        seen = {(err.code, err.path) for err in errors}
        for pe in pyd_errs:
            key = (pe.code, pe.path)
            if key not in seen:
                errors.append(pe)

    return LintResult(valid=(len(errors) == 0), errors=errors)


__all__ = ["LintError", "LintResult", "lint"]
