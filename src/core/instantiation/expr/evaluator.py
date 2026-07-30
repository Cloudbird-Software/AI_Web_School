"""安全表达式求值器核心实现（T-W2-002）。

实现要点：
  1. 用 ast.parse(mode='eval') 解析为 AST。
  2. 静态遍历 AST，禁止非白名单节点（循环/赋值/import/属性访问/下标/
     lambda/comprehension/starred/yield/await/joinedstr/namedexpr 等）。
  3. 求值阶段：BinOp/UnaryOp/BoolOp/Compare/IfExp/Call 严格走白名单函数表；
     Name 节点除 True/False/None 外必须在 env 中查得，否则抛
     ExpressionUnsafeError（未声明标识符）。
  4. 运行时异常（除零、类型错误、值错误）包装为 ExpressionUnsafeError，
     保留原异常为 __cause__，便于上游统一处理（验收 §4）。

宪法 X6：本模块不 import 任何学科包/学段包。
"""
from __future__ import annotations

import ast
import math
from typing import Any, Callable

# ────────────────────────────────────────────────────────────────────
# 白名单函数表
# 为什么用 dict 而非 module：函数级白名单避免暴露 __builtins__ 等危险属性；
# 学科函数库通过 REGISTERED_SUBJECT_FUNCTIONS 注入（架构 v2 §4.1 扩展点），
# 不污染本表。
# ────────────────────────────────────────────────────────────────────
BASE_SAFE_FUNCTIONS: dict[str, Callable[..., Any]] = {
    "abs": abs,
    "min": min,
    "max": max,
    "sqrt": math.sqrt,
    "round": round,
    "floor": math.floor,
    "ceil": math.ceil,
}

REGISTERED_SUBJECT_FUNCTIONS: dict[str, Callable[..., Any]] = {}


def register_subject_functions(fn_dict: dict[str, Callable[..., Any]]) -> None:
    """注册学科函数到求值器白名单（A5合规：核心不硬引学科，由调用方延迟注册）.

    与 BASE_SAFE_FUNCTIONS 命名冲突时，后注册的覆盖先注册的（学科可覆盖内置）。
    """
    REGISTERED_SUBJECT_FUNCTIONS.update(fn_dict)


def _get_allowed_fns() -> dict[str, Callable[..., Any]]:
    """合并内置白名单与已注册学科函数（运行时动态合并）."""
    merged: dict[str, Callable[..., Any]] = {}
    merged.update(BASE_SAFE_FUNCTIONS)
    merged.update(REGISTERED_SUBJECT_FUNCTIONS)
    return merged


# 允许的 AST 节点类型集合
# 严禁：Attribute（防 obj.__import__）、Subscript、Import、Loop、Lambda 等。
_ALLOWED_BINOPS: frozenset[type] = frozenset(
    {ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow}
)
_ALLOWED_UNARYOPS: frozenset[type] = frozenset({ast.UAdd, ast.USub, ast.Not})
_ALLOWED_BOOLOPS: frozenset[type] = frozenset({ast.And, ast.Or})
_ALLOWED_CMPOPS: frozenset[type] = frozenset(
    {ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.Eq, ast.NotEq}
)

# 内置常量名（ast.Constant 不再覆盖 True/False/None 在 3.8+ 已用 Constant；
# 这里仍保留是为了兼容部分以 Name 形式出现的 True/False/None）
_BUILTIN_CONSTANTS: frozenset[str] = frozenset({"True", "False", "None"})


class ExpressionUnsafeError(Exception):
    """表达式不安全或求值失败时抛出。

    覆盖三类场景（验收 §2 / §4）：
      - 静态不安全：含 import/open/loop/attribute/subscript/lambda 等禁节点
      - 名字不安全：未声明的标识符、非白名单函数调用
      - 运行时不安全：除零、类型错误、值错误等被包装为本异常
    """


# ────────────────────────────────────────────────────────────────────
# 静态校验：遍历 AST 拒绝非白名单节点
# ────────────────────────────────────────────────────────────────────

def _node_name(node: ast.AST) -> str:
    """返回节点类型可读名，用于错误信息。"""
    return type(node).__name__


def _validate_node(node: ast.AST) -> None:
    """递归检查 AST 节点是否全部在白名单内。

    Args:
        node: 待检查的 AST 节点。

    Raises:
        ExpressionUnsafeError: 发现禁用节点时立即抛出。
    """
    # ── 顶层 ──
    if isinstance(node, ast.Expression):
        _validate_node(node.body)
        return

    # ── 字面量（3.8+ 已合并 True/False/None 到 Constant） ──
    if isinstance(node, ast.Constant):
        return

    # ── 变量引用 ──
    if isinstance(node, ast.Name):
        # 运行时再校验是否在 env 中；这里只确认它是 Name 形式
        return

    # ── 二元运算 ──
    if isinstance(node, ast.BinOp):
        if type(node.op) not in _ALLOWED_BINOPS:
            raise ExpressionUnsafeError(
                f"禁止的二元运算符：{_node_name(node.op)}"
            )
        _validate_node(node.left)
        _validate_node(node.right)
        return

    # ── 一元运算 ──
    if isinstance(node, ast.UnaryOp):
        if type(node.op) not in _ALLOWED_UNARYOPS:
            raise ExpressionUnsafeError(
                f"禁止的一元运算符：{_node_name(node.op)}"
            )
        _validate_node(node.operand)
        return

    # ── 布尔短路运算 and/or ──
    if isinstance(node, ast.BoolOp):
        if type(node.op) not in _ALLOWED_BOOLOPS:
            raise ExpressionUnsafeError(
                f"禁止的布尔运算符：{_node_name(node.op)}"
            )
        for v in node.values:
            _validate_node(v)
        return

    # ── 比较运算（可链式：a < b < c） ──
    if isinstance(node, ast.Compare):
        for op in node.ops:
            if type(op) not in _ALLOWED_CMPOPS:
                raise ExpressionUnsafeError(
                    f"禁止的比较运算符：{_node_name(op)}"
                )
        _validate_node(node.left)
        for c in node.comparators:
            _validate_node(c)
        return

    # ── 条件表达式 if/else（三元） ──
    if isinstance(node, ast.IfExp):
        _validate_node(node.test)
        _validate_node(node.body)
        _validate_node(node.orelse)
        return

    # ── 函数调用：仅允许 ast.Name + 白名单 ──
    if isinstance(node, ast.Call):
        # 禁止 func 为 Attribute（obj.method()）：防止 __import__/getattr 套娃
        if not isinstance(node.func, ast.Name):
            raise ExpressionUnsafeError(
                f"禁止的调用形式：{_node_name(node.func)}（仅允许白名单函数直调）"
            )
        func_name = node.func.id
        allowed_fns = _get_allowed_fns()
        if func_name not in allowed_fns:
            raise ExpressionUnsafeError(
                f"调用非白名单函数：{func_name!r}"
            )
        # 关键字参数禁止：求值器不保证函数对 kwargs 行为可控
        if node.keywords:
            raise ExpressionUnsafeError("禁止使用关键字参数")
        # *args / **kwargs 禁止
        for arg in node.args:
            if isinstance(arg, ast.Starred):
                raise ExpressionUnsafeError("禁止使用 *args 解包")
            _validate_node(arg)
        return

    # ── 其余节点一律拒绝 ──
    # 涵盖：Import/ImportFrom、Attribute、Subscript、For/While、Try、
    # FunctionDef/ClassDef/Lambda、ListComp/SetComp/DictComp/GeneratorExp、
    # Assign/AugAssign/AnnAssign、Delete、JoinedStr/FormattedValue、NamedExpr、
    # Await/Yield/YieldFrom 等。
    raise ExpressionUnsafeError(
        f"禁止的语法节点：{_node_name(node)}"
    )


def validate(expression: str) -> None:
    """静态校验表达式是否安全（不执行）。

    Args:
        expression: 待校验的表达式字符串。

    Raises:
        ExpressionUnsafeError: 表达式含禁用语法或不安全结构时抛出。
        SyntaxError: 表达式本身无法解析（语法错误）。
    """
    if not isinstance(expression, str):
        raise ExpressionUnsafeError(
            f"表达式必须为 str，实际为 {type(expression).__name__}"
        )
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as e:
        # 非法语法（如未闭合括号、赋值语句、循环语句）统一为 SyntaxError 透传
        raise e
    _validate_node(tree)


# ────────────────────────────────────────────────────────────────────
# 求值
# ────────────────────────────────────────────────────────────────────

def _eval_node(node: ast.AST, env: dict[str, Any]) -> Any:
    """递归求值 AST 节点（已通过 validate 静态校验）。

    Args:
        node: AST 节点。
        env: 变量绑定（槽值/学科函数扩展）。

    Returns:
        求值结果（任意 Python 值）。

    Raises:
        ExpressionUnsafeError: 运行时未声明标识符 / 类型错误 / 除零等。
    """
    if isinstance(node, ast.Expression):
        return _eval_node(node.body, env)

    if isinstance(node, ast.Constant):
        return node.value

    if isinstance(node, ast.Name):
        name = node.id
        if name in _BUILTIN_CONSTANTS:
            # True/False/None 在 3.8+ 通常是 Constant，这里兜底
            return {"True": True, "False": False, "None": None}[name]
        if name not in env:
            raise ExpressionUnsafeError(f"未声明的标识符：{name!r}")
        return env[name]

    if isinstance(node, ast.BinOp):
        left = _eval_node(node.left, env)
        right = _eval_node(node.right, env)
        try:
            return _apply_binop(node.op, left, right)
        except ExpressionUnsafeError:
            raise
        except Exception as e:  # ZeroDivisionError / TypeError / ValueError 等
            raise ExpressionUnsafeError(
                f"二元运算 {_node_name(node.op)} 失败：{type(e).__name__}: {e}"
            ) from e

    if isinstance(node, ast.UnaryOp):
        operand = _eval_node(node.operand, env)
        try:
            if isinstance(node.op, ast.UAdd):
                return +operand
            if isinstance(node.op, ast.USub):
                return -operand
            if isinstance(node.op, ast.Not):
                return not operand
        except Exception as e:
            raise ExpressionUnsafeError(
                f"一元运算 {_node_name(node.op)} 失败：{type(e).__name__}: {e}"
            ) from e
        raise ExpressionUnsafeError(
            f"未支持的一元运算符：{_node_name(node.op)}"
        )

    if isinstance(node, ast.BoolOp):
        # 短路：and 全真则取最后一个真值；or 取第一个真值
        if isinstance(node.op, ast.And):
            result: Any = True
            for v in node.values:
                result = _eval_node(v, env)
                if not result:
                    return result
            return result
        # ast.Or
        result = False
        for v in node.values:
            result = _eval_node(v, env)
            if result:
                return result
        return result

    if isinstance(node, ast.Compare):
        left = _eval_node(node.left, env)
        for op, comparator in zip(node.ops, node.comparators):
            right = _eval_node(comparator, env)
            try:
                ok = _apply_cmpop(op, left, right)
            except Exception as e:
                raise ExpressionUnsafeError(
                    f"比较 {_node_name(op)} 失败：{type(e).__name__}: {e}"
                ) from e
            if not ok:
                return False
            left = right
        return True

    if isinstance(node, ast.IfExp):
        test = _eval_node(node.test, env)
        if test:
            return _eval_node(node.body, env)
        return _eval_node(node.orelse, env)

    if isinstance(node, ast.Call):
        func_name = node.func.id  # type: ignore[attr-defined]
        allowed_fns = _get_allowed_fns()
        func = allowed_fns[func_name]
        args = [_eval_node(a, env) for a in node.args]
        try:
            return func(*args)
        except Exception as e:
            raise ExpressionUnsafeError(
                f"调用 {func_name}() 失败：{type(e).__name__}: {e}"
            ) from e

    # 静态校验应已拒绝所有其他节点
    raise ExpressionUnsafeError(
        f"求值阶段遇到未支持节点：{_node_name(node)}（应已被 validate 拦截）"
    )


def _apply_binop(op: ast.AST, left: Any, right: Any) -> Any:
    """应用二元运算符（不使用 eval）。"""
    if isinstance(op, ast.Add):
        return left + right
    if isinstance(op, ast.Sub):
        return left - right
    if isinstance(op, ast.Mult):
        return left * right
    if isinstance(op, ast.Div):
        return left / right
    if isinstance(op, ast.FloorDiv):
        return left // right
    if isinstance(op, ast.Mod):
        return left % right
    if isinstance(op, ast.Pow):
        return left**right
    raise ExpressionUnsafeError(f"未支持的二元运算符：{_node_name(op)}")


def _apply_cmpop(op: ast.AST, left: Any, right: Any) -> bool:
    """应用比较运算符。"""
    if isinstance(op, ast.Lt):
        return left < right
    if isinstance(op, ast.LtE):
        return left <= right
    if isinstance(op, ast.Gt):
        return left > right
    if isinstance(op, ast.GtE):
        return left >= right
    if isinstance(op, ast.Eq):
        return left == right
    if isinstance(op, ast.NotEq):
        return left != right
    raise ExpressionUnsafeError(f"未支持的比较运算符：{_node_name(op)}")


def evaluate(expression: str, env: dict[str, Any] | None = None) -> Any:
    """安全求值表达式。

    Args:
        expression: 表达式字符串。
        env: 变量绑定字典。允许覆盖内置常量（True/False/None 除外），
            用于注入学科函数库扩展点（架构 v2 §4.1）。

    Returns:
        求值结果。

    Raises:
        ExpressionUnsafeError: 表达式不安全或运行时求值失败。
        SyntaxError: 表达式语法错误。

    确定性保证（验收 §3）：
        同一 (expression, env) 在同一 Python 版本下任意次求值结果一致；
        浮点运算遵循 IEEE 754，本求值器不做额外随机化。
    """
    if env is None:
        env = {}
    elif not isinstance(env, dict):
        raise ExpressionUnsafeError(
            f"env 必须为 dict，实际为 {type(env).__name__}"
        )
    validate(expression)
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as e:
        raise e
    return _eval_node(tree, env)


# 向后兼容别名（原 SAFE_FUNCTIONS = BASE_SAFE_FUNCTIONS）
SAFE_FUNCTIONS: dict[str, Callable[..., Any]] = BASE_SAFE_FUNCTIONS

__all__ = [
    "BASE_SAFE_FUNCTIONS",
    "REGISTERED_SUBJECT_FUNCTIONS",
    "SAFE_FUNCTIONS",
    "ExpressionUnsafeError",
    "evaluate",
    "register_subject_functions",
    "validate",
]
