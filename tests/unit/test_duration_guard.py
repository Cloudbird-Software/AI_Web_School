"""T-W4-034 时长保护触发单元测试.

覆盖验收标准：
2. ``DurationGuard.check(session, grade_band)`` 在超时时返回 False，阻止取下一题。
3. 学段阈值正确：L≤15min / M≤60min / H≤60min；可配置。
4. ``make accept TASK=T-W4-034`` 全绿。
5. 不 import 任何学科包/学段包。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from src.core.session.duration_guard import (
    DEFAULT_TIME_LIMIT_SEC,
    DurationGuard,
    InvalidGradeBandError,
    VALID_GRADEBANDS,
)


# ────────────────────────────────────────────────────────────────────
# fixture
# ────────────────────────────────────────────────────────────────────

_T0 = datetime(2026, 7, 28, 10, 0, 0, tzinfo=timezone.utc)


def _make_session(
    *,
    last_resume_at: datetime | None = None,
    started_at: datetime | None = None,
) -> SimpleNamespace:
    """构造测试用会话 stub（duck typing，只需 last_resume_at / started_at）."""
    return SimpleNamespace(
        last_resume_at=last_resume_at or _T0,
        started_at=started_at or _T0,
    )


# ────────────────────────────────────────────────────────────────────
# 1. 学段阈值正确（验收 3：L≤15min / M≤60min / H≤60min）
# ────────────────────────────────────────────────────────────────────

class TestGradeBandThresholds:
    """学段时长保护阈值与 ADR §4.8 一致."""

    def test_default_thresholds(self) -> None:
        """默认阈值：L=900s(15min) / M=3600s(60min) / H=3600s(60min)."""
        assert DEFAULT_TIME_LIMIT_SEC["L"] == 15 * 60
        assert DEFAULT_TIME_LIMIT_SEC["M"] == 60 * 60
        assert DEFAULT_TIME_LIMIT_SEC["H"] == 60 * 60

    def test_guard_default_thresholds(self) -> None:
        """DurationGuard 默认使用 DEFAULT_TIME_LIMIT_SEC."""
        guard = DurationGuard()
        assert guard.thresholds == DEFAULT_TIME_LIMIT_SEC

    def test_valid_gradebands(self) -> None:
        """合法学段标识为 L/M/H."""
        assert VALID_GRADEBANDS == frozenset({"L", "M", "H"})

    def test_get_threshold_l(self) -> None:
        guard = DurationGuard()
        assert guard.get_threshold("L") == 900

    def test_get_threshold_m(self) -> None:
        guard = DurationGuard()
        assert guard.get_threshold("M") == 3600

    def test_get_threshold_h(self) -> None:
        guard = DurationGuard()
        assert guard.get_threshold("H") == 3600


# ────────────────────────────────────────────────────────────────────
# 2. check() 判定（验收 2：超时返回 False）
# ────────────────────────────────────────────────────────────────────

class TestCheckWithinLimit:
    """未超时 check 返回 True."""

    def test_within_limit_l(self) -> None:
        """L 学段 14 分钟未超时."""
        session = _make_session(last_resume_at=_T0)
        guard = DurationGuard()
        now = _T0 + timedelta(minutes=14)
        assert guard.check(session, "L", now=now) is True

    def test_within_limit_m(self) -> None:
        """M 学段 59 分钟未超时."""
        session = _make_session(last_resume_at=_T0)
        guard = DurationGuard()
        now = _T0 + timedelta(minutes=59)
        assert guard.check(session, "M", now=now) is True

    def test_within_limit_h(self) -> None:
        """H 学段 59 分钟未超时."""
        session = _make_session(last_resume_at=_T0)
        guard = DurationGuard()
        now = _T0 + timedelta(minutes=59)
        assert guard.check(session, "H", now=now) is True

    def test_zero_elapsed(self) -> None:
        """刚开始（0 秒）未超时."""
        session = _make_session(last_resume_at=_T0)
        guard = DurationGuard()
        assert guard.check(session, "L", now=_T0) is True


class TestCheckOvertime:
    """超时 check 返回 False（阻止取下一题）."""

    def test_overtime_l(self) -> None:
        """L 学段 16 分钟超时."""
        session = _make_session(last_resume_at=_T0)
        guard = DurationGuard()
        now = _T0 + timedelta(minutes=16)
        assert guard.check(session, "L", now=now) is False

    def test_overtime_m(self) -> None:
        """M 学段 61 分钟超时."""
        session = _make_session(last_resume_at=_T0)
        guard = DurationGuard()
        now = _T0 + timedelta(minutes=61)
        assert guard.check(session, "M", now=now) is False

    def test_overtime_h(self) -> None:
        """H 学段 61 分钟超时."""
        session = _make_session(last_resume_at=_T0)
        guard = DurationGuard()
        now = _T0 + timedelta(minutes=61)
        assert guard.check(session, "H", now=now) is False

    def test_overtime_long_elapsed(self) -> None:
        """超时很久仍返回 False."""
        session = _make_session(last_resume_at=_T0)
        guard = DurationGuard()
        now = _T0 + timedelta(hours=3)
        assert guard.check(session, "L", now=now) is False


class TestCheckBoundary:
    """边界值：恰好等于阈值."""

    def test_exact_threshold_l(self) -> None:
        """L 学段恰好 15 分钟：≤阈值=允许（True）."""
        session = _make_session(last_resume_at=_T0)
        guard = DurationGuard()
        now = _T0 + timedelta(minutes=15)
        assert guard.check(session, "L", now=now) is True

    def test_exact_threshold_m(self) -> None:
        """M 学段恰好 60 分钟：≤阈值=允许（True）."""
        session = _make_session(last_resume_at=_T0)
        guard = DurationGuard()
        now = _T0 + timedelta(minutes=60)
        assert guard.check(session, "M", now=now) is True

    def test_one_second_over_l(self) -> None:
        """L 学段 15 分 1 秒：超时（False）."""
        session = _make_session(last_resume_at=_T0)
        guard = DurationGuard()
        now = _T0 + timedelta(minutes=15, seconds=1)
        assert guard.check(session, "L", now=now) is False


# ────────────────────────────────────────────────────────────────────
# 3. 可配置阈值（验收 3：可配置）
# ────────────────────────────────────────────────────────────────────

class TestConfigurableThresholds:
    """自定义阈值生效."""

    def test_custom_thresholds(self) -> None:
        """自定义阈值生效."""
        guard = DurationGuard(thresholds={"L": 300, "M": 1800, "H": 1800})
        assert guard.get_threshold("L") == 300
        assert guard.get_threshold("M") == 1800
        session = _make_session(last_resume_at=_T0)
        # 6 分钟 > 5 分钟自定义阈值 → 超时
        assert guard.check(session, "L", now=_T0 + timedelta(minutes=6)) is False
        # 4 分钟 < 5 分钟 → 未超时
        assert guard.check(session, "L", now=_T0 + timedelta(minutes=4)) is True

    def test_custom_thresholds_partial_raises(self) -> None:
        """自定义阈值缺少学段抛 InvalidGradeBandError."""
        with pytest.raises(InvalidGradeBandError):
            DurationGuard(thresholds={"L": 300, "M": 1800})  # 缺 H

    def test_custom_thresholds_non_positive_raises(self) -> None:
        """阈值为非正整数抛 ValueError."""
        with pytest.raises(ValueError):
            DurationGuard(thresholds={"L": 0, "M": 1800, "H": 1800})

    def test_thresholds_property_is_copy(self) -> None:
        """thresholds 属性返回副本，修改不影响内部状态."""
        guard = DurationGuard()
        t = guard.thresholds
        t["L"] = 999999
        assert guard.get_threshold("L") == 900  # 内部不变


# ────────────────────────────────────────────────────────────────────
# 4. 计时锚点（last_resume_at）
# ────────────────────────────────────────────────────────────────────

class TestTimeAnchor:
    """时长保护计时锚点为 last_resume_at."""

    def test_resume_resets_timer(self) -> None:
        """休息确认后 last_resume_at 重置，时长重新计算."""
        guard = DurationGuard()
        # 第一次开始：10:00
        session = _make_session(last_resume_at=_T0)
        # 14 分钟后（未超时）
        assert guard.check(session, "L", now=_T0 + timedelta(minutes=14)) is True

        # 休息确认：重置锚点到 10:15
        session.last_resume_at = _T0 + timedelta(minutes=15)
        # 从 10:15 起 14 分钟 = 10:29（未超时）
        assert guard.check(
            session, "L", now=_T0 + timedelta(minutes=29)
        ) is True
        # 从 10:15 起 16 分钟 = 10:31（超时）
        assert guard.check(
            session, "L", now=_T0 + timedelta(minutes=31)
        ) is False

    def test_fallback_to_started_at(self) -> None:
        """无 last_resume_at 时回退到 started_at."""
        guard = DurationGuard()
        session = SimpleNamespace(
            last_resume_at=None,
            started_at=_T0,
        )
        # 14 分钟 < 15 分钟 → 未超时
        assert guard.check(session, "L", now=_T0 + timedelta(minutes=14)) is True
        # 16 分钟 > 15 分钟 → 超时
        assert guard.check(session, "L", now=_T0 + timedelta(minutes=16)) is False

    def test_no_anchor_raises(self) -> None:
        """既无 last_resume_at 也无 started_at 抛 AttributeError."""
        guard = DurationGuard()
        session = SimpleNamespace()
        with pytest.raises(AttributeError):
            guard.check(session, "L", now=_T0)


# ────────────────────────────────────────────────────────────────────
# 5. remaining_sec
# ────────────────────────────────────────────────────────────────────

class TestRemainingSec:
    """remaining_sec 返回距超时的剩余秒数."""

    def test_remaining_within_limit(self) -> None:
        """未超时时返回正剩余."""
        guard = DurationGuard()
        session = _make_session(last_resume_at=_T0)
        # 10 分钟后，L 阈值 15 分钟 → 剩余 5 分钟 = 300 秒
        remaining = guard.remaining_sec(session, "L", now=_T0 + timedelta(minutes=10))
        assert remaining == 300

    def test_remaining_overtime_negative(self) -> None:
        """超时时返回负剩余."""
        guard = DurationGuard()
        session = _make_session(last_resume_at=_T0)
        # 16 分钟后，L 阈值 15 分钟 → 超时 1 分钟 = -60 秒
        remaining = guard.remaining_sec(session, "L", now=_T0 + timedelta(minutes=16))
        assert remaining == -60

    def test_remaining_at_start(self) -> None:
        """刚开始时剩余=全阈值."""
        guard = DurationGuard()
        session = _make_session(last_resume_at=_T0)
        remaining = guard.remaining_sec(session, "M", now=_T0)
        assert remaining == 3600


# ────────────────────────────────────────────────────────────────────
# 6. 非法学段
# ────────────────────────────────────────────────────────────────────

class TestInvalidGradeBand:
    """非法学段标识抛 InvalidGradeBandError."""

    def test_check_invalid_gradeband(self) -> None:
        """check 非法学段抛错."""
        guard = DurationGuard()
        session = _make_session()
        with pytest.raises(InvalidGradeBandError):
            guard.check(session, "X", now=_T0)

    def test_get_threshold_invalid_gradeband(self) -> None:
        """get_threshold 非法学段抛错."""
        guard = DurationGuard()
        with pytest.raises(InvalidGradeBandError):
            guard.get_threshold("low")

    def test_remaining_invalid_gradeband(self) -> None:
        """remaining_sec 非法学段抛错."""
        guard = DurationGuard()
        session = _make_session()
        with pytest.raises(InvalidGradeBandError):
            guard.remaining_sec(session, "Z", now=_T0)


# ────────────────────────────────────────────────────────────────────
# 7. 学科包隔离（X6）
# ────────────────────────────────────────────────────────────────────

class TestNoSubjectPackImport:
    """session/duration_guard 不 import 任何学科包/学段包（宪法 A5/X6）."""

    def test_duration_guard_module_no_subject_pack(self) -> None:
        """duration_guard 模块不 import 任何学科包/学段包.

        为什么用 AST 而非子串匹配：duration_guard 以 "L"/"M"/"H" 字符串
        承载学段维度（VALID_GRADEBANDS / grade_band 参数），这些是合法的
        学段概念引用而非学段包导入；子串匹配 "gradeband" 会误报。
        AST 检查 import 语句可精确判定是否引入了外部包。
        """
        import ast
        import inspect
        from src.core.session import duration_guard
        source = inspect.getsource(duration_guard)
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert not alias.name.startswith("src.packs"), (
                        f"duration_guard 不得 import 学科包（X6）：{alias.name}"
                    )
            elif isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                assert not mod.startswith("src.packs"), (
                    f"duration_guard 不得 from 学科包（X6）：{mod}"
                )
