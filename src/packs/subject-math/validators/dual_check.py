"""T-W2-026 数学双实现独立验算验证器.

架构 v2 §4.3 / §5.2：用 SymPy 独立重算 answer_program，与实例化引擎结果比对。
双实现的目的：验算器与实例化引擎不共享任何代码，避免同源 bug 导致
「自己验证自己」的假阳性通过。

设计要点：
  1. **零代码共享**：本模块不 import 任何 src.core.instantiation 子模块
     （engine / expr / distractor / dsl）。表达式求值用 SymPy + 本模块
     独立实现的 AST→SymPy 转换器，与引擎的 ast 求值器完全独立。
  2. **精确算术**：int→sympy.Integer、decimal→sympy.Rational(str)、
     fraction→sympy.Rational(num,den)，全程无浮点漂移。
  3. **表达式语法**：支持 + - * / // % **、比较、and/or/not、三元 if/else、
     白名单函数 abs/min/max/sqrt/floor/ceil/round——与引擎求值器白名单对齐，
     但实现路径完全不同（引擎走 Python 原生算术，本模块走 SymPy 符号运算）。
  4. **除零检测**：SymPy 的 / 对零除数返回 zoo（不抛错），本模块在 AST
     转换阶段显式检测除零并抛 _DivisionByZeroMarker，让验证器能优雅处理。
  5. **比对策略**：优先精确比较 simplify(a-b)==0；若精确比较失败（如无理数
     vs float），退化到数值比较（容差 1e-9）。

宪法 X6 反向：学科包可依赖核心域暴露的框架（gate.validator 基类/注册表），
但不私接核心域内部实现（instantiation.*）。
"""
from __future__ import annotations

import ast
import time
from decimal import Decimal as _PyDecimal
from fractions import Fraction as _PyFraction
from typing import Any

import sympy

from src.core.gate.validator import (
    GateContext,
    Validator,
    ValidatorResult,
    register_validator,
)

__all__ = [
    "DualCheckValidator",
    "evaluate_with_sympy",
    "_DivisionByZeroMarker",
    "_build_sympy_env",
    "_answers_equal",
    "_engine_answer_to_sympy",
]


# ────────────────────────────────────────────────────────────────────
# SymPy 函数白名单（与引擎 SAFE_FUNCTIONS 对齐，但映射到 SymPy 实现）
# ────────────────────────────────────────────────────────────────────
# 为什么不用 sympy.sympify 直传字符串：sympify 会解析任意 SymPy 语法，
# 安全性不可控；本模块用 ast 白名单 + 显式 SymPy 函数映射，保证只接受
# 引擎求值器同样允许的语法子集。
_SYMPY_FUNCS: dict[str, Any] = {
    "abs": sympy.Abs,
    "min": sympy.Min,
    "max": sympy.Max,
    "sqrt": sympy.sqrt,
    "floor": sympy.floor,
    "ceil": sympy.ceiling,
}


class _DivisionByZeroMarker(Exception):
    """求值过程中检测到除零.

    独立于引擎的 ExpressionUnsafeError/ZeroDivisionError——本模块不引用
    引擎异常类型，用自定义异常标记除零，供验证器与采样器统一捕获。
    """


# ────────────────────────────────────────────────────────────────────
# 参数值 → SymPy 精确值
# ────────────────────────────────────────────────────────────────────


def _to_sympy_value(value: Any, slot_type: str) -> sympy.Basic:
    """按 slot.type 把参数值转为 SymPy 精确值.

    为什么不直接 sympy.sympify(value)：sympify 对 float 会产生二进制漂移
    （0.1→Rational(3602879701896397, 36028797018963968)）；本函数按 slot
    类型走 str→Rational 路径，保证 decimal/fraction 的精确表示。
    """
    if slot_type == "int":
        return sympy.Integer(int(value))
    if slot_type == "decimal":
        # str(Decimal('3.14')) = '3.14' → Rational('3.14') = Rational(157, 50)
        return sympy.Rational(str(value))
    if slot_type == "fraction":
        s = str(value).strip()
        if "/" in s:
            num, _, den = s.partition("/")
            return sympy.Rational(int(num.strip()), int(den.strip()))
        # "0.75" 等十进制形式
        return sympy.Rational(s)
    if slot_type == "bool":
        return sympy.true if bool(value) else sympy.false
    # string / choice：不参与算术；若出现在表达式中，sympify 兜底
    return sympy.sympify(value)


def _build_sympy_env(
    params: dict[str, Any], slots: dict[str, Any]
) -> dict[str, sympy.Basic]:
    """构造 SymPy 求值环境：把 params 按 slots 声明的类型转为 SymPy 值.

    Args:
        params: 参数字典（槽名 → 值）。
        slots: spec.slots（槽名 → Slot 定义，dict 或 Pydantic 模型）。

    Returns:
        槽名 → SymPy 值的字典。
    """
    env: dict[str, sympy.Basic] = {}
    for name, value in params.items():
        slot = slots.get(name) if isinstance(slots, dict) else None
        if slot is None:
            continue
        # 兼容 dict（raw spec）与 Pydantic Slot 模型
        if isinstance(slot, dict):
            slot_type = slot.get("type", "string")
        else:
            slot_type = getattr(slot, "type", "string")
        try:
            env[name] = _to_sympy_value(value, slot_type)
        except (TypeError, ValueError, ArithmeticError):
            # 无法按类型转换时兜底 sympify（求值时若用到会自然报错）
            env[name] = sympy.sympify(value)
    return env


# ────────────────────────────────────────────────────────────────────
# Python AST → SymPy 表达式（独立于引擎的 ast 求值器）
# ────────────────────────────────────────────────────────────────────


def _convert_node(
    node: ast.AST, env: dict[str, sympy.Basic]
) -> sympy.Basic:
    """递归把 Python AST 节点转为 SymPy 表达式.

    与引擎 evaluator._eval_node 的区别：本函数产出 SymPy Basic 对象
    （符号精确运算），引擎产出 Python 原生值（int/Decimal/Fraction）。
    两者代码路径完全独立——这是双实现验算的核心价值。
    """
    if isinstance(node, ast.Expression):
        return _convert_node(node.body, env)

    # 字面量
    if isinstance(node, ast.Constant):
        v = node.value
        if isinstance(v, bool):
            return sympy.true if v else sympy.false
        if isinstance(v, int):
            return sympy.Integer(v)
        if isinstance(v, float):
            # float→str→Rational 避免二进制漂移
            return sympy.Rational(str(v))
        if v is None:
            return sympy.sympify(None)
        return sympy.sympify(v)

    # 变量引用
    if isinstance(node, ast.Name):
        if node.id in env:
            return env[node.id]
        if node.id == "True":
            return sympy.true
        if node.id == "False":
            return sympy.false
        if node.id == "None":
            return sympy.sympify(None)
        raise ValueError(f"未声明的标识符：{node.id!r}")

    # 二元运算
    if isinstance(node, ast.BinOp):
        left = _convert_node(node.left, env)
        right = _convert_node(node.right, env)
        op = node.op
        if isinstance(op, ast.Add):
            return left + right
        if isinstance(op, ast.Sub):
            return left - right
        if isinstance(op, ast.Mult):
            return left * right
        if isinstance(op, ast.Div):
            # 显式检测除零：SymPy 的 / 对零除数返回 zoo（不抛错），
            # 这里提前拦截以便上层（采样器/验证器）统一处理。
            if right == 0:
                raise _DivisionByZeroMarker(f"除零：{left} / 0")
            return left / right
        if isinstance(op, ast.FloorDiv):
            if right == 0:
                raise _DivisionByZeroMarker(f"除零：{left} // 0")
            # Python // 是 floor 除法：floor(a / b)
            return sympy.floor(left / right)
        if isinstance(op, ast.Mod):
            if right == 0:
                raise _DivisionByZeroMarker(f"除零：{left} % 0")
            return sympy.Mod(left, right)
        if isinstance(op, ast.Pow):
            return left ** right
        raise ValueError(f"不支持的二元运算符：{type(op).__name__}")

    # 一元运算
    if isinstance(node, ast.UnaryOp):
        operand = _convert_node(node.operand, env)
        if isinstance(node.op, ast.UAdd):
            return +operand
        if isinstance(node.op, ast.USub):
            return -operand
        if isinstance(node.op, ast.Not):
            return sympy.Not(operand)
        raise ValueError(f"不支持的一元运算符：{type(node.op).__name__}")

    # 布尔短路 and / or
    if isinstance(node, ast.BoolOp):
        values = [_convert_node(v, env) for v in node.values]
        if isinstance(node.op, ast.And):
            result: sympy.Basic = sympy.true
            for v in values:
                result = sympy.And(result, v)
            return result
        if isinstance(node.op, ast.Or):
            result = sympy.false
            for v in values:
                result = sympy.Or(result, v)
            return result
        raise ValueError(f"不支持的布尔运算符：{type(node.op).__name__}")

    # 比较运算（可链式 a < b < c）
    if isinstance(node, ast.Compare):
        left = _convert_node(node.left, env)
        result = sympy.true
        for op, comparator in zip(node.ops, node.comparators):
            right = _convert_node(comparator, env)
            if isinstance(op, ast.Lt):
                cmp: sympy.Basic = sympy.StrictLessThan(left, right)
            elif isinstance(op, ast.LtE):
                cmp = sympy.LessThan(left, right)
            elif isinstance(op, ast.Gt):
                cmp = sympy.StrictLessThan(right, left)
            elif isinstance(op, ast.GtE):
                cmp = sympy.LessThan(right, left)
            elif isinstance(op, ast.Eq):
                cmp = sympy.Eq(left, right)
            elif isinstance(op, ast.NotEq):
                cmp = sympy.Ne(left, right)
            else:
                raise ValueError(f"不支持的比较运算符：{type(op).__name__}")
            result = sympy.And(result, cmp)
            left = right
        return result

    # 三元 if/else
    if isinstance(node, ast.IfExp):
        test = _convert_node(node.test, env)
        body = _convert_node(node.body, env)
        orelse = _convert_node(node.orelse, env)
        return sympy.Piecewise((body, test), (orelse, True))

    # 函数调用（白名单）
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name):
            raise ValueError("仅允许白名单函数直调（禁止属性访问调用）")
        func_name = node.func.id
        if node.keywords:
            raise ValueError("禁止使用关键字参数")
        for arg in node.args:
            if isinstance(arg, ast.Starred):
                raise ValueError("禁止使用 *args 解包")
        args = [_convert_node(a, env) for a in node.args]

        if func_name == "round":
            # round(x) 或 round(x, n)——SymPy round 行为与 Python 一致（银行家舍入）
            if len(args) == 1:
                return sympy.round(args[0])
            if len(args) == 2:
                return sympy.round(args[0], int(args[1]))
            raise ValueError(f"round 参数数量不合法：{len(args)}")
        if func_name not in _SYMPY_FUNCS:
            raise ValueError(f"非白名单函数：{func_name!r}")
        return _SYMPY_FUNCS[func_name](*args)

    raise ValueError(f"不支持的语法节点：{type(node).__name__}")


def evaluate_with_sympy(
    expression: str, env: dict[str, sympy.Basic]
) -> sympy.Basic:
    """用 SymPy 独立求值表达式.

    本函数是双实现验算的核心：与引擎 evaluate() 完全独立的代码路径，
    用 SymPy 符号运算重算同一表达式。

    Args:
        expression: 表达式字符串（Python 算术语法）。
        env: 变量绑定（槽名 → SymPy 值）。

    Returns:
        SymPy Basic：求值结果（精确算术）。

    Raises:
        _DivisionByZeroMarker: 表达式含除零。
        ValueError: 表达式含不支持的语法/函数/标识符。
        SyntaxError: 表达式语法错误。
    """
    if not isinstance(expression, str):
        raise ValueError(
            f"表达式必须为 str，实际为 {type(expression).__name__}"
        )
    tree = ast.parse(expression, mode="eval")
    return _convert_node(tree.body, env)


# ────────────────────────────────────────────────────────────────────
# 引擎答案 → SymPy 值（用于比对）
# ────────────────────────────────────────────────────────────────────


def _engine_answer_to_sympy(engine_answer: Any) -> sympy.Basic:
    """把引擎计算的正解值转为 SymPy 值，用于与 SymPy 独立重算结果比对.

    引擎正解可能是 int / Decimal / Fraction / float / bool，本函数按类型
    走 str→Rational 路径避免浮点漂移。
    """
    if isinstance(engine_answer, bool):
        return sympy.true if engine_answer else sympy.false
    if isinstance(engine_answer, int):
        return sympy.Integer(engine_answer)
    if isinstance(engine_answer, _PyDecimal):
        return sympy.Rational(str(engine_answer))
    if isinstance(engine_answer, _PyFraction):
        return sympy.Rational(engine_answer.numerator, engine_answer.denominator)
    if isinstance(engine_answer, float):
        return sympy.Rational(str(engine_answer))
    if isinstance(engine_answer, str):
        return sympy.Rational(engine_answer)
    # 兜底：交给 sympy 解析
    return sympy.sympify(engine_answer)


def _answers_equal(sympy_answer: sympy.Basic, engine_sympy: sympy.Basic) -> bool:
    """比较 SymPy 独立重算值与引擎值是否一致.

    比对策略：
      1. 精确比较：simplify(a - b) == 0（覆盖 int/分数/小数等精确算术）。
      2. 数值退化：若精确比较失败（如 sqrt 产生无理数 vs float），
         退化到 float 数值比较，容差 1e-9。
    """
    # 精确比较
    diff = sympy.simplify(sympy_answer - engine_sympy)
    if diff == 0:
        return True
    # 数值退化（无理数 / float 场景）
    try:
        num_diff = float(diff)
        if abs(num_diff) < 1e-9:
            return True
    except (TypeError, ValueError):
        pass
    return False


# ────────────────────────────────────────────────────────────────────
# 双实现验算验证器
# ────────────────────────────────────────────────────────────────────


class DualCheckValidator(Validator):
    """双实现独立验算验证器.

    用 SymPy 独立重算 answer_program，与实例化引擎计算的正解比对。
    不一致则 fail（阻断）；SymPy 无法计算（如除零）则 review（无法判定）。

    ctx.artifact_payload 期望字段：
    - spec: 母题 spec dict（含 answer_program.expression, slots）。
    - params: 实例化参数（槽名 → 值）。
    - engine_answer: 实例化引擎计算的正解值。

    verdict 规则：
    - review：payload 缺字段 / SymPy 检测到除零 / 求值失败。
    - pass：SymPy 重算值 == 引擎值。
    - fail：SymPy 重算值 != 引擎值（阻断签发）。
    """

    validator_id = "dual_check"
    version = "1.0.0+subject-math"
    blocking = True
    cost_tier = "expensive"

    async def validate(
        self, artifact_ref: str, ctx: GateContext
    ) -> ValidatorResult:
        start = time.monotonic()
        elapsed_ms = lambda: int((time.monotonic() - start) * 1000)

        payload = ctx.artifact_payload
        if payload is None:
            return self._timed_result(
                verdict="review",
                evidence={"reason": "artifact_payload 为 None"},
                confidence=_PyDecimal("0.000"),
                elapsed_ms=elapsed_ms(),
            )

        spec = payload.get("spec")
        params = payload.get("params")
        engine_answer = payload.get("engine_answer")

        if not isinstance(spec, dict) or not isinstance(params, dict):
            return self._timed_result(
                verdict="review",
                evidence={"reason": "payload 缺少 spec(dict) 或 params(dict)"},
                confidence=_PyDecimal("0.000"),
                elapsed_ms=elapsed_ms(),
            )
        if engine_answer is None:
            return self._timed_result(
                verdict="review",
                evidence={"reason": "payload 缺少 engine_answer"},
                confidence=_PyDecimal("0.000"),
                elapsed_ms=elapsed_ms(),
            )

        answer_program = spec.get("answer_program") or {}
        expression = answer_program.get("expression")
        if not expression:
            return self._timed_result(
                verdict="review",
                evidence={"reason": "spec.answer_program.expression 缺失"},
                confidence=_PyDecimal("0.000"),
                elapsed_ms=elapsed_ms(),
            )

        slots = spec.get("slots") or {}

        # SymPy 独立重算
        try:
            env = _build_sympy_env(params, slots)
            sympy_answer = evaluate_with_sympy(expression, env)
        except _DivisionByZeroMarker as e:
            return self._timed_result(
                verdict="review",
                evidence={
                    "reason": "SymPy 检测到除零，无法独立验算",
                    "detail": str(e),
                    "expression": expression,
                    "params": params,
                },
                confidence=_PyDecimal("0.000"),
                elapsed_ms=elapsed_ms(),
            )
        except (ValueError, SyntaxError) as e:
            return self._timed_result(
                verdict="review",
                evidence={
                    "reason": "SymPy 求值失败",
                    "detail": f"{type(e).__name__}: {e}",
                    "expression": expression,
                },
                confidence=_PyDecimal("0.000"),
                elapsed_ms=elapsed_ms(),
            )

        # 引擎答案 → SymPy 值
        try:
            engine_sympy = _engine_answer_to_sympy(engine_answer)
        except (TypeError, ValueError, ArithmeticError) as e:
            return self._timed_result(
                verdict="review",
                evidence={
                    "reason": "engine_answer 无法转为 SymPy 值",
                    "detail": f"{type(e).__name__}: {e}",
                    "engine_answer": repr(engine_answer),
                },
                confidence=_PyDecimal("0.000"),
                elapsed_ms=elapsed_ms(),
            )

        # 比对
        if _answers_equal(sympy_answer, engine_sympy):
            return self._timed_result(
                verdict="pass",
                evidence={
                    "sympy_answer": str(sympy_answer),
                    "engine_answer": str(engine_answer),
                    "expression": expression,
                    "method": "sympy_independent_recompute",
                },
                confidence=_PyDecimal("1.000"),
                elapsed_ms=elapsed_ms(),
            )

        return self._timed_result(
            verdict="fail",
            evidence={
                "reason": "SymPy 独立验算与引擎结果不一致",
                "sympy_answer": str(sympy_answer),
                "engine_answer": str(engine_answer),
                "expression": expression,
                "params": params,
            },
            confidence=_PyDecimal("1.000"),
            elapsed_ms=elapsed_ms(),
        )


# 模块加载时注册（pack_id='subject-math'，与 generic.py 注册 platform 同理）
register_validator("subject-math", DualCheckValidator)
