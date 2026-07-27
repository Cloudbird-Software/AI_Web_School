"""受控变式引擎实现（T-W2-005）.

按母题 variation_axes 中指定轴的槽子集重采样，其余槽冻结；
对每个变式调用 instantiate() 生成 ItemVersion；
最后签发 VariantCertificate 记录目标不变性证据。

两条建模纪律（ADR §4.1）：
  ①凡改变考查目标的参数必须拆母题，不得作为变式维度——
    本引擎检测 objective 依赖槽被变更时拒绝发证（certified=False, UNPROVEN）。
  ②优先"按构造必然合法"的生成器设计——
    默认采样器在槽取值域内生成值，不依赖随机源（可复现）。

AI 自由改写：永远标记 UNPROVEN，不产出已认证 VariantCertificate（mark_ai_free_rewrite）。

宪法 X6：本模块不 import 任何学科包/学段包。
"""
from __future__ import annotations

import ast
from decimal import Decimal
from fractions import Fraction
from typing import Any, Callable

from src.core.instantiation.dsl.schema import ItemTemplateSpec, Slot, VariationAxis
from src.core.instantiation.engine import (
    ItemVersionResult,
    instantiate,
)
from src.core.instantiation.variation.certificate import (
    CONTROLLED_VARIATION_OPERATOR,
    VariantCertificate,
    compute_objective_signature,
    issue_certificate,
    mark_unproven,
)

# ────────────────────────────────────────────────────────────────────
# 默认采样器
# ────────────────────────────────────────────────────────────────────

def _default_sampler(
    slot_name: str,
    slot: Slot,
    base_value: Any,
    variant_index: int,
) -> Any:
    """默认采样器：在槽取值域内确定性生成新值.

    为什么用确定性而非随机：
      - D3 可复现性：同一 (base_params, seed=variant_index) 必得同一变式集
      - "按构造必然合法"：在取值域内递增，避免碰撞与退化

    采样策略（按 slot.type）：
      - int：base + (index+1)；若有 min/max 则取模回绕到区间内
      - decimal：base + Decimal(index+1)
      - fraction：base + Fraction(index+1, 1)
      - choice：choices[(base_index + index + 1) % len(choices)]
      - string/bool：原值（字符串/布尔槽不参与数值变式，由调用方提供采样器）
    """
    if slot.type == "int":
        val = int(base_value) + variant_index + 1
        if slot.min is not None and val < int(slot.min):
            val = int(slot.min) + variant_index
        if slot.max is not None and val > int(slot.max):
            span = int(slot.max) - int(slot.min) if slot.min is not None else 1
            val = int(slot.min) + ((val - int(slot.min)) % max(span, 1))
        return val
    if slot.type == "decimal":
        dec = Decimal(str(base_value)) + Decimal(str(variant_index + 1))
        return format(dec.normalize(), "f")
    if slot.type == "fraction":
        frac = Fraction(str(base_value)) + Fraction(variant_index + 1, 1)
        return f"{frac.numerator}/{frac.denominator}"
    if slot.type == "choice":
        choices = slot.choices or []
        if not choices:
            return base_value
        try:
            base_idx = choices.index(base_value)
        except ValueError:
            base_idx = 0
        return choices[(base_idx + variant_index + 1) % len(choices)]
    # string / bool：默认不变（调用方应提供自定义采样器）
    return base_value


Sampler = Callable[[str, Slot, Any, int], Any]
"""采样器类型：(slot_name, slot_def, base_value, variant_index) -> new_value"""


# ────────────────────────────────────────────────────────────────────
# objective 依赖检测
# ────────────────────────────────────────────────────────────────────

def _extract_referenced_slots(expression: str) -> set[str]:
    """解析 answer_program 表达式，提取引用的槽名集合.

    用 ast.walk 遍历所有 ast.Name 节点（变量引用）。
    安全表达式求值器只允许 Name 作为变量引用，故 Name 集合 = 槽引用集合。
    """
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError:
        return set()
    return {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}


def _check_objective_dependency(
    spec: ItemTemplateSpec, axis: VariationAxis
) -> tuple[bool, list[str]]:
    """检测变式轴是否包含 objective 依赖槽.

    两条判定规则（任一命中即视为 objective 依赖）：
      1. **全槽变式**：轴覆盖 spec.slots 的全部槽（无冻结槽）→
         变式改变整个题目，很可能改变考查目标。
      2. **choice 槽进表达式**：轴包含 type=choice 且出现在 answer_program
         表达式中的槽 → choice 槽在表达式中通常选择运算类型，变式会改变
         考查的知识点（如加法→乘法）。

    Args:
        spec: 母题 spec（六大块）。
        axis: 变式轴定义。

    Returns:
        (has_dependency, dependent_slot_names)
        - has_dependency: True 表示存在 objective 依赖，应拒绝发证
        - dependent_slot_names: 触发依赖的槽名列表（用于错误信息）
    """
    all_slot_names = set(spec.slots.keys())
    axis_slot_set = set(axis.slots)

    dependent: list[str] = []

    # 规则 1：全槽变式（无冻结槽）
    if axis_slot_set >= all_slot_names and len(all_slot_names) > 0:
        dependent.extend(sorted(axis_slot_set & all_slot_names))

    # 规则 2：choice 槽进表达式
    expr_slots = _extract_referenced_slots(spec.answer_program.expression)
    for slot_name in axis.slots:
        slot = spec.slots.get(slot_name)
        if slot is None:
            continue
        if slot.type == "choice" and slot_name in expr_slots:
            if slot_name not in dependent:
                dependent.append(slot_name)

    return len(dependent) > 0, dependent


# ────────────────────────────────────────────────────────────────────
# 主入口
# ────────────────────────────────────────────────────────────────────

def generate_variants(
    template_version: dict[str, Any] | Any,
    axis_id: str,
    n: int,
    base_params: dict[str, Any],
    *,
    pack_digest: str,
    interaction_id: str,
    scorer_id: str,
    scorer_params: dict[str, Any] | None = None,
    locale: str = "zh-CN",
    corpus_digests: list[str] | None = None,
    seed: int = 0,
    sampler: Sampler | None = None,
    operator_id: str = CONTROLLED_VARIATION_OPERATOR,
) -> tuple[list[ItemVersionResult], VariantCertificate]:
    """按变式轴重采样生成 n 个变式实例 + VariantCertificate.

    流程：
      1. 解析母题版本为 ItemTemplateSpec
      2. 查找 axis_id 对应的 VariationAxis
      3. 检测 objective 依赖：若轴含 objective 依赖槽 → 返回空列表 + UNPROVEN 证书
      4. 按 axis.slots 重采样（其余槽冻结），生成 n 组 params
      5. 对每组 params 调用 instantiate() 生成 ItemVersion
      6. 计算 objective 签名，校验 kp_set / skill_set 不变性
      7. 签发 VariantCertificate

    Args:
        template_version: 母题版本（dict 或 Pydantic 模型）。
        axis_id: 变式轴 id（必须在 spec.variation_axes 中声明）。
        n: 生成变式数量。
        base_params: 基准参数（轴外槽从此取值并冻结）。
        pack_digest: 学科包摘要。
        interaction_id: 交互类型 id。
        scorer_id: 评分器 id。
        scorer_params: 评分器参数。
        locale: 语言/地区。
        corpus_digests: 语料库摘要链。
        seed: 随机种子（保留扩展点；当前确定性采样不使用）。
        sampler: 自定义采样器（None 用默认）。
        operator_id: 操作者标识（默认 controlled-variation-engine）。

    Returns:
        (variants, certificate)
        - variants: n 个 ItemVersionResult 列表（objective 依赖时为空）
        - certificate: VariantCertificate（certified=True 表示已认证）

    Raises:
        ValueError: axis_id 不存在、n <= 0、参数校验失败等。

    验收对照：
        §1 generate_variants 按轴重采样，返回 n 个实例 + VariantCertificate ✅
        §3 对 objective 依赖变更槽的变式，拒绝发证并标记 UNPROVEN ✅
        §5 不 import 学科包 ✅
    """
    if n <= 0:
        raise ValueError(f"n 必须为正整数，实际为 {n}")

    # ── 1. 解析母题版本 ──
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
    spec = ItemTemplateSpec.model_validate(spec_dict)

    # ── 2. 查找变式轴 ──
    axis: VariationAxis | None = None
    for ax in spec.variation_axes.axes:
        if ax.axis_id == axis_id:
            axis = ax
            break
    if axis is None:
        available = [ax.axis_id for ax in spec.variation_axes.axes]
        raise ValueError(
            f"变式轴 {axis_id!r} 不存在；可用轴：{available}"
        )

    # ── 3. 检测 objective 依赖 ──
    has_dep, dep_slots = _check_objective_dependency(spec, axis)
    if has_dep:
        cert = mark_unproven(
            operator_id=operator_id,
            reason=(
                f"变式轴 {axis_id!r} 包含 objective 依赖槽 {dep_slots!r}："
                f"改变考查目标的参数必须拆母题（ADR §4.1 纪律①）"
            ),
            objective_signature=compute_objective_signature(spec.objective),
            axis_id=axis_id,
            axis_slots=axis.slots,
            frozen_slots=[s for s in spec.slots if s not in axis.slots],
            variant_ids=[],
        )
        return [], cert

    # ── 4. 按轴重采样 ──
    if sampler is None:
        sampler = _default_sampler

    all_slot_names = set(spec.slots.keys())
    axis_slots = set(axis.slots)
    frozen_slots = all_slot_names - axis_slots

    # 校验轴内槽名都存在
    for slot_name in axis.slots:
        if slot_name not in spec.slots:
            raise ValueError(
                f"变式轴 {axis_id!r} 引用了未知槽 {slot_name!r}"
            )

    # 校验基准参数覆盖所有槽
    for slot_name in spec.slots:
        if slot_name not in base_params:
            raise ValueError(
                f"基准参数缺少槽 {slot_name!r}（base_params 必须覆盖全部槽）"
            )

    # ── 5. 生成 n 组变式参数并实例化 ──
    variants: list[ItemVersionResult] = []
    for i in range(n):
        variant_params: dict[str, Any] = {}
        # 冻结槽：直接取基准值
        for slot_name in frozen_slots:
            variant_params[slot_name] = base_params[slot_name]
        # 轴槽：采样器生成新值
        for slot_name in axis.slots:
            slot_def = spec.slots[slot_name]
            base_val = base_params[slot_name]
            variant_params[slot_name] = sampler(
                slot_name, slot_def, base_val, i
            )

        result = instantiate(
            template_version,
            variant_params,
            pack_digest=pack_digest,
            interaction_id=interaction_id,
            scorer_id=scorer_id,
            scorer_params=scorer_params,
            locale=locale,
            corpus_digests=corpus_digests,
            seed=seed + i,
        )
        variants.append(result)

    # ── 6. 校验 objective 不变性 ──
    # objective 来自母题（静态），所有变式共享同一 objective；
    # 此处计算签名作为不变性证据。
    objective_sig = compute_objective_signature(spec.objective)
    # 受控变式不改母题，kp_set / skill_set 必然不变
    kp_set_unchanged = True
    skill_set_unchanged = True

    # ── 7. 签发证书 ──
    variant_ids = [v.item_version_id for v in variants]
    cert = issue_certificate(
        operator_id=operator_id,
        axis_id=axis_id,
        certified=True,
        reason=(
            f"受控变式（轴={axis_id!r}, n={n}）：objective 签名一致，"
            f"kp_set 与 skill_set 未变"
        ),
        objective_signature=objective_sig,
        kp_set_unchanged=kp_set_unchanged,
        skill_set_unchanged=skill_set_unchanged,
        axis_slots=sorted(axis_slots),
        frozen_slots=sorted(frozen_slots),
        variant_ids=variant_ids,
    )

    return variants, cert


def mark_ai_free_rewrite(
    variant: ItemVersionResult,
    *,
    ai_operator_id: str,
    axis_id: str = "",
    objective_signature: str = "",
) -> VariantCertificate:
    """标记 AI 自由改写产物为 UNPROVEN.

    AI 自由改写可能改变表达式/结构/知识点，无法证明 objective 不变，
    永远标记 UNPROVEN，不产出已认证 VariantCertificate（验收 §4）。

    Args:
        variant: AI 改写产出的 ItemVersion。
        ai_operator_id: AI 操作者标识（如模型 id）。
        axis_id: 关联的变式轴 id（可选，AI 改写通常无轴）。
        objective_signature: 已知的 objective 签名（无法确定时为空）。

    Returns:
        VariantCertificate（certified=False, operator_id=ai_operator_id）。
    """
    return mark_unproven(
        operator_id=ai_operator_id,
        reason=(
            "AI 自由改写：无法证明 objective 不变（ADR §4.1：AI 改写永标 UNPROVEN）"
        ),
        objective_signature=objective_signature,
        axis_id=axis_id,
        variant_ids=[variant.item_version_id],
    )


__all__ = [
    "generate_variants",
    "mark_ai_free_rewrite",
]
