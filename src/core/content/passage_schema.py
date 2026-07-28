"""C 线命题方向 schema 与校验（T-W4-012）.

架构 v2 §4.1 C 线：命题方向 = 知识点×体裁×难度×学段×学科，是 AI 起草语篇
（T-W4-013 generate_passage）的输入规约，也是语篇难度门（T-W4-014）比对的
目标基准。

本模块只定义 schema 与纯函数校验，不触 DB、不 import 学科包（A5/X6）：
- 知识点存在性：kp_refs 非空、code 非空（图谱存在性由调用方/学科包侧校验，
  本层只做结构校验，避免核心域引学科包）。
- 难度区间合法性：0.0<=min<=max<=1.0。
- 学段匹配性：grade_band ∈ {L,M,H}；低段（L）排除复杂社会议题体裁
  （argumentative/news_report），与 §4.3「适龄性」一致。

为什么校验返回错误列表而非抛异常：调用方（生成器/门）需要一次性收集全部
违规以反馈给教研或 AI 改写提示，异常只能传第一个错误。
"""
from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.core.models.item_version import KpRef
from src.core.models.passage import GENRE_VALUES, GRADE_BAND_VALUES


# 低段不适配的体裁（复杂社会议题/论辩，与 §4.3 适龄性一致）
_LOW_BAND_BLOCKED_GENRES: frozenset[str] = frozenset(
    {"argumentative", "news_report"}
)


class DifficultyTarget(BaseModel):
    """目标难度区间（0.0~1.0，用于 AI 起草与难度门比对）.

    语义：期望语篇难度指标落在 [min, max] 内（如 oov_rate、标准化难度）。
    min/max 均为 [0,1] 闭区间，min<=max 由 model_validator 强制。
    """

    model_config = ConfigDict(extra="forbid")

    min: float = Field(..., ge=0, le=1, description="难度下限")
    max: float = Field(..., ge=0, le=1, description="难度上限")

    @model_validator(mode="after")
    def _check_range(self) -> "DifficultyTarget":
        if self.min > self.max:
            raise ValueError(
                f"难度区间非法：min={self.min} > max={self.max}"
            )
        return self


class PromptDirection(BaseModel):
    """C 线命题方向（架构 v2 §4.1）.

    - kp_refs：知识点引用集（KpRef 结构，与 item_version.objective.kp_set 同构）。
    - genre：体裁（取值见 GENRE_VALUES）。
    - difficulty_target：目标难度区间（供难度门比对）。
    - grade_band：学段 L/M/H。
    - subject：学科 pack_id（如 'subject-chinese'/'subject-english'）。
    - word_count_target：字数区间 [lo, hi]（可选，供生成器控制篇幅）。
    """

    model_config = ConfigDict(extra="forbid")

    kp_refs: list[KpRef] = Field(..., min_length=1)
    genre: str
    difficulty_target: DifficultyTarget
    grade_band: Literal["L", "M", "H"]
    subject: str = Field(..., min_length=1)
    word_count_target: Optional[tuple[int, int]] = None

    @model_validator(mode="after")
    def _check_word_count(self) -> "PromptDirection":
        if self.word_count_target is not None:
            lo, hi = self.word_count_target
            if lo < 0 or hi < lo:
                raise ValueError(
                    f"字数区间非法：[{lo}, {hi}]（需 lo>=0 且 hi>=lo）"
                )
        return self


def validate_prompt_direction(direction: PromptDirection) -> list[str]:
    """校验命题方向，返回错误列表（空=通过）.

    三类校验（任务卡 T-W4-012 验收 #2）：
    1. 知识点存在性：kp_refs 非空、每条 code 非空。
    2. 难度区间合法性：min<=max（DifficultyTarget 已强制，此处冗余断言兜底）。
    3. 学段匹配性：grade_band 合法；低段排除 argumentative/news_report。

    为什么不校验知识点图谱存在性：图谱存在性需查 DB 且依赖学科包的图谱维度，
    核心域不 import 学科包（A5）；图谱校验由学科包侧验证器或调用方负责。

    Args:
        direction: 命题方向。

    Returns:
        错误信息列表（空列表表示通过）。
    """
    errors: list[str] = []

    # 1. 知识点存在性
    if not direction.kp_refs:
        errors.append("kp_refs 不能为空")
    for idx, kp in enumerate(direction.kp_refs):
        if not kp.code or not kp.code.strip():
            errors.append(f"kp_refs[{idx}].code 为空")

    # 2. 体裁合法
    if direction.genre not in GENRE_VALUES:
        errors.append(
            f"genre={direction.genre!r} 不在合法取值 {list(GENRE_VALUES)}"
        )

    # 3. 学段合法
    if direction.grade_band not in GRADE_BAND_VALUES:
        errors.append(
            f"grade_band={direction.grade_band!r} 不在 {list(GRADE_BAND_VALUES)}"
        )

    # 4. 学段×体裁匹配：低段排除复杂社会议题体裁（适龄性，§4.3）
    if (
        direction.grade_band == "L"
        and direction.genre in _LOW_BAND_BLOCKED_GENRES
    ):
        errors.append(
            f"低段(L)不适配体裁 {direction.genre!r}：低段不出现复杂社会议题"
        )

    return errors


def direction_to_prompt(direction: PromptDirection) -> str:
    """将命题方向渲染为 AI 起草 prompt 文本（供 T-W4-013 generate_passage）.

    本函数只做结构→文本的确定性映射，不含 PII（调用 AI 总线前由总线剥离）。
    保持纯函数：同 direction 必得同 prompt（可复现基础）。

    Args:
        direction: 命题方向。

    Returns:
        prompt 文本。
    """
    kp_codes = ", ".join(kp.code for kp in direction.kp_refs)
    parts = [
        f"体裁：{direction.genre}",
        f"学段：{direction.grade_band}",
        f"学科：{direction.subject}",
        f"知识点：{kp_codes}",
        f"目标难度区间：[{direction.difficulty_target.min}, {direction.difficulty_target.max}]",
    ]
    if direction.word_count_target is not None:
        lo, hi = direction.word_count_target
        parts.append(f"字数区间：{lo}-{hi}")
    return "；".join(parts)


__all__ = [
    "DifficultyTarget",
    "PromptDirection",
    "validate_prompt_direction",
    "direction_to_prompt",
]
