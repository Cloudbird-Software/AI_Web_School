#!/usr/bin/env python3
"""T-W4-033 静态扫描：跨用户排名查询路径不存在实证（宪法 D8）.

落地 specs/constitution.md#D8「代码层不得提供跨用户成绩排名的查询路径；
对外呈现一律等级化」与 ADR §4.8「代码层不提供跨用户排名查询」.

扫描 src/ 全部 .py 代码，检测任何按成绩排序的跨用户排名查询路径：
- SQL 字符串含 ``ORDER BY ... score`` / ``RANK()`` / ``DENSE_RANK()`` 等窗口函数
- ORM ``.order_by()`` 调用含 score 类列（如 ``.order_by(StudentScore.total_score)``）
- API 路由路径含 ``/rank`` ``/ranking`` ``/leaderboard``
- 函数名含排名语义（``get_ranking`` / ``compute_rank`` / ``leaderboard``）

为什么不报假阳性（验收 5）：
- 合法的单用户内排序（如 ``ORDER BY created_at``、``ORDER BY item_number``、
  ``ORDER BY version DESC``）不涉及 score 列，不被误报。
- docstring / 注释中提及「排名」「rank」作为禁令说明（如合规层自身文档），
  通过 AST 精确跳过 docstring 节点，不产生假阳性。

用法（CLI）：
    python scripts/ci/check_no_ranking.py [src_dir]
    默认扫描项目根 src/，发现违则以非零码退出（CI 阻断）。

用法（库）：
    from scripts.ci.check_no_ranking import scan_directory, scan_file
    violations = scan_directory(Path("src"))
"""
from __future__ import annotations

import ast
import re
import sys
from dataclasses import dataclass
from pathlib import Path

# ────────────────────────────────────────────────────────────────────
# 检测模式
# ────────────────────────────────────────────────────────────────────

# score 类列名（出现在 ORDER BY / RANK OVER / order_by() 中即判定为排名）
# 为什么把这些列为「成绩列」：它们表达的是学生表现度量，按其排序即排名。
_SCORE_COLUMNS = (
    r"score|total_score|total_points|points|"
    r"correct_count|correct_rate|correctness|accuracy|"
    r"exam_score|test_score|raw_score|scaled_score|percentile"
)

# 1. SQL ORDER BY + score 列（跨用户排名的典型形态）
_RE_SQL_ORDER_BY_SCORE = re.compile(
    rf"ORDER\s+BY\s+[^\n;]*?\b({_SCORE_COLUMNS})\b",
    re.IGNORECASE,
)

# 2. SQL 窗口函数排名：RANK() / DENSE_RANK() / PERCENT_RANK() / CUME_DIST()
_RE_SQL_WINDOW_RANK = re.compile(
    r"\b(?:RANK|DENSE_RANK|PERCENT_RANK|CUME_DIST)\s*\(\s*\)",
    re.IGNORECASE,
)

# 3. ROW_NUMBER() OVER(... score ...)：按成绩编号即排名
_RE_SQL_ROWNUM_OVER_SCORE = re.compile(
    rf"ROW_NUMBER\s*\(\s*\)\s+OVER\s*\([^)]*?\b({_SCORE_COLUMNS})\b",
    re.IGNORECASE,
)

# 4. rank 作为 SQL 查询列（SELECT rank / WHERE rank / ORDER BY rank）
#    仅匹配 rank 作为独立词出现在查询子句中（排除 rank 出现在其他单词内部）
_RE_SQL_RANK_COLUMN = re.compile(
    r"\b(?:SELECT|WHERE|ORDER\s+BY|GROUP\s+BY|HAVING)\s+[^\n;]*?\brank\b",
    re.IGNORECASE,
)

# 5. ORM .order_by(<含 score 列的表达式>)
_RE_ORM_ORDER_BY_SCORE = re.compile(
    rf"\.order_by\s*\([^)]*?\b({_SCORE_COLUMNS})\b",
    re.IGNORECASE,
)

# 6. API 路由路径含排名语义
_RE_ROUTE_RANK = re.compile(
    r"/(?:rank|ranking|rankings|leaderboard|leaderboards)\b",
    re.IGNORECASE,
)

# 7. 函数名排名语义（get_ranking / compute_rank / student_leaderboard 等）
_RE_FUNC_NAME_RANK = re.compile(
    r"(?:^|_)(?:rank|ranking|leaderboard)(?:s|ing|_for|_by|_list)?$|"
    r"^(?:get|compute|fetch|list|build)_?(?:rank|ranking|leaderboard)",
    re.IGNORECASE,
)


# ────────────────────────────────────────────────────────────────────
# 违规数据结构
# ────────────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class Violation:
    """一处排名查询违规.

    Attributes:
        filepath: 违规文件相对路径.
        lineno: 行号（1-based）.
        category: 违规类别（sql_order_by_score / sql_window_rank / ...）.
        snippet: 违规代码片段（截断，供定位）.
    """

    filepath: str
    lineno: int
    category: str
    snippet: str

    def __str__(self) -> str:
        return (
            f"{self.filepath}:{self.lineno} [{self.category}] {self.snippet}"
        )


# ────────────────────────────────────────────────────────────────────
# AST 扫描器
# ────────────────────────────────────────────────────────────────────

class _RankingScanner(ast.NodeVisitor):
    """遍历 AST 检测排名查询模式.

    为什么用 AST 而非纯正则：
    - 精确跳过 docstring（合规层文档提及「排名」是禁令说明，非查询实现）。
    - 精确定位 Call 节点（.order_by() / 路由装饰器），而非在任意文本中匹配。
    - 提供准确行号，便于开发者定位修复。
    """

    def __init__(self, filepath: str, source: str) -> None:
        self.filepath = filepath
        self.source = source
        self.violations: list[Violation] = []
        # docstring Constant 节点的 id()，扫描时跳过
        self._docstring_node_ids: set[int] = set()

    def _collect_docstrings(self, tree: ast.Module) -> None:
        """收集所有 docstring 节点 id，扫描字符串常量时跳过.

        为什么跳过 docstring：合规层 __init__.py / parental_consent.py 等模块
        的 docstring 会提及「排名」「rank」作为禁令说明（D8），这些是文档而非
        查询实现，扫描它们会产出假阳性。
        """
        for node in ast.walk(tree):
            if isinstance(
                node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
            ):
                docstring = ast.get_docstring(node, clean=False)
                if docstring and node.body:
                    first = node.body[0]
                    if (
                        isinstance(first, ast.Expr)
                        and isinstance(first.value, ast.Constant)
                        and isinstance(first.value.value, str)
                    ):
                        self._docstring_node_ids.add(id(first.value))

    def _add(self, lineno: int, category: str, snippet: str) -> None:
        """记录一处违规（snippet 截断到 120 字符防刷屏）."""
        self.violations.append(
            Violation(
                filepath=self.filepath,
                lineno=lineno,
                category=category,
                snippet=snippet.strip()[:120],
            )
        )

    # ── 字符串常量：检查 SQL 排名模式 ──

    def visit_Constant(self, node: ast.Constant) -> None:
        # 跳过 docstring
        if id(node) in self._docstring_node_ids:
            self.generic_visit(node)
            return
        if isinstance(node.value, str):
            text = node.value
            if _RE_SQL_ORDER_BY_SCORE.search(text):
                self._add(node.lineno, "sql_order_by_score", text)
            if _RE_SQL_WINDOW_RANK.search(text):
                self._add(node.lineno, "sql_window_rank", text)
            if _RE_SQL_ROWNUM_OVER_SCORE.search(text):
                self._add(node.lineno, "sql_rownum_over_score", text)
            if _RE_SQL_RANK_COLUMN.search(text):
                self._add(node.lineno, "sql_rank_column", text)
            # 路由路径含排名
            if _RE_ROUTE_RANK.search(text):
                self._add(node.lineno, "route_rank_path", text)
        self.generic_visit(node)

    # ── 函数调用：检查 .order_by(score) / 路由装饰器 ──

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func

        # .order_by(...) 调用
        if isinstance(func, ast.Attribute) and func.attr == "order_by":
            for arg in node.args:
                try:
                    arg_src = ast.unparse(arg)
                except Exception:
                    arg_src = ""
                if arg_src and _RE_ORM_ORDER_BY_SCORE.search(f".order_by({arg_src})"):
                    self._add(
                        node.lineno,
                        "orm_order_by_score",
                        f".order_by({arg_src})",
                    )

        # 路由装饰器：@router.get("/path") / @app.get("/path") 等
        # 装饰器在 AST 中是 Call 节点的外层，这里检查 router/app 方法的
        # 第一个字符串参数是否含排名路径
        if isinstance(func, ast.Attribute) and func.attr in (
            "get", "post", "put", "delete", "patch", "head", "options",
            "api_route", "websocket",
        ):
            for arg in node.args:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    if _RE_ROUTE_RANK.search(arg.value):
                        self._add(
                            node.lineno,
                            "route_rank_decorator",
                            f"@{func.attr}({arg.value!r})",
                        )

        self.generic_visit(node)

    # ── 函数定义：检查排名语义函数名 ──

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._check_func_name(node.name, node.lineno)
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._check_func_name(node.name, node.lineno)
        self.generic_visit(node)

    def _check_func_name(self, name: str, lineno: int) -> None:
        """检查函数名是否含排名语义.

        为什么检查函数名：即使 SQL/ORM 未直接出现排名模式，名为
        get_ranking / compute_leaderboard 的函数即表明存在排名查询路径。
        """
        if _RE_FUNC_NAME_RANK.match(name):
            self._add(lineno, "func_name_rank", f"def {name}(...):")


# ────────────────────────────────────────────────────────────────────
# 对外 API
# ────────────────────────────────────────────────────────────────────

def scan_file(filepath: Path, base: Path | None = None) -> list[Violation]:
    """扫描单个 .py 文件，返回违规列表.

    Args:
        filepath: 文件绝对/相对路径.
        base: 项目根目录（用于计算相对路径显示）；None 则用 filepath 自身.
    """
    rel = str(filepath.relative_to(base)) if base else str(filepath)
    try:
        source = filepath.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    try:
        tree = ast.parse(source, filename=rel)
    except SyntaxError:
        return []

    scanner = _RankingScanner(rel, source)
    scanner._collect_docstrings(tree)
    scanner.visit(tree)
    return scanner.violations


def scan_directory(src_dir: Path) -> list[Violation]:
    """递归扫描目录下全部 .py 文件，返回违规列表.

    Args:
        src_dir: 要扫描的根目录（如项目 src/）.
    """
    base = src_dir.parent if src_dir.name == "src" else src_dir
    violations: list[Violation] = []
    for pyfile in sorted(src_dir.rglob("*.py")):
        violations.extend(scan_file(pyfile, base=base))
    return violations


# ────────────────────────────────────────────────────────────────────
# CLI
# ────────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    """CLI 入口：扫描 src/，有违规返回 1（CI 阻断），无违规返回 0.

    用法：python scripts/ci/check_no_ranking.py [src_dir]
    """
    args = argv if argv is not None else sys.argv[1:]
    src_dir = Path(args[0]) if args else Path("src")

    if not src_dir.is_dir():
        print(f"❌ 扫描目录不存在: {src_dir}", file=sys.stderr)
        return 2

    violations = scan_directory(src_dir)

    if not violations:
        print(f"✅ {src_dir}/ 未发现跨用户排名查询路径（D8 实证通过）")
        return 0

    print(
        f"❌ 发现 {len(violations)} 处跨用户排名查询路径（违反 D8）：",
        file=sys.stderr,
    )
    for v in violations:
        print(f"  {v}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
