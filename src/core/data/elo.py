"""W3 S8：掌握度 Elo v1（在线轻量增量更新）.

架构 v2 §4.7「掌握度」：Elo/带遗忘衰减加权正确率起步；在线轻量增量 +
夜间批权威双轨；可换 BKT/IRT（D6 估计器可替换）。

本模块是 v1 的「在线轻量增量」侧：纯函数，无 DB IO、无副作用——
评级状态的存取（学生掌握度账）由调用方负责，便于后续接入夜间批权威侧。

模型（双评级 Elo，学生掌握度 × 题目难度成对更新）：
- 期望得分   E = 1 / (1 + 10^(-(R_s - R_i) / 400))
- 增量更新   R_s' = R_s + K·(S - E)   （学生掌握度）
             R_i' = R_i - K·(S - E)   （题目难度，方向相反：
               学生答对比预期容易 → 题难度评级下调）
- S ∈ {0, 1}（客观题对错）；K 为步长（默认 32）

难度换算（CTT 正确率 p → Elo 题目评级）：
平均学生 R_s = BASE 时期望得分 = p ⟹ R_i = BASE + 400·log10((1-p)/p)。
p 越小题越难，R_i 越高；p=0.5 时 R_i = BASE。
该换算让 CTT 实测难度（item_param.params.difficulty）可直接初始化
Elo 题目侧评级，打通「批处理标定 → 在线掌握度」的数据飞轮（§4.7）。

宪法 A5/X6：本模块是核心域，不 import 任何学科包/学段包。
"""
from __future__ import annotations

import math

# 基准评级（平均学生/中位难度锚点）与量表（Elo 经典 400 量表）
BASE_RATING = 1500.0
SCALE = 400.0
DEFAULT_K = 32.0

# p 截断边界：log10(0) 无定义；全对/全错样本的 p 截断到开区间
_P_EPS = 1e-6


def expected_score(student_rating: float, item_rating: float) -> float:
    """期望得分 E = P(学生答对) ∈ (0, 1).

    Args:
        student_rating: 学生掌握度评级。
        item_rating: 题目难度评级。

    Returns:
        Elo 期望得分；两评级相等时为 0.5。
    """
    return 1.0 / (1.0 + math.pow(10.0, -(student_rating - item_rating) / SCALE))


def elo_update(
    student_rating: float,
    item_rating: float,
    score: float,
    *,
    k: float = DEFAULT_K,
) -> tuple[float, float]:
    """一次作答的增量更新，返回 (新学生评级, 新题目评级).

    Args:
        student_rating: 作答前学生掌握度评级。
        item_rating: 作答前题目难度评级。
        score: 实际得分 S ∈ [0, 1]（客观题 0|1；部分分给分题可取中间值）。
        k: 步长（默认 32；低学段/小样本可调小，由策略配置）。

    Returns:
        (new_student_rating, new_item_rating)。

    Raises:
        ValueError: score 不在 [0, 1] 或 k <= 0。
    """
    if not 0.0 <= score <= 1.0:
        raise ValueError(f"score 必须在 [0, 1]，得到 {score!r}")
    if k <= 0:
        raise ValueError(f"k 必须为正，得到 {k!r}")
    e = expected_score(student_rating, item_rating)
    delta = k * (score - e)
    return student_rating + delta, item_rating - delta


def difficulty_to_rating(
    p: float,
    *,
    base: float = BASE_RATING,
    scale: float = SCALE,
) -> float:
    """CTT 正确率 p → Elo 题目难度评级.

    推导：平均学生 R_s = base 时 expected_score(base, R_i) = p
    ⟹ R_i = base + scale·log10((1-p)/p)。

    Args:
        p: 正确率（CTT difficulty，item_param.params.difficulty）；
           截断到 (1e-6, 1-1e-6) 开区间避免 log10(0)。
        base: 基准评级（默认 1500）。
        scale: 量表（默认 400）。

    Returns:
        Elo 题目难度评级；p=0.5 → base，p→0 → +∞ 方向（越难越高）。
    """
    clipped = min(max(p, _P_EPS), 1.0 - _P_EPS)
    return base + scale * math.log10((1.0 - clipped) / clipped)


__all__ = [
    "BASE_RATING",
    "SCALE",
    "DEFAULT_K",
    "expected_score",
    "elo_update",
    "difficulty_to_rating",
]
