"""母题 DSL v1：六大块 Schema 与 Linter（T-W2-001）。

六大块：objective / slots / variation_axes / presentation / answer_program /
distractor_rules（架构 v2 §4.1 A 线）。DSL 自身版本化（dsl_version 字段）。
本包只做静态结构校验，不执行实例化（实例化在 instantiation/engine，T-W2-004）。
"""
from src.core.instantiation.dsl.linter import LintError, LintResult, lint
from src.core.instantiation.dsl.schema import (
    ALLOWED_SLOT_TYPES,
    AnswerProgram,
    DistractorRule,
    DistractorRules,
    ItemTemplateSpec,
    Objective,
    ObjectiveStep,
    Presentation,
    PresentationBlock,
    Slot,
    VariationAxes,
    VariationAxis,
)

__all__ = [
    "ALLOWED_SLOT_TYPES",
    "AnswerProgram",
    "DistractorRule",
    "DistractorRules",
    "ItemTemplateSpec",
    "LintError",
    "LintResult",
    "Objective",
    "ObjectiveStep",
    "Presentation",
    "PresentationBlock",
    "Slot",
    "VariationAxes",
    "VariationAxis",
    "lint",
]
