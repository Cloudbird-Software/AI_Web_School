"""干扰项生成器实现（T-W2-003）。

设计要点：
  1. 接口 generate_distractors(rule, slot_values, *, answer_value, allow_collision)
     返回 {options: [DistractorOption, ...]}。每条 rule 绑定一个 error_type_id，
     故 options 内的所有 option 共享同一 error_binding（验收 §1）。
  2. deterministic：用安全表达式求值器（T-W2-002 evaluate）求值 rule.expression，
     env=slot_values。表达式若返回 list/tuple，则展开为多个 option；标量返回单 option。
     expression 缺失或求值抛错时拒绝（不静默吞错）。
  3. corpus_sample：返回占位 option（value=None, label=rule.label or corpus_ref），
     等待 B 线语料库（T-W2-017）装配真实值。corpus_ref 缺失拒绝。
  4. 碰撞检查（验收 §3）：若 option.value == answer_value，则
     - allow_collision=False → 抛 DistractorCollisionError；
     - allow_collision=True → 仍加入 options，但 collision 字段置 True（B 线容差）。
  5. 学科无关：本模块只依赖 expr 求值器与 DSL schema，不引用任何学科包。

宪法 X6：本模块不 import 任何学科包/学段包。
"""
from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field

from src.core.instantiation.dsl.schema import DistractorRule
from src.core.instantiation.expr import (
    ExpressionUnsafeError,
    evaluate,
)


class DistractorCollisionError(Exception):
    """干扰项值与正解值碰撞（验收 §3）。

    默认不允许（生成器拒绝）；抽样场景 allow_collision=True 时改为标记而不抛。
    """


class DistractorOption(BaseModel):
    """单个干扰项选项。

    value：选项取值（deterministic 表达式求值结果，或 corpus_sample 的 None 占位）。
    label：可读标签（可选，由 rule.label 或求值结果字符串化得来）。
    error_binding：错误类型 id（来自 rule.error_type_id，选项→错误类型确定映射，
        架构 v2 §4.5「选某项是证据非因果」）。
    collision：是否与正解碰撞（抽样容差场景标记用）。
    corpus_ref：corpus_sample 规则的语料库引用（仅该类型有效，便于 B 线回填）。
    """

    model_config = ConfigDict(extra="forbid")

    value: Any = Field(..., description="选项值（deterministic 求值结果；corpus_sample 为 None）")
    label: Optional[str] = Field(default=None, description="可读标签")
    error_binding: str = Field(..., description="错误类型 id（来自 rule.error_type_id）")
    collision: bool = Field(default=False, description="是否与正解碰撞（容差场景标记）")
    corpus_ref: Optional[str] = Field(
        default=None,
        description="corpus_sample 规则的语料库引用（B 线回填用）",
    )


class DistractorResult(BaseModel):
    """干扰项生成结果。

    options 列表长度通常为 1（单条 rule 一个干扰项）；deterministic 表达式
    返回 list/tuple 时可展开为多个。空列表不允许（无干扰项产出视为生成失败）。
    """

    model_config = ConfigDict(extra="forbid")

    options: list[DistractorOption] = Field(
        ..., min_length=1, description="干扰项选项列表（至少 1 项）"
    )


def _make_option(
    value: Any,
    *,
    error_binding: str,
    label: Optional[str],
    answer_value: Any,
    allow_collision: bool,
    corpus_ref: Optional[str] = None,
) -> DistractorOption:
    """构造单个选项，做碰撞检查。

    Args:
        value: 干扰项取值。
        error_binding: 错误类型 id。
        label: 可读标签（可选）。
        answer_value: 正解值（None 表示不做碰撞检查）。
        allow_collision: 抽样容差；True 时碰撞改为标记，False 时抛错。
        corpus_ref: corpus_sample 引用（仅该类型填）。

    Returns:
        DistractorOption 实例。

    Raises:
        DistractorCollisionError: 碰撞且 allow_collision=False。
    """
    collision = False
    if answer_value is not None and _values_equal(value, answer_value):
        if not allow_collision:
            raise DistractorCollisionError(
                f"干扰项值 {value!r} 与正解值 {answer_value!r} 碰撞"
            )
        collision = True
    return DistractorOption(
        value=value,
        label=label,
        error_binding=error_binding,
        collision=collision,
        corpus_ref=corpus_ref,
    )


def _values_equal(a: Any, b: Any) -> bool:
    """宽松相等：处理数值类型跨表示（int vs float 同值相等）。

    为什么不直接用 ==：Python 中 1 == 1.0 为 True 已满足；
    但 1 == "1" 为 False（不同类型），符合本系统"避免字符串冒充数值"纪律。
    本函数仅做 == 兜底，未来若需分数/小数跨表示等价，再在此扩展。
    """
    try:
        return a == b
    except Exception:
        return False


def _generate_deterministic(
    rule: DistractorRule,
    slot_values: dict[str, Any],
    *,
    answer_value: Any,
    allow_collision: bool,
) -> list[DistractorOption]:
    """deterministic 规则：用安全求值器求 expression。"""
    if not rule.expression:
        raise ValueError(
            "deterministic 规则缺少 expression（rule.expression 为空）"
        )
    try:
        result = evaluate(rule.expression, env=slot_values)
    except ExpressionUnsafeError as e:
        raise ExpressionUnsafeError(
            f"deterministic 干扰项表达式求值失败 (error_type_id={rule.error_type_id!r}): {e}"
        ) from e
    # 表达式可能返回 list/tuple（一次产生多个干扰项）
    if isinstance(result, (list, tuple)):
        values: list[Any] = list(result)
    else:
        values = [result]
    if not values:
        raise ValueError(
            f"deterministic 规则表达式返回空列表 (error_type_id={rule.error_type_id!r})"
        )
    return [
        _make_option(
            v,
            error_binding=rule.error_type_id,
            label=rule.label,
            answer_value=answer_value,
            allow_collision=allow_collision,
        )
        for v in values
    ]


def _generate_corpus_sample(
    rule: DistractorRule,
    *,
    answer_value: Any,
    allow_collision: bool,
) -> list[DistractorOption]:
    """corpus_sample 规则：返回占位（等待 B 线语料装配）。"""
    if not rule.corpus_ref:
        raise ValueError(
            "corpus_sample 规则缺少 corpus_ref（rule.corpus_ref 为空，B 线未接入）"
        )
    # 占位 value=None：B 线接入后由语料装配回填真实值（T-W2-017+）
    option = _make_option(
        None,
        error_binding=rule.error_type_id,
        label=rule.label or rule.corpus_ref,
        answer_value=answer_value,
        allow_collision=allow_collision,
        corpus_ref=rule.corpus_ref,
    )
    return [option]


def generate_distractors(
    rule: DistractorRule,
    slot_values: dict[str, Any],
    *,
    answer_value: Any = None,
    allow_collision: bool = False,
) -> DistractorResult:
    """按规则生成干扰项选项列表。

    Args:
        rule: 来自母题 spec.distractor_rules.rules[*] 的单条规则。
        slot_values: 槽值字典（注入 deterministic 求值器的 env）。
        answer_value: 正解值，用于碰撞检查；None 表示不做碰撞检查。
        allow_collision: 抽样容差。True 时碰撞改为标记 collision=True
            而非抛错；默认 False（确定性场景必须严格不碰撞）。

    Returns:
        DistractorResult：options 列表（至少 1 项），每项含
        value/label/error_binding/collision/corpus_ref。

    Raises:
        DistractorCollisionError: 干扰项值与正解值碰撞且 allow_collision=False。
        ValueError: 规则配置错误（缺 expression 或 corpus_ref）。
        ExpressionUnsafeError: deterministic 表达式求值失败。

    验收对照：
        §1 返回 {options: [{value, label, error_binding}]} ✅（含扩展字段）
        §2 deterministic 用 T-W2-002 求值；corpus_sample 返回 corpus_ref 占位 ✅
        §3 碰撞抛 DistractorCollisionError（抽样容差可配） ✅
        §5 不 import 学科包 ✅
    """
    if rule.rule_type == "deterministic":
        options = _generate_deterministic(
            rule,
            slot_values,
            answer_value=answer_value,
            allow_collision=allow_collision,
        )
    elif rule.rule_type == "corpus_sample":
        options = _generate_corpus_sample(
            rule,
            answer_value=answer_value,
            allow_collision=allow_collision,
        )
    else:
        # DistractorRule.rule_type 已是 Literal，理论不可达
        raise ValueError(f"未知 rule_type: {rule.rule_type!r}")

    return DistractorResult(options=options)


__all__ = [
    "DistractorCollisionError",
    "DistractorOption",
    "DistractorResult",
    "generate_distractors",
]
