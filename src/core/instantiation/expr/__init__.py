"""安全表达式求值器（T-W2-002）。

DSL answer_program 与 distractor_rules 使用的纯函数表达式求值：
无 IO、无循环、无递归调用、无属性访问、无 import。
函数调用只能命中白名单（核心数学函数 + env 注入的学科函数库扩展点）。

宪法 X6：本模块不 import 任何学科包/学段包。
"""
from src.core.instantiation.expr.evaluator import (
    SAFE_FUNCTIONS,
    ExpressionUnsafeError,
    evaluate,
    validate,
)

__all__ = [
    "SAFE_FUNCTIONS",
    "ExpressionUnsafeError",
    "evaluate",
    "validate",
]
