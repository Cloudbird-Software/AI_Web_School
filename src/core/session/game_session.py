"""T-W4-036 低段闯关形态会话状态机（架构 v2 §5.3）.

闯关形态（game session）= 即时反馈 + 星级评定 + 关卡推进语义。本模块实现
纯内存状态机：未开始 → 进行中 → 完成（含 1–3 星评定）。

设计要点：
- **纯状态机，不持久化**：运行态进度由 PracticeSession ORM 表承载
  （src/core/session/models.py），本模块只表达闯关形态的关卡语义，
  便于组卷/会话编排层组合使用。
- **即时反馈**：每次 submit_answer 立即返回对错 + 运行正确率 + 星级预览，
  满足低段「即时反馈」要求（架构 §5.3）。
- **确定性星级**：finish 时按整体正确率与阈值算 1–3 星；同结果必同星
  （纯函数 compute_stars）。

宪法 A5：本模块不 import 任何学科包/学段包；星级阈值是核心域默认，
学段包可通过 GameSession(star_thresholds=...) 注入覆盖（不感知包文件位置）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

# 默认星级阈值 (two_star_floor, three_star_floor)：
# 正确率 ≥ three_star_floor → 3 星；≥ two_star_floor → 2 星；否则 1 星。
# 与低学段包 config.yaml::game.star_thresholds 同值（核心不 import 学段包，
# 此处为核心域默认；学段包可经 star_thresholds 参数注入覆盖）。
DEFAULT_STAR_THRESHOLDS: tuple[float, float] = (0.6, 0.9)


class GameStatus(str, Enum):
    """闯关会话状态机三态."""

    NOT_STARTED = "not_started"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"


def compute_stars(
    correct_rate: float,
    thresholds: tuple[float, float] = DEFAULT_STAR_THRESHOLDS,
) -> int:
    """纯函数：按正确率与阈值计算星级（1–3 星）.

    Args:
        correct_rate: 正确率 [0.0, 1.0]。
        thresholds: (2 星下限, 3 星下限)。

    Returns:
        1 / 2 / 3 星。正确率 ≥ 3 星下限 → 3；≥ 2 星下限 → 2；否则 1。
    """
    two_star, three_star = thresholds
    if correct_rate >= three_star:
        return 3
    if correct_rate >= two_star:
        return 2
    return 1


@dataclass
class GameSession:
    """闯关会话状态机（未开始 → 进行中 → 完成 + 星级）.

    用法::

        game = GameSession(total_items=8)
        game.start()
        for item_id, correct in answers:
            fb = game.submit_answer(item_id=item_id, correct=correct)
        result = game.finish()  # 含 stars / correct_rate

    Attributes:
        total_items: 本关题量（>0）。
        star_thresholds: (2 星下限, 3 星下限)。
        status: 当前状态。
        answered / correct: 已作答数 / 答对数。
        stars: 完成后的星级评定（未完成时为 None）。
        feedback_log: 每次即时反馈的记录（审计/回放用）。
    """

    total_items: int
    star_thresholds: tuple[float, float] = DEFAULT_STAR_THRESHOLDS
    status: GameStatus = GameStatus.NOT_STARTED
    answered: int = 0
    correct: int = 0
    stars: Optional[int] = None
    feedback_log: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.total_items <= 0:
            raise ValueError(f"total_items 必须 >0，实际 {self.total_items}")
        if (
            len(self.star_thresholds) != 2
            or not (0.0 <= self.star_thresholds[0] <= self.star_thresholds[1] <= 1.0)
        ):
            raise ValueError(
                f"star_thresholds 必须为 (a, b) 且 0≤a≤b≤1，实际 {self.star_thresholds}"
            )

    # ── 状态迁移 ────────────────────────────────────────────────

    def start(self) -> None:
        """开始闯关：NOT_STARTED → IN_PROGRESS."""
        if self.status != GameStatus.NOT_STARTED:
            raise GameSessionStateError(
                f"仅未开始的会话可 start，当前状态 {self.status.value}"
            )
        self.status = GameStatus.IN_PROGRESS

    def submit_answer(
        self,
        *,
        item_id: str,
        correct: bool,
        feedback: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """提交单题作答，返回即时反馈（对错 + 运行正确率 + 星级预览）.

        即时反馈（架构 §5.3）：低段闯关形态要求每题作答后立即出示对错与星级进度。

        Args:
            item_id: 题目标识。
            correct: 是否答对。
            feedback: 附加反馈（解析等），原样附在返回里。

        Returns:
            即时反馈 dict：{item_id, correct, immediate=True,
            running_correct_rate, stars_preview, feedback}。

        Raises:
            GameSessionStateError: 非 IN_PROGRESS 状态提交。
        """
        if self.status != GameStatus.IN_PROGRESS:
            raise GameSessionStateError(
                f"仅进行中的会话可提交作答，当前状态 {self.status.value}"
            )
        self.answered += 1
        if correct:
            self.correct += 1
        rate = self.correct / self.answered
        entry: dict[str, Any] = {
            "item_id": item_id,
            "correct": correct,
            "immediate": True,
            "running_correct_rate": round(rate, 4),
            "stars_preview": compute_stars(rate, self.star_thresholds),
            "feedback": feedback,
        }
        self.feedback_log.append(entry)
        return entry

    def finish(self) -> dict[str, Any]:
        """结束闯关：IN_PROGRESS → COMPLETED，评定星级.

        Returns:
            完成结果 dict：{status, stars, correct_rate, correct, total}。

        Raises:
            GameSessionStateError: 非 IN_PROGRESS 状态结束；或未作答完所有题
                （闯关形态要求走完全部题才评定星级）。
        """
        if self.status != GameStatus.IN_PROGRESS:
            raise GameSessionStateError(
                f"仅进行中的会话可 finish，当前状态 {self.status.value}"
            )
        if self.answered != self.total_items:
            raise GameSessionStateError(
                f"闯关未走完全部题：已作答 {self.answered}/{self.total_items}，"
                "不可评定星级"
            )
        self.status = GameStatus.COMPLETED
        rate = self.correct / self.total_items
        self.stars = compute_stars(rate, self.star_thresholds)
        return {
            "status": GameStatus.COMPLETED.value,
            "stars": self.stars,
            "correct_rate": round(rate, 4),
            "correct": self.correct,
            "total": self.total_items,
        }


class GameSessionStateError(ValueError):
    """闯关会话状态不允许当前操作."""


__all__ = [
    "DEFAULT_STAR_THRESHOLDS",
    "GameStatus",
    "GameSession",
    "GameSessionStateError",
    "compute_stars",
]
