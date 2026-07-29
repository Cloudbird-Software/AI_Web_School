"""语篇难度分析器（T-W4-013）.

架构 v2 §4.1 / §4.8：AI 起草语篇后自动分析难度指标，输出字频/句长/生词率，
供语篇难度门（T-W4-014）比对目标区间。

指标定义（对齐 passage.DifficultyMetrics）：
- char_freq：字符频次分布（字→出现次数），字频分析留档。
- avg_sentence_length：平均句长（字/句），适龄参考（低段短句为主）。
- oov_rate：生词率（课标词表外词占比，0.0~1.0），越低越适龄。
- total_chars / total_sentences：基础统计量。

为什么字频用「字符」而非「词」：
- 小学语数英三科中，语文字频以「字」为单位（课标字表也是字级）；
- 分词需 jieba 等学科包工具，核心域不 import 学科包（A5）；
- 字符级分析零依赖、确定、可复现，学科包侧可注入 tokenizer overlay 做词级补充。

生词率（OOV）依赖课标词表，词表是学科包资产（语文课标字表/英语课标词表）。
核心域不持有词表：vocab_baseline 由调用方（学科包/C线 pipeline）注入；
未注入时 oov_rate=0.0 且标记 oov_baseline_available=False，难度门据此降级
（T-W4-014 难度门在无基线时仅做句长比对，不阻断）。

宪法 A5/X6：本模块不 import 任何学科包/学段包。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal, Optional

from src.core.content.passage_schema import DifficultyTarget
from src.core.models.passage import DifficultyMetrics

# 句子分隔符：中文（。！？；）+ 英文（.!?;\n）
# 保留分隔符前的内容为一个句子；连续分隔符合并（避免空句）
_SENTENCE_SPLIT_RE = re.compile(r"[。！？；.!?;\n]+")

# 标点与空白：字频统计排除这些（只统计实质字符）
# 中文标点范围 + 英文标点 + 空白
_PUNCT_OR_SPACE_RE = re.compile(
    r"[\s\u3000-\u303f\uff00-\uffef.,;:!?\"'()\[\]{}…—\-–]"
)

# 学段×平均句长的适龄参考上限（字/句），供偏差报告参考（非硬阈值）
# 低段（L）短句为主 ≤ 15；中段（M）≤ 25；高段（H）≤ 40
# 来源：课标学段认知特征的经验值，非精确科学，仅作偏差报告基线
_GRADE_BAND_SENTENCE_LENGTH_CEILING: dict[str, float] = {
    "L": 15.0,
    "M": 25.0,
    "H": 40.0,
}


@dataclass(frozen=True)
class DifficultyDeviation:
    """单指标偏差报告.

    Attributes:
        metric: 指标名（"oov_rate" / "avg_sentence_length"）。
        target_min: 目标区间下限。
        target_max: 目标区间上限。
        actual: 实际值。
        status: within（区间内）/ below（低于下限）/ above（高于上限）。
        delta: 实际值与最近边界的差（within=0；above=actual-max；below=min-actual）。
    """

    metric: str
    target_min: float
    target_max: float
    actual: float
    status: Literal["within", "below", "above"]
    delta: float


@dataclass(frozen=True)
class DifficultyReport:
    """难度分析报告.

    - metrics：难度指标（落 passage.difficulty_metrics JSONB）。
    - oov_baseline_available：是否提供了课标词表（OOV 基线）。
    - deviations：偏差报告列表（实际值 vs 目标区间）。
    """

    metrics: DifficultyMetrics
    oov_baseline_available: bool
    deviations: list[DifficultyDeviation] = field(default_factory=list)


def _split_sentences(text: str) -> list[str]:
    """按中英文句末标点切句，返回非空句子列表.

    连续标点合并（如「。。」），避免产生空句；末尾无标点的剩余文本也作一句。
    """
    if not text or not text.strip():
        return []
    # split 保留标点间内容；过滤空串
    parts = _SENTENCE_SPLIT_RE.split(text)
    return [p for p in (s.strip() for s in parts) if p]


def _count_chars(text: str) -> tuple[dict[str, int], int]:
    """统计实质字符频次（排除标点与空白），返回 (频次字典, 实质字符总数)."""
    freq: dict[str, int] = {}
    total = 0
    for ch in text:
        if _PUNCT_OR_SPACE_RE.fullmatch(ch):
            continue
        freq[ch] = freq.get(ch, 0) + 1
        total += 1
    return freq, total


def _compute_oov_rate(
    char_freq: dict[str, int], total_chars: int, vocab_baseline: Optional[set[str]]
) -> tuple[float, bool]:
    """计算生词率（OOV rate）.

    Args:
        char_freq: 字符频次。
        total_chars: 实质字符总数。
        vocab_baseline: 课标词表（字级集合）；None 表示未提供。

    Returns:
        (oov_rate, baseline_available)：oov_rate 0.0~1.0；baseline_available 表示
        是否有基线（无基线时 oov_rate=0.0 且 available=False）。
    """
    if vocab_baseline is None:
        return 0.0, False
    if total_chars == 0:
        return 0.0, True
    oov_count = sum(
        cnt for ch, cnt in char_freq.items() if ch not in vocab_baseline
    )
    return round(oov_count / total_chars, 6), True


def _build_deviation(
    metric: str, actual: float, target: DifficultyTarget
) -> DifficultyDeviation:
    """构建单指标偏差报告（实际值 vs 目标区间）."""
    if actual < target.min:
        return DifficultyDeviation(
            metric=metric,
            target_min=target.min,
            target_max=target.max,
            actual=actual,
            status="below",
            delta=round(actual - target.min, 6),
        )
    if actual > target.max:
        return DifficultyDeviation(
            metric=metric,
            target_min=target.min,
            target_max=target.max,
            actual=actual,
            status="above",
            delta=round(actual - target.max, 6),
        )
    return DifficultyDeviation(
        metric=metric,
        target_min=target.min,
        target_max=target.max,
        actual=actual,
        status="within",
        delta=0.0,
    )


def analyze_difficulty(
    text: str,
    grade_band: str,
    *,
    vocab_baseline: Optional[set[str]] = None,
    difficulty_target: Optional[DifficultyTarget] = None,
) -> DifficultyReport:
    """分析语篇难度，返回字频/句长/生词率 + 偏差报告（任务卡 T-W4-013 验收 #2）.

    Args:
        text: 语篇正文。
        grade_band: 学段 L/M/H（影响句长适龄参考上限）。
        vocab_baseline: 课标词表（字级集合），由学科包/C线 pipeline 注入；
            None 时不计算生词率（oov_rate=0.0，标记无基线）。
        difficulty_target: 目标难度区间，用于偏差报告；None 时无偏差报告。

    Returns:
        DifficultyReport：含 DifficultyMetrics + 偏差列表。

    Notes:
        - 空文本：total_chars=0、total_sentences=0、avg_sentence_length=0.0、oov_rate=0.0。
        - 句长按实质字符（排除标点空白）计，更准确反映阅读负荷。
        - 偏差报告当前仅比对 oov_rate（生词率与难度区间直接对应）；
          句长适龄性由 T-W4-014 age_appropriate 验证器负责，此处不混估（D5 精神）。
    """
    sentences = _split_sentences(text)
    char_freq, total_chars = _count_chars(text)
    total_sentences = len(sentences)

    if total_sentences > 0 and total_chars > 0:
        avg_sentence_length = round(total_chars / total_sentences, 4)
    else:
        avg_sentence_length = 0.0

    oov_rate, baseline_available = _compute_oov_rate(
        char_freq, total_chars, vocab_baseline
    )

    metrics = DifficultyMetrics(
        avg_sentence_length=avg_sentence_length,
        oov_rate=oov_rate,
        total_chars=total_chars,
        total_sentences=total_sentences,
        char_freq=char_freq,
    )

    deviations: list[DifficultyDeviation] = []
    if difficulty_target is not None and baseline_available:
        # 仅在有 OOV 基线时比对 oov_rate（无基线时 oov_rate=0.0 无意义，不比对）
        deviations.append(
            _build_deviation("oov_rate", oov_rate, difficulty_target)
        )

    return DifficultyReport(
        metrics=metrics,
        oov_baseline_available=baseline_available,
        deviations=deviations,
    )


def grade_band_sentence_length_ceiling(grade_band: str) -> float:
    """返回学段平均句长适龄参考上限（字/句），供 age_appropriate 验证器消费.

    为什么暴露此函数：T-W4-014 适龄性验证器需句长上限做软阈值判断，而句长
    上限是难度分析的伴生产物，本模块是自然归属（避免验证器重复定义）。
    """
    return _GRADE_BAND_SENTENCE_LENGTH_CEILING.get(grade_band, 25.0)


__all__ = [
    "DifficultyDeviation",
    "DifficultyReport",
    "analyze_difficulty",
    "grade_band_sentence_length_ceiling",
]
