"""T-W4-033 单元测试：跨用户排名查询静态扫描器.

覆盖验收标准：
1. ``check_no_ranking.py`` 扫描 src/ 全部代码，发现 ``ORDER BY score`` /
   ``rank`` / ``RANK()`` 跨用户查询即报错。
2. 扫描范围覆盖 SQL 字符串、ORM 查询、API 端点定义、报告生成代码。
3. 不引入假阳性：合法的单用户内排序（如个人历史成绩时间序）不被误报。
4. ``make accept TASK=T-W4-033`` 全绿。

测试策略：
- 用 ``scan_source`` 内联扫描代码片段，验证检测精度与假阳性控制。
- 不依赖 DB / 网络；纯静态分析。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

# ────────────────────────────────────────────────────────────────────
# 加载扫描器（scripts/ci/ 非包成员，用 importlib 加载）
# ────────────────────────────────────────────────────────────────────

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_SCANNER_PATH = _PROJECT_ROOT / "scripts" / "ci" / "check_no_ranking.py"


@pytest.fixture(scope="module")
def scanner() -> object:
    """importlib 加载 check_no_ranking.py 为模块."""
    spec = importlib.util.spec_from_file_location(
        "_check_no_ranking_test", _SCANNER_PATH
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_check_no_ranking_test"] = mod
    spec.loader.exec_module(mod)
    return mod


def _scan_source(scanner: object, source: str, name: str = "<test>") -> list:
    """扫描内联源码片段，返回违规列表.

    写入临时 .py 文件 → scan_file → 读取违规 → 删除。
    """
    import tempfile

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".py", delete=False, encoding="utf-8"
    ) as f:
        f.write(source)
        f.flush()
        path = Path(f.name)
    try:
        return scanner.scan_file(path, base=path.parent)
    finally:
        path.unlink(missing_ok=True)


# ────────────────────────────────────────────────────────────────────
# 1. SQL 排名模式检测
# ────────────────────────────────────────────────────────────────────

class TestSqlRankingDetection:
    """SQL 字符串含排名模式即报违规."""

    def test_order_by_score_detected(self, scanner: object) -> None:
        """ORDER BY score 被检测."""
        source = '''
def bad_query():
    sql = "SELECT student_id, score FROM results ORDER BY score DESC"
    return sql
'''
        violations = _scan_source(scanner, source)
        cats = [v.category for v in violations]
        assert "sql_order_by_score" in cats

    def test_order_by_total_score_detected(self, scanner: object) -> None:
        """ORDER BY total_score 被检测."""
        source = '''
def bad():
    sql = "SELECT * FROM exam ORDER BY total_score DESC LIMIT 10"
'''
        violations = _scan_source(scanner, source)
        cats = [v.category for v in violations]
        assert "sql_order_by_score" in cats

    def test_rank_window_function_detected(self, scanner: object) -> None:
        """RANK() 窗口函数被检测."""
        source = '''
def bad():
    sql = "SELECT student_id, RANK() OVER (ORDER BY score) AS rnk FROM results"
'''
        violations = _scan_source(scanner, source)
        cats = [v.category for v in violations]
        assert "sql_window_rank" in cats

    def test_dense_rank_detected(self, scanner: object) -> None:
        """DENSE_RANK() 被检测."""
        source = '''
def bad():
    sql = "SELECT *, DENSE_RANK() OVER (ORDER BY points) FROM scores"
'''
        violations = _scan_source(scanner, source)
        cats = [v.category for v in violations]
        assert "sql_window_rank" in cats

    def test_row_number_over_score_detected(self, scanner: object) -> None:
        """ROW_NUMBER() OVER(... score ...) 被检测."""
        source = '''
def bad():
    sql = (
        "SELECT *, ROW_NUMBER() OVER (ORDER BY total_score DESC) "
        "FROM exam_results"
    )
'''
        violations = _scan_source(scanner, source)
        cats = [v.category for v in violations]
        assert "sql_rownum_over_score" in cats

    def test_rank_column_in_select_detected(self, scanner: object) -> None:
        """SELECT rank 列被检测."""
        source = '''
def bad():
    sql = "SELECT student_id, rank FROM class_ranking"
'''
        violations = _scan_source(scanner, source)
        cats = [v.category for v in violations]
        assert "sql_rank_column" in cats


# ────────────────────────────────────────────────────────────────────
# 2. ORM 排名模式检测
# ────────────────────────────────────────────────────────────────────

class TestOrmRankingDetection:
    """ORM .order_by(score) 被检测."""

    def test_orm_order_by_score_column(self, scanner: object) -> None:
        """.order_by(StudentScore.total_score) 被检测."""
        source = '''
def bad(db):
    return await db.execute(
        select(Student).order_by(Student.score.desc())
    )
'''
        violations = _scan_source(scanner, source)
        cats = [v.category for v in violations]
        assert "orm_order_by_score" in cats

    def test_orm_order_by_accuracy(self, scanner: object) -> None:
        """.order_by(ExamResult.accuracy) 被检测."""
        source = '''
def bad(db):
    stmt = select(ExamResult).order_by(ExamResult.accuracy.desc())
    return stmt
'''
        violations = _scan_source(scanner, source)
        cats = [v.category for v in violations]
        assert "orm_order_by_score" in cats


# ────────────────────────────────────────────────────────────────────
# 3. API 端点排名检测
# ────────────────────────────────────────────────────────────────────

class TestApiRankingDetection:
    """排名 API 路由被检测."""

    def test_route_get_ranking(self, scanner: object) -> None:
        """@router.get('/rankings') 被检测."""
        source = '''
from fastapi import APIRouter
router = APIRouter()

@router.get("/rankings")
async def get_rankings():
    pass
'''
        violations = _scan_source(scanner, source)
        cats = [v.category for v in violations]
        assert "route_rank_decorator" in cats or "route_rank_path" in cats

    def test_route_leaderboard(self, scanner: object) -> None:
        """@router.get('/leaderboard') 被检测."""
        source = '''
from fastapi import APIRouter
router = APIRouter()

@router.get("/leaderboard")
async def leaderboard():
    pass
'''
        violations = _scan_source(scanner, source)
        cats = [v.category for v in violations]
        assert "route_rank_decorator" in cats or "route_rank_path" in cats


# ────────────────────────────────────────────────────────────────────
# 4. 函数名排名检测
# ────────────────────────────────────────────────────────────────────

class TestFunctionNameRankingDetection:
    """排名语义函数名被检测."""

    def test_func_get_ranking(self, scanner: object) -> None:
        """def get_ranking(...) 被检测."""
        source = '''
async def get_ranking(db):
    pass
'''
        violations = _scan_source(scanner, source)
        cats = [v.category for v in violations]
        assert "func_name_rank" in cats

    def test_func_compute_leaderboard(self, scanner: object) -> None:
        """def compute_leaderboard(...) 被检测."""
        source = '''
def compute_leaderboard(students):
    pass
'''
        violations = _scan_source(scanner, source)
        cats = [v.category for v in violations]
        assert "func_name_rank" in cats


# ────────────────────────────────────────────────────────────────────
# 5. 假阳性控制（验收 5：合法单用户排序不误报）
# ────────────────────────────────────────────────────────────────────

class TestNoFalsePositive:
    """合法的单用户内排序 / 非成绩排序不被误报（验收 5）."""

    def test_order_by_created_at_safe(self, scanner: object) -> None:
        """ORDER BY created_at（个人历史时间序）不报."""
        source = '''
def get_history(db, student_id):
    sql = (
        "SELECT * FROM response_event "
        "WHERE student_alias_id = :sid "
        "ORDER BY created_at DESC"
    )
    return await db.execute(text(sql), {"sid": student_id})
'''
        violations = _scan_source(scanner, source)
        assert violations == [], f"误报: {violations}"

    def test_order_by_item_number_safe(self, scanner: object) -> None:
        """ORDER BY item_number（卷内题序）不报."""
        source = '''
def get_paper_items(db, paper_id):
    sql = "SELECT * FROM paper_item WHERE paper_id = :pid ORDER BY item_number"
    return await db.execute(text(sql), {"pid": paper_id})
'''
        violations = _scan_source(scanner, source)
        assert violations == [], f"误报: {violations}"

    def test_order_by_version_safe(self, scanner: object) -> None:
        """ORDER BY version DESC（版本序）不报."""
        source = '''
def get_versions(db, sid):
    sql = "SELECT * FROM parental_consent WHERE student_alias_id = :sid ORDER BY version DESC"
    return await db.execute(text(sql), {"sid": sid})
'''
        violations = _scan_source(scanner, source)
        assert violations == [], f"误报: {violations}"

    def test_orm_order_by_created_at_safe(self, scanner: object) -> None:
        """.order_by(Item.created_at.desc()) 不报."""
        source = '''
def list_items(db):
    stmt = select(Item).order_by(Item.created_at.desc()).limit(100)
    return stmt
'''
        violations = _scan_source(scanner, source)
        assert violations == [], f"误报: {violations}"

    def test_orm_order_by_due_at_safe(self, scanner: object) -> None:
        """.order_by(ReviewQueueEntry.due_at) 不报."""
        source = '''
def get_due(db):
    stmt = select(ReviewQueueEntry).order_by(
        ReviewQueueEntry.due_at, ReviewQueueEntry.entry_id
    )
    return stmt
'''
        violations = _scan_source(scanner, source)
        assert violations == [], f"误报: {violations}"

    def test_docstring_mention_rank_safe(self, scanner: object) -> None:
        """docstring 中提及「排名」「rank」作为禁令说明不报."""
        source = '''"""合规层：不提供跨用户排名查询路径（D8）.

代码层不得提供排名功能；rank 查询路径不存在。
"""


def check_consent(db):
    """检查授权（不涉及 rank / ranking）."""
    return True
'''
        violations = _scan_source(scanner, source)
        assert violations == [], f"误报: {violations}"

    def test_comment_mention_rank_safe(self, scanner: object) -> None:
        """注释中提及排名不报（AST 不解析注释）."""
        source = '''
def get_report(db, student_id):
    # 注意：不提供跨用户排名（D8），只返回个人报告
    # rank / ranking / leaderboard 均禁止
    sql = "SELECT * FROM report WHERE student_alias_id = :sid ORDER BY created_at"
    return await db.execute(text(sql), {"sid": student_id})
'''
        violations = _scan_source(scanner, source)
        assert violations == [], f"误报: {violations}"

    def test_variable_named_score_safe(self, scanner: object) -> None:
        """变量名含 score 但非查询排序不报."""
        source = '''
def calculate_score(correct: int, total: int) -> float:
    score = correct / total
    return score
'''
        violations = _scan_source(scanner, source)
        assert violations == [], f"误报: {violations}"


# ────────────────────────────────────────────────────────────────────
# 6. 学科包隔离（X6）
# ────────────────────────────────────────────────────────────────────

class TestNoSubjectPackImport:
    """合规层扫描器不 import 任何学科包/学段包（宪法 A5/X6）."""

    def test_scanner_no_subject_pack(self, scanner: object) -> None:
        """扫描器源码不引用任何学科包."""
        source = _SCANNER_PATH.read_text(encoding="utf-8")
        forbidden = (
            "src.packs",
            "subject_math",
            "subject_chinese",
            "subject_english",
            "gradeband",
            "subject-math",
            "subject-chinese",
            "subject-english",
        )
        for token in forbidden:
            assert token not in source, (
                f"扫描器不得引用学科包/学段包（X6），发现 {token!r}"
            )
