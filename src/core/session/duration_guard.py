"""T-W4-034 时长保护触发（宪法 A6 / ADR §4.8）.

落地 ADR §4.8「会话层内置时长与用眼保护：低段≤15 分钟、3–6 年级≤60 分钟
建议阈值」与宪法 A6「学段是一等维度——时长/合规差异须有类型化承载」.

设计要点：
- ``DurationGuard`` 是**纯策略闸门**：输入会话状态 + 学段，输出是否允许继续取题。
- 阈值按学段 (L/M/H) 配置，默认与 ADR §4.8 一致（L=15min / M=60min / H=60min）。
- ``check()`` 返回 bool：True=未超时可继续，False=已超时须阻断取题。
- 计时锚点为 ``session.last_resume_at``（开始或上次休息确认时刻），
  与 session/service.py 的时长保护语义同源。
- clock 注入（``now`` 参数）：测试不依赖 sleep。

与 session/service.py 的关系：
- session/service.py 的 ``_check_time_protection`` 在超时时抛
  ``RestRequiredError``（内置 REST 提示语义）；
- ``DurationGuard`` 是可复用的纯判定器（返回 bool），供会话服务、
  API 中间件、测试等场景独立调用——不耦合 ORM 事务/状态机。
- 两者共享同一阈值常量语义（L≤15 / M≤60 / H≤60 分钟）。

宪法 A5/X6：本模块不 import 任何学科包/学段包（学段以 "L"/"M"/"H" 字符串承载）。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional, Protocol


# ────────────────────────────────────────────────────────────────────
# 常量
# ────────────────────────────────────────────────────────────────────

# 学段时长保护阈值（秒）：ADR §4.8 建议值
#   L（低段 1–2 年级）≤ 15 分钟
#   M（中段 3–4 年级）≤ 60 分钟
#   H（高段 5–6 年级）≤ 60 分钟
DEFAULT_TIME_LIMIT_SEC: dict[str, int] = {
    "L": 15 * 60,
    "M": 60 * 60,
    "H": 60 * 60,
}

# 合法学段标识
VALID_GRADEBANDS = frozenset(DEFAULT_TIME_LIMIT_SEC.keys())


# ────────────────────────────────────────────────────────────────────
# 协议（duck typing：会话对象只需有 last_resume_at）
# ────────────────────────────────────────────────────────────────────

class _SessionLike(Protocol):
    """时长保护所需的最小会话接口（duck typing）.

    任何具有 ``last_resume_at`` 属性的对象均可传入 DurationGuard.check，
    无需继承本 Protocol——运行时按 duck typing 匹配。
    """

    last_resume_at: datetime


# ────────────────────────────────────────────────────────────────────
# 异常
# ────────────────────────────────────────────────────────────────────

class InvalidGradeBandError(ValueError):
    """学段标识非法（非 L/M/H）."""


# ────────────────────────────────────────────────────────────────────
# DurationGuard
# ────────────────────────────────────────────────────────────────────

class DurationGuard:
    """时长保护闸门：按学段阈值判定会话是否超时.

    用法::

        guard = DurationGuard()
        if not guard.check(session, "L"):
            # 超时，阻止取下一题
            raise RestRequiredError(...)

    阈值可配置::

        guard = DurationGuard(thresholds={"L": 600, "M": 3600, "H": 3600})
    """

    def __init__(
        self,
        thresholds: Optional[dict[str, int]] = None,
    ) -> None:
        """初始化时长保护闸门.

        Args:
            thresholds: 自定义学段阈值（秒），key ∈ {"L","M","H"}；
                None 用 DEFAULT_TIME_LIMIT_SEC。
        """
        self._thresholds: dict[str, int] = dict(
            thresholds if thresholds is not None else DEFAULT_TIME_LIMIT_SEC
        )
        # 校验：自定义阈值必须覆盖全部学段且为正整数
        for gb in VALID_GRADEBANDS:
            if gb not in self._thresholds:
                raise InvalidGradeBandError(
                    f"thresholds 缺少学段 {gb!r}（须含 L/M/H 三键）"
                )
            if not isinstance(self._thresholds[gb], int) or self._thresholds[gb] <= 0:
                raise ValueError(
                    f"thresholds[{gb!r}]={self._thresholds[gb]!r} 须为正整数"
                )

    @property
    def thresholds(self) -> dict[str, int]:
        """当前阈值（只读副本）."""
        return dict(self._thresholds)

    def get_threshold(self, grade_band: str) -> int:
        """取指定学段的时长阈值（秒）.

        Raises:
            InvalidGradeBandError: 学段非 L/M/H。
        """
        self._validate_gradeband(grade_band)
        return self._thresholds[grade_band]

    def check(
        self,
        session: _SessionLike,
        grade_band: str,
        *,
        now: Optional[datetime] = None,
    ) -> bool:
        """判定会话是否在时长保护范围内.

        Args:
            session: 会话对象（须有 ``last_resume_at`` datetime 属性）。
            grade_band: 学段标识（"L"/"M"/"H"）。
            now: 判定基准时刻（默认当前 UTC；测试注入）。

        Returns:
            True = 未超时可继续取题；False = 已超时须阻断。

        Raises:
            InvalidGradeBandError: 学段非 L/M/H。
        """
        threshold = self.get_threshold(grade_band)
        ts = now if now is not None else datetime.now(timezone.utc)
        anchor = _get_anchor(session)
        elapsed_sec = (ts - anchor).total_seconds()
        # ≤ 阈值=允许（边界值本身允许，与 session/service.py 语义一致：
        # _check_time_protection 用 elapsed > time_limit_sec 判超时）
        return elapsed_sec <= threshold

    def remaining_sec(
        self,
        session: _SessionLike,
        grade_band: str,
        *,
        now: Optional[datetime] = None,
    ) -> int:
        """距超时的剩余秒数（负值=已超时）.

        供 API 层展示「剩余作答时长」给前端。
        """
        threshold = self.get_threshold(grade_band)
        ts = now if now is not None else datetime.now(timezone.utc)
        anchor = _get_anchor(session)
        elapsed_sec = (ts - anchor).total_seconds()
        return int(threshold - elapsed_sec)

    @staticmethod
    def _validate_gradeband(grade_band: str) -> None:
        """校验学段标识合法."""
        if grade_band not in VALID_GRADEBANDS:
            raise InvalidGradeBandError(
                f"grade_band 必须 ∈ {sorted(VALID_GRADEBANDS)}，实际 {grade_band!r}"
            )


# ────────────────────────────────────────────────────────────────────
# 内部工具
# ────────────────────────────────────────────────────────────────────

def _get_anchor(session: Any) -> datetime:
    """取时长保护计时锚点：优先 last_resume_at，回退 started_at.

    为什么回退 started_at：某些轻量会话对象（如测试 stub）可能只有 started_at
    而无 last_resume_at；两者都是 datetime 即可计算时长。
    """
    anchor = getattr(session, "last_resume_at", None)
    if anchor is None:
        anchor = getattr(session, "started_at", None)
    if anchor is None:
        raise AttributeError(
            "session 须有 last_resume_at 或 started_at 属性（datetime）"
        )
    return anchor


__all__ = [
    "DEFAULT_TIME_LIMIT_SEC",
    "DurationGuard",
    "InvalidGradeBandError",
    "VALID_GRADEBANDS",
]
