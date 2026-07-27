"""确定性实例化引擎实现（T-W2-004，失败即升级卡）.

核心流程见模块 docstring（__init__.py）。

设计要点：
  1. **学科无关**：本模块不引用任何学科包；interaction_id/scorer_id 由调用方传入
     （同一母题可挂多种交互类型——例如同一个数学母题可作为 single_choice 或
     numeric_blank 实例化）。
  2. **禁浮点漂移**：normalize_params 把 decimal/fraction 槽转为字符串规范化形式
     （Decimal('3.14')→'3.14'；Fraction(3,4)→'3/4'），仅用于 compute_instance_id
     与谱系留存；求值阶段用原生数值（Decimal/Fraction/int）传给 expr 求值器。
     求值器对 float 求值是确定性的（IEEE 754），但 id 计算不依赖浮点 str。
  3. **可复现性**：同一 (template_version, params, pack_digest, engine_digest,
     corpus_digests, locale) 必得同一 item_version_id（D3）。规范化参数序列化为
     canonical JSON 进入 sha256，键序固定、空白最紧。
  4. **纯计算**：不写 DB、不调 IO；返回 dict（含 item_version_id 与六大块）。
  5. **不签名**：本引擎只产出 draft 状态 ItemVersion dict；签发由校验门承载（§4 状态机）。

宪法 X6：本模块不 import 任何学科包/学段包。
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from decimal import Decimal
from fractions import Fraction
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field

from src.core.instantiation.distractor import (
    DistractorCollisionError,
    DistractorResult,
    generate_distractors,
)
from src.core.instantiation.dsl.schema import (
    DistractorRule,
    ItemTemplateSpec,
    PresentationBlock,
    Slot,
)
from src.core.instantiation.expr import ExpressionUnsafeError, evaluate
from src.core.models.content_addressing import compute_instance_id

# ────────────────────────────────────────────────────────────────────
# 引擎版本与摘要（进入公式一的 engine_digest）
# ────────────────────────────────────────────────────────────────────
ENGINE_VERSION: str = "1.0.0"
# engine_digest = sha256(ENGINE_VERSION)
# 为什么用版本字符串而非代码 hash：代码 hash 会随每次实现细节变化（如重构），
# 破坏已发布实例的可复现性；版本号语义化升级（破坏性变更必须升版本）。
ENGINE_DIGEST: str = "sha256:" + hashlib.sha256(ENGINE_VERSION.encode("utf-8")).hexdigest()

# ────────────────────────────────────────────────────────────────────
# 默认值与签名
# ────────────────────────────────────────────────────────────────────
_DEFAULT_PIPELINE_ID: str = "instantiation-engine"
_DEFAULT_SIGNED_BY: str = "instantiation-engine"
_DEFAULT_LOCALE: str = "zh-CN"


# ────────────────────────────────────────────────────────────────────
# 结果模型
# ────────────────────────────────────────────────────────────────────


class ItemVersionResult(BaseModel):
    """实例化结果（ItemVersion dict 的强类型表示）.

    与 item_version 表六大块对齐（契约 §2.2）。返回 dict 时调用 .model_dump()。
    """

    model_config = ConfigDict(extra="forbid")

    item_version_id: str = Field(..., description="公式一内容寻址哈希")
    item_id: str = Field(
        ..., description="不变身份，A/B 级 = item_version_id（自引用）"
    )
    status: str = Field(default="draft", description="实例化产物默认 draft")
    objective: dict[str, Any] = Field(..., description="知识标注集（来自母题）")
    interaction_ref: dict[str, Any] = Field(..., description="交互类型 + 参数")
    content: dict[str, Any] = Field(..., description="题面语义 AST + 素材引用")
    scoring_ref: dict[str, Any] = Field(..., description="评分器 + 参数")
    error_bindings: list[dict[str, Any]] = Field(
        ..., description="选项/评分维度 → 错误类型 + 置信规则"
    )
    lineage: dict[str, Any] = Field(..., description="生产谱系")


# ────────────────────────────────────────────────────────────────────
# 规范化参数（禁浮点漂移）
# ────────────────────────────────────────────────────────────────────


def _normalize_value(value: Any, slot_type: str, slot_name: str) -> Any:
    """按 slot.type 规范化单个值，返回 JSON 兼容表示.

    Args:
        value: 原始参数值。
        slot_type: 槽类型（int/decimal/fraction/string/bool/choice）。
        slot_name: 槽名（错误信息用）。

    Returns:
        JSON 兼容的规范化值：
          - int → int
          - decimal → str(Decimal(value))  # '3.14' 形式，避免 0.1+0.2 漂移
          - fraction → "numerator/denominator"  # '3/4'，与 Fraction 字面量一致
          - string → str
          - bool → bool
          - choice → str

    Raises:
        ValueError: 值无法规范化（如 int 槽传入非数字字符串）。
        TypeError: slot_type 不在允许列表（理论不可达，schema 已校验）。
    """
    if slot_type == "int":
        # int(value) 对 str "5" / float 5.0 / int 5 都生效
        try:
            return int(value)
        except (TypeError, ValueError) as e:
            raise ValueError(
                f"槽 {slot_name!r} (int) 规范化失败：{value!r} 无法转为 int"
            ) from e
    if slot_type == "decimal":
        # 用 Decimal(str(value)) 避免 float→Decimal 精度问题
        try:
            dec = Decimal(str(value))
        except (ArithmeticError, ValueError) as e:
            raise ValueError(
                f"槽 {slot_name!r} (decimal) 规范化失败：{value!r}"
            ) from e
        # 为什么用 format(dec.normalize(), 'f')：
        # - normalize() 去尾零（'3.10'→'3.1'）但可能产生科学计数法（'100'→'1E+2'）
        # - format(..., 'f') 强制定点表示，避免 E 记号进入 id 计算（D3 可复现）
        # - 同值不同字面量（'3.10' / '3.1' / 3.10）规范化后必相同
        return format(dec.normalize(), "f")
    if slot_type == "fraction":
        # Fraction(str(value)) 支持 "3/4" 与 "0.75"
        try:
            frac = Fraction(str(value))
        except (ArithmeticError, ValueError) as e:
            raise ValueError(
                f"槽 {slot_name!r} (fraction) 规范化失败：{value!r}"
            ) from e
        return f"{frac.numerator}/{frac.denominator}"
    if slot_type == "string":
        return str(value)
    if slot_type == "bool":
        return bool(value)
    if slot_type == "choice":
        return str(value)
    # schema 已限制 slot.type 在 ALLOWED_SLOT_TYPES 内
    raise ValueError(f"槽 {slot_name!r} 类型未知：{slot_type!r}")


def normalize_params(
    params: dict[str, Any], slots: dict[str, Slot]
) -> dict[str, Any]:
    """规范化参数字典，避免浮点漂移（契约 §3 公式一要求）.

    Args:
        params: 原始参数（槽名 → 值）。
        slots: 母题 spec.slots（槽名 → Slot 定义，含 type）。

    Returns:
        规范化参数字典：每个值按 slot.type 转为 JSON 兼容确定性表示。

    Raises:
        ValueError: 未知槽名、值无法规范化。

    Notes:
        - 未在 slots 中声明的 params 键被拒绝（防止隐式参数影响 id）。
        - slots 中声明但 params 中缺失的槽：本函数不报错，由调用方决定
          （默认值由母题定义承载，本函数只规范化已传值）。
    """
    if not isinstance(params, dict):
        raise ValueError(
            f"params 必须为 dict，实际为 {type(params).__name__}"
        )
    normalized: dict[str, Any] = {}
    for name, value in params.items():
        if name not in slots:
            raise ValueError(
                f"未知槽名 {name!r}（不在 spec.slots 声明中）"
            )
        slot = slots[name]
        normalized[name] = _normalize_value(value, slot.type, name)
    return normalized


# ────────────────────────────────────────────────────────────────────
# 求值 env：把规范化参数转为可求值的原生值
# ────────────────────────────────────────────────────────────────────


def _eval_env(
    params: dict[str, Any], slots: dict[str, Slot]
) -> dict[str, Any]:
    """构造求值器 env：把参数转为 Python 原生数值（确定性）.

    decimal → Decimal（求值器支持 Decimal 算术）
    fraction → Fraction（求值器支持 Fraction 算术）
    其余类型 → 原值（int/str/bool）

    为什么不直接用 normalized 字符串：求值器 env 需要可计算值；
    Decimal('3.14') 与 Fraction(3, 4) 都支持 +、-、*、/ 等算术且确定性。
    """
    env: dict[str, Any] = {}
    for name, value in params.items():
        if name not in slots:
            continue  # normalize_params 已校验，这里兜底
        stype = slots[name].type
        if stype == "decimal":
            env[name] = Decimal(str(value))
        elif stype == "fraction":
            # value 形如 "3/4"
            num, _, den = str(value).partition("/")
            env[name] = Fraction(int(num), int(den))
        else:
            env[name] = value
    return env


# ────────────────────────────────────────────────────────────────────
# presentation 插值
# ────────────────────────────────────────────────────────────────────


class _SafeFormatDict(dict):
    """format_map 缺失键时抛 KeyError 而非静默保留 {key}."""

    def __missing__(self, key: str) -> str:  # type: ignore[override]
        raise KeyError(f"presentation 模板引用了未提供的槽：{key!r}")


def _interpolate_block(
    block: PresentationBlock, params: dict[str, Any]
) -> dict[str, Any]:
    """对单个 presentation block 做插值，返回 content block dict.

    为什么用 str.format_map：它只支持 {key} 简单插值，不能执行表达式；
    花括号字面量需用 {{ }} 转义（与 Python format 一致）。
    """
    safe_params = _SafeFormatDict({k: str(v) for k, v in params.items()})
    try:
        rendered = block.template.format_map(safe_params)
    except KeyError as e:
        raise ValueError(
            f"presentation block (kind={block.kind!r}) 插值失败：{e}"
        ) from e
    except (IndexError, ValueError) as e:
        raise ValueError(
            f"presentation block (kind={block.kind!r}) 模板格式错误：{e}"
        ) from e
    return {
        "kind": block.kind,
        "template": block.template,
        "rendered": rendered,
    }


def _render_content(
    spec: ItemTemplateSpec, params: dict[str, Any]
) -> dict[str, Any]:
    """装配 content 块：presentation.blocks 全部插值.

    返回 {"blocks": [...]}（与契约 §2.2 content 结构对齐）。
    """
    blocks = [
        _interpolate_block(block, params) for block in spec.presentation.blocks
    ]
    return {"blocks": blocks}


# ────────────────────────────────────────────────────────────────────
# error_bindings 装配
# ────────────────────────────────────────────────────────────────────


def _build_error_bindings(
    spec: ItemTemplateSpec, eval_env: dict[str, Any], answer_value: Any
) -> list[dict[str, Any]]:
    """遍历 distractor_rules，生成 error_bindings 列表.

    每条 rule 产 1+ 个 option，每个 option 对应一个 error_binding：
      {option_value, label, error_type_id, collision, corpus_ref}

    碰撞策略：默认 allow_collision=False（确定性场景必须严格不碰撞）。
    corpus_sample 规则 value=None，allow_collision 由调用方在语料装配阶段决定；
    本引擎对 corpus_sample 也用 allow_collision=False（无正解碰撞问题，因为 value=None）。
    """
    bindings: list[dict[str, Any]] = []
    for rule in spec.distractor_rules.rules:
        result = _generate_one_distractor(rule, eval_env, answer_value)
        for opt in result.options:
            bindings.append(
                {
                    "option_value": opt.value,
                    "label": opt.label,
                    "error_type_id": opt.error_binding,
                    "collision": opt.collision,
                    "corpus_ref": opt.corpus_ref,
                }
            )
    return bindings


def _generate_one_distractor(
    rule: DistractorRule,
    eval_env: dict[str, Any],
    answer_value: Any,
) -> DistractorResult:
    """调用 distractor 生成器，把异常包装为 ValueError（保留 __cause__）."""
    try:
        return generate_distractors(
            rule,
            eval_env,
            answer_value=answer_value,
            allow_collision=False,
        )
    except DistractorCollisionError as e:
        raise ValueError(
            f"干扰项规则 (error_type_id={rule.error_type_id!r}) 与正解碰撞：{e}"
        ) from e
    except ExpressionUnsafeError as e:
        raise ValueError(
            f"干扰项表达式求值失败 (error_type_id={rule.error_type_id!r}): {e}"
        ) from e


# ────────────────────────────────────────────────────────────────────
# 主入口
# ────────────────────────────────────────────────────────────────────


def instantiate(
    template_version: dict[str, Any] | Any,
    params: dict[str, Any],
    *,
    pack_digest: str,
    interaction_id: str,
    scorer_id: str,
    scorer_params: dict[str, Any] | None = None,
    locale: str = _DEFAULT_LOCALE,
    corpus_digests: list[str] | None = None,
    seed: int = 0,
    engine_digest: str = ENGINE_DIGEST,
    signed_by: str = _DEFAULT_SIGNED_BY,
    signed_at: str | None = None,
) -> ItemVersionResult:
    """确定性实例化母题为 ItemVersion dict.

    Args:
        template_version: 母题版本（ItemTemplateVersionPydantic 或 dict）。
            必须含 template_version_id, template_id, dsl_version, spec 字段。
        params: 实例化参数（槽名 → 值）；值类型应与 spec.slots[*].type 兼容。
        pack_digest: 所属学科包摘要（sha256:...）。
        interaction_id: 交互类型 id（必须在 interaction.yaml 注册，调用方负责）。
        scorer_id: 评分器 id（必须在 scorer.yaml 注册，调用方负责）。
        scorer_params: 评分器参数（与评分器 params_schema 对齐）。默认 {}。
        locale: 语言/地区，默认 zh-CN。
        corpus_digests: 引用的语料库版本摘要链。默认 []。
        seed: 实例化随机种子（保留扩展点；当前确定性实例化不使用，仅记入 lineage）。
        engine_digest: 实例化引擎摘要（默认为本引擎版本摘要）。
        signed_by: 谱系签发人 id（默认 'instantiation-engine'）。
        signed_at: 谱系签发时间（ISO 8601 字符串）；None 时用当前 UTC 时间。

    Returns:
        ItemVersionResult：含 item_version_id（公式一）与六大块。

    Raises:
        ValueError: spec 校验失败、参数规范化失败、干扰项碰撞、表达式求值失败。
        pydantic.ValidationError: spec 结构不合规。

    验收对照：
        §1 返回完整 ItemVersion dict（objective/interaction_ref/content/
           scoring_ref/error_bindings/lineage） ✅
        §2 item_version_id 与公式一一致；同输入同 id ✅
        §3 黄金样例回归（见 tests/golden/instantiation/） ✅
        §5 不 import 学科包 ✅
    """
    # ── 1. 解析母题版本 ──
    if hasattr(template_version, "model_dump"):
        # Pydantic 实例
        tv_dict = template_version.model_dump()  # type: ignore[union-attr]
    elif isinstance(template_version, dict):
        tv_dict = template_version
    else:
        raise ValueError(
            f"template_version 必须为 dict 或 Pydantic 模型，实际为 "
            f"{type(template_version).__name__}"
        )

    template_version_id = tv_dict.get("template_version_id")
    template_id = tv_dict.get("template_id")
    dsl_version = tv_dict.get("dsl_version", "1")
    spec_dict = tv_dict.get("spec")
    if not template_version_id:
        raise ValueError("template_version 缺少 template_version_id 字段")
    if not template_id:
        raise ValueError("template_version 缺少 template_id 字段")
    if not isinstance(spec_dict, dict):
        raise ValueError("template_version.spec 必须为 dict")

    # ── 2. 解析 spec 为强类型（六大块校验） ──
    spec = ItemTemplateSpec.model_validate(spec_dict)

    # ── 3. 规范化参数（禁浮点漂移） ──
    normalized_params = normalize_params(params, spec.slots)

    # ── 4. 求正解（answer_program） ──
    eval_env = _eval_env(params, spec.slots)
    try:
        answer_value = evaluate(spec.answer_program.expression, env=eval_env)
    except ExpressionUnsafeError as e:
        raise ValueError(
            f"answer_program 求值失败：{e}"
        ) from e

    # ── 5. 生成干扰项 + error_bindings ──
    error_bindings = _build_error_bindings(spec, eval_env, answer_value)

    # ── 6. 装配 content（presentation 插值） ──
    content = _render_content(spec, normalized_params)

    # ── 7. 装配 interaction_ref / scoring_ref / objective / lineage ──
    interaction_ref = {
        "interaction_id": interaction_id,
        "interaction_params": {},  # 由交互类型 schema 决定，本引擎不绑定
    }
    scoring_ref = {
        "scorer_id": scorer_id,
        "scorer_params": scorer_params or {},
    }
    objective = spec.objective.model_dump()
    if signed_at is None:
        signed_at = datetime.now(timezone.utc).isoformat()
    corpus_refs = (
        [{"corpus_version_id": d, "digest": d} for d in corpus_digests]
        if corpus_digests
        else []
    )
    lineage = {
        "tier": "A",  # 规则模板实例化默认 A 级
        "pipeline": {"id": _DEFAULT_PIPELINE_ID, "version": ENGINE_VERSION},
        "template_version_id": template_version_id,
        "params": {"normalized": normalized_params},
        "seed": seed,
        "corpus_refs": corpus_refs,
        "ai_ledger_refs": [],  # A 级实例无 AI 起草
        "signed_by": signed_by,
        "signed_at": signed_at,
    }

    # ── 8. 计算公式一：item_version_id ──
    item_version_id = compute_instance_id(
        template_version_digest=template_version_id,
        normalized_params=normalized_params,
        pack_digest=pack_digest,
        engine_digest=engine_digest,
        corpus_digests=corpus_digests or [],
        locale=locale,
    )

    # ── 9. A/B 级 item_id = item_version_id（自引用，不变身份） ──
    item_id = item_version_id

    return ItemVersionResult(
        item_version_id=item_version_id,
        item_id=item_id,
        status="draft",
        objective=objective,
        interaction_ref=interaction_ref,
        content=content,
        scoring_ref=scoring_ref,
        error_bindings=error_bindings,
        lineage=lineage,
    )


__all__ = [
    "ENGINE_DIGEST",
    "ENGINE_VERSION",
    "ItemVersionResult",
    "instantiate",
    "normalize_params",
]
