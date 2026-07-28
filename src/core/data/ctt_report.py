"""T-W4-030 测量卷 CTT 信度/区分度报告（架构 v2 §4.7 / 宪法 D6）.

本模块落地「测量卷 CTT 级报告」的纯函数部分：
- Cronbach's α（内部一致性系数）：α = (k/(k-1))·(1 - Σσ²ᵢ / σ²_total)
- 标准误 SEM = SD_total · √(1-α)
- 每题难度（正确率 p）与区分度（修正点二列）——复用 src/core/data/ctt.py 的
  compute_ctt，与既有 CTT 实现完全一致（任务卡验收 #3）
- 难度分布（按 [0,0.3)/[0.3,0.5)/[0.5,0.7)/[0.7,0.9)/[0.9,1.0] 五档分桶）
- 小样本警示：n < CTT_MIN_SAMPLE_DEFAULT（30）时报告头标记
  「样本不足，结果仅供参考」（任务卡验收 #2）

为什么纯函数与 DB IO 分离（与 ctt.py 同构）：
- 数值正确性（α / SEM 公式）可离线单测，无需 DB
- DB IO（按 paper_id 取数 + ActiveModelPointer 引用回填）由
  src/core/report/measurement_report.py 承担，owner=src/core/report
- 本模块 owner=src/core/data，禁止 import 学科包/学段包（宪法 A5/A7）

为什么 α 用样本方差（n-1 分母）：
- CTT 报告面对的是「样本学生的测量数据」，总体方差未知，标准做法用样本方差
  估计；Cronbach 原始公式用样本方差（Kuder-Richardson 同口径）
- 与既有 compute_ctt 内部 _pearson 的「样本均值 + 平方和」口径一致

宪法 D6（估计器可替换）：本报告引用当时活跃的 ActiveModelPointer（model_version
+ code_digest + input_snapshot_id + graph_release_id），由 measurement_report
回填；历史报告永远引用当时版本的实证，不因后续估计器升级而漂移。

非目标（任务卡 non_goals）：Rasch/IRT 报告、测量等值、标准设定、常模建立。
"""
from __future__ import annotations

import logging
import math
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, Sequence

from src.core.data.ctt import (
    CTT_MIN_SAMPLE_DEFAULT,
    ItemCttStats,
    ResponseRecord,
    compute_ctt,
)

logger = logging.getLogger(__name__)

# 难度分布分桶边界（p_correct 口径，越大越易）
# 五档：难(0-0.3) / 较难(0.3-0.5) / 中(0.5-0.7) / 较易(0.7-0.9) / 易(0.9-1.0)
# 半开区间 [lo, hi)，最后一档含 1.0
_DIFFICULTY_BANDS: tuple[tuple[str, float, float], ...] = (
    ("hard", 0.0, 0.3),
    ("somewhat_hard", 0.3, 0.5),
    ("medium", 0.5, 0.7),
    ("somewhat_easy", 0.7, 0.9),
    ("easy", 0.9, 1.0 + 1e-9),  # 含 1.0
)


# ────────────────────────────────────────────────────────────────────
# 数据结构（用 dataclass 而非 Pydantic：纯数据容器，无需序列化校验开销）
# ────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ItemStat:
    """单题 CTT 统计（报告内嵌条目）.

    与 ItemCttStats 字段一致，但语义为「报告呈现口径」——区分度 None 时
    含义有二：(a) 小样本（n<30）点二列无意义；(b) 零方差/单样本不可计算。
    两者均不伪造 0，由 notes 显式说明。
    """

    item_version_id: str
    sample_size: int
    difficulty: float
    discrimination: Optional[float]


@dataclass(frozen=True)
class DifficultyBand:
    """难度分布单桶统计."""

    band: str
    lower: float  # 区间下界（含）
    upper: float  # 区间上界（不含，最后一桶含 1.0）
    count: int


@dataclass(frozen=True)
class CttReport:
    """CTT 信度/区分度报告（纯函数产物，无 DB 依赖）.

    Attributes:
        paper_id: 关联测量卷 id（仅作标签，本函数不校验其存在性）。
        sample_size: 学生数 n（去重 student_alias_id 计数）。
        item_count: 题数 k（去重 item_version_id 计数）。
        cronbach_alpha: Cronbach's α（内部一致性）；k<2 / σ²_total=0 / n<2 时 None。
        sem: 测量标准误 SD·√(1-α)；α 为 None 时 None。
        item_stats: 每题统计（按 item_version_id 升序，确定性）。
        difficulty_distribution: 难度分布五档计数。
        small_sample_warning: n<30 时 True（验收 #2）。
        notes: 备注与警示列表（含小样本警示文案，供消费方直接呈现）。
        generated_at: 报告生成时刻（UTC）。
    """

    paper_id: str
    sample_size: int
    item_count: int
    cronbach_alpha: Optional[float]
    sem: Optional[float]
    item_stats: list[ItemStat]
    difficulty_distribution: list[DifficultyBand]
    small_sample_warning: bool
    notes: list[str]
    generated_at: datetime


# ────────────────────────────────────────────────────────────────────
# 纯函数：方差 / Cronbach's α / SEM
# ────────────────────────────────────────────────────────────────────


def _sample_variance(values: Sequence[float]) -> Optional[float]:
    """样本方差（n-1 分母）；n<2 或零方差返回 None.

    为什么 None 而非 0：方差为 0 是合法但极端情况（全员同分），与「不可计算」
    语义不同——但 Cronbach's α 在零方差下定义失效（分母 0），统一返回 None
    让上层用 α=None 表达「不可计算」，notes 解释原因。
    """
    n = len(values)
    if n < 2:
        return None
    mean = sum(values) / n
    ss = sum((v - mean) ** 2 for v in values)
    if ss == 0.0:
        return None
    return ss / (n - 1)


def _cronbach_alpha(
    item_scores_by_student: Sequence[Sequence[float]],
) -> Optional[float]:
    """Cronbach's α（内部一致性系数）.

    α = (k/(k-1)) · (1 - Σσ²ᵢ / σ²_total)
    其中：
    - k = 题数
    - σ²ᵢ = 题 i 的样本方差（n-1 分母）
    - σ²_total = 学生总分（各题得分之和）的样本方差

    Args:
        item_scores_by_student: 每学生一行的题分序列；行 i 是学生 i 在 k 题上的
            得分列表。所有行必须等长（同题集）。空列表或 k<2 返回 None。

    Returns:
        α ∈ [可能负值, 1.0]；k<2 / n<2 / σ²_total=0 时返回 None。
        负值表示题间反向相关（理论可能，提示题序或反向计分问题，如实返回）。
    """
    n = len(item_scores_by_student)
    if n < 2:
        return None
    k = len(item_scores_by_student[0])
    if k < 2:
        return None
    # 校验所有行等长（防御性，调用方应保证）
    for row in item_scores_by_student:
        if len(row) != k:
            return None

    # 逐题方差
    item_variances: list[float] = []
    for j in range(k):
        col = [row[j] for row in item_scores_by_student]
        v = _sample_variance(col)
        if v is None:
            # 某题零方差：σ²ᵢ 贡献为 0，仍可计算 α（该题不区分学生，但 α 公式有效）
            item_variances.append(0.0)
        else:
            item_variances.append(v)

    # 学生总分方差
    total_scores = [sum(row) for row in item_scores_by_student]
    total_var = _sample_variance(total_scores)
    if total_var is None or total_var == 0.0:
        return None

    sum_item_var = sum(item_variances)
    alpha = (k / (k - 1)) * (1.0 - sum_item_var / total_var)
    return alpha


def _bin_difficulty(p: float) -> Optional[str]:
    """将难度 p 分到五档之一；越界返回 None（不应发生，p∈[0,1]）."""
    for band, lo, hi in _DIFFICULTY_BANDS:
        if lo <= p < hi:
            return band
    return None


def _difficulty_distribution(item_stats: Sequence[ItemStat]) -> list[DifficultyBand]:
    """按五档汇总难度分布；每桶含 count，确定性按 band 定义序输出."""
    counter: Counter[str] = Counter()
    for s in item_stats:
        band = _bin_difficulty(s.difficulty)
        if band is not None:
            counter[band] += 1
    return [
        DifficultyBand(band=band, lower=lo, upper=hi, count=counter.get(band, 0))
        for band, lo, hi in _DIFFICULTY_BANDS
    ]


# ────────────────────────────────────────────────────────────────────
# 主入口（任务卡验收 #1）
# ────────────────────────────────────────────────────────────────────


def generate_ctt_report(
    response_events: Sequence[ResponseRecord],
    paper_id: str,
    *,
    min_sample: int = CTT_MIN_SAMPLE_DEFAULT,
    now: Optional[datetime] = None,
) -> CttReport:
    """生成测量卷 CTT 信度/区分度报告（纯函数，验收 #1）.

    参数:
        response_events: 单场景作答记录（调用方保证已按 scene='measurement' 过滤，
            D5 禁混估；本函数不复检 scene 字段，因 ResponseRecord 不携带 scene）。
        paper_id: 关联测量卷 id（仅作报告标签；本函数不校验其与事件的关联性，
            关联校验由 measurement_report.build_measurement_report 通过
            source_ref->>'paper_id' 取数时保证）。
        min_sample: 小样本门槛，默认 CTT_MIN_SAMPLE_DEFAULT(30)；n<min_sample 时
            small_sample_warning=True 并在 notes 加警示文案（验收 #2）。
        now: 报告生成时刻（默认 datetime.now(UTC)）；可传入固定值用于确定性测试。

    返回:
        CttReport（含 α / SEM / item_stats / difficulty_distribution / notes）。

    Notes:
        - 区分度复用 compute_ctt（验收 #3）：n<2 或零方差时 discrimination=None。
        - 小样本（n<30）时区分度仍按 compute_ctt 计算（既有逻辑），但
          small_sample_warning=True 警示整体结果仅供参考；区分度本身 None
          与否由 compute_ctt 的零方差/n<2 判定决定，与 min_sample 解耦——
          min_sample 仅控制报告头警示，不影响单题区分度 None 判定（与
          compute_discrimination 的 30 门槛职责分离，避免双重门槛混淆）。
        - 空事件列表：返回 α=None、sem=None、item_stats=[]、n=0、k=0，
          small_sample_warning=True，notes 含「无作答数据」。
    """
    notes: list[str] = []
    generated_at = now or datetime.now(timezone.utc)

    # 学生数 n（去重）
    student_ids = {r.student_alias_id for r in response_events}
    n = len(student_ids)

    # 题数 k（去重）
    item_ids = {r.item_version_id for r in response_events}
    k = len(item_ids)

    # 复用 compute_ctt 计算每题统计（验收 #3：区分度与既有 CTT 一致）
    ctt_stats: list[ItemCttStats] = compute_ctt(response_events)
    item_stats: list[ItemStat] = [
        ItemStat(
            item_version_id=s.item_version_id,
            sample_size=s.sample_size,
            difficulty=s.difficulty,
            discrimination=s.discrimination,
        )
        for s in ctt_stats
    ]

    # 小样本警示（验收 #2）
    small_sample_warning = n < min_sample
    if small_sample_warning:
        notes.append(
            f"样本不足，结果仅供参考（n={n} < min_sample={min_sample}；"
            "Cronbach's α 与区分度在小样本下方差大，不可作为定论）"
        )

    # 边界情形
    if n == 0 or k == 0:
        notes.append("无作答数据：无法计算 α / SEM（n=0 或 k=0）。")
        return CttReport(
            paper_id=paper_id,
            sample_size=n,
            item_count=k,
            cronbach_alpha=None,
            sem=None,
            item_stats=item_stats,
            difficulty_distribution=_difficulty_distribution(item_stats),
            small_sample_warning=small_sample_warning,
            notes=notes,
            generated_at=generated_at,
        )

    # 构造 α 计算矩阵：行=学生，列=题，缺位用 0.0 填（学生未答该题记 0 分）
    # 为什么用 0 填缺位而非过滤：CTT 假设全题集，缺答视为 0 分是教育测量惯例
    # （未答=不得分）；过滤会改变 σ²_total 的样本基数，与 CTT 定义不符。
    sorted_item_ids = sorted(item_ids)
    item_idx = {vid: j for j, vid in enumerate(sorted_item_ids)}
    matrix: list[list[float]] = []
    # 按学生聚合（去重 student_alias_id，同一学生多条同题记录取最后一条——
    # 调用方应保证不重复，本处防御性去重）
    student_rows: dict[str, list[float]] = {}
    for r in response_events:
        sid = r.student_alias_id
        if sid not in student_rows:
            student_rows[sid] = [0.0] * k
        student_rows[sid][item_idx[r.item_version_id]] = r.correct
    matrix = list(student_rows.values())

    alpha = _cronbach_alpha(matrix)
    if alpha is None:
        notes.append(
            "Cronbach's α 不可计算：k<2 / n<2 / 学生总分零方差（全员同分）。"
        )
        sem: Optional[float] = None
    else:
        # SEM = SD_total · √(1-α)，SD_total 用样本标准差（n-1 分母）
        total_scores = [sum(row) for row in matrix]
        total_var = _sample_variance(total_scores)
        if total_var is None:
            sem = None
            notes.append("SEM 不可计算：学生总分零方差。")
        else:
            sd = math.sqrt(total_var)
            sem = sd * math.sqrt(1.0 - alpha)

    # 区分度全 None 时补一条备注（区分度不可计算原因：n<2 / 零方差）
    if item_stats and all(s.discrimination is None for s in item_stats):
        notes.append(
            "所有题目区分度均不可计算（n<2 或题分零方差）；不伪造 0。"
        )

    return CttReport(
        paper_id=paper_id,
        sample_size=n,
        item_count=k,
        cronbach_alpha=alpha,
        sem=sem,
        item_stats=item_stats,
        difficulty_distribution=_difficulty_distribution(item_stats),
        small_sample_warning=small_sample_warning,
        notes=notes,
        generated_at=generated_at,
    )


__all__ = [
    "ItemStat",
    "DifficultyBand",
    "CttReport",
    "generate_ctt_report",
]
