"""T-W4-038/039 性能压测共享 fixture.

设计原则：
- 候选池在测试内纯内存构造（模拟「DB 已加载候选池」后的求解热路径），
  避免依赖 DB 数据状态，保证压测可重复。
  「本地 DB，无网络抖动」的环境条件在 report 中单列（PostgreSQL 16 本地实例）。
- 不 import 任何学科包/学段包（宪法 A5/A7）；知识点 code 用中性的 math.* 前缀，
  仅作求解器分组键，不引入学科语义。
"""
from __future__ import annotations

from src.core.assembly import CandidateItem, compile_profile
from src.core.assembly.profile import AssemblyProfile

# ────────────────────────────────────────────────────────────────────
# 候选池构造
# ────────────────────────────────────────────────────────────────────

# 4 个知识点 × 50 题 = 200 题候选池（覆盖典型单元组卷规模）
_POOL_KPS: list[str] = ["math.a", "math.b", "math.c", "math.d"]
_POOL_PER_KP: int = 50


def _mk_candidate(
    vid: str,
    kp: list[str],
    *,
    p: float | None = 0.6,
    mode: str = "single",
    tpl: str | None = None,
    gradeband: str = "M",
    mix_tag: str | None = None,
    group_id: str | None = None,
) -> CandidateItem:
    """构造候选题（字段默认值与 tests/unit/test_assembly_solver._mk 对齐）."""
    return CandidateItem(
        item_version_id=vid,
        item_id=f"item-{vid}",
        template_version_id=tpl if tpl is not None else f"tpl-{vid}",
        kp_codes=kp,
        kp_set_mode=mode,  # type: ignore[arg-type]
        gradeband=gradeband,  # type: ignore[arg-type]
        interaction_id="single_choice",
        p_correct_prior=p,
        mix_tag=mix_tag,  # type: ignore[arg-type]
        group_id=group_id,
    )


def build_large_pool(
    kps: list[str] | None = None,
    per_kp: int = _POOL_PER_KP,
) -> list[CandidateItem]:
    """构造大规模候选池：每知识点 per_kp 题，难度 0.30–0.95 均匀分布.

    为什么纯内存构造：压测目标是 assemble() 求解热路径延迟，而非 DB I/O。
    生产中候选池按 (pack, gradeband) 加载后可在请求批次内复用；
    此处模拟「池已就绪」状态，专注测求解器。
    """
    kps = kps or _POOL_KPS
    pool: list[CandidateItem] = []
    for kp in kps:
        for i in range(per_kp):
            # 难度从 0.30 递增到 0.95，覆盖冷启动区间与梯度排序需求
            p = 0.30 + (0.65 * i / max(1, per_kp - 1))
            # 每 7 题一道无先验题（测梯度排序末尾分支）
            prior = None if i % 7 == 6 else round(p, 3)
            # 内容配比标签轮转（new/review/confusable）
            mix = ["new", "review", "confusable"][i % 3]
            pool.append(
                _mk_candidate(
                    f"{kp}.{i:03d}",
                    [kp],
                    p=prior,
                    mix_tag=mix,  # type: ignore[arg-type]
                )
            )
    return pool


def build_practice_profile(
    kps: list[str] | None = None,
    count: tuple[int, int] = (12, 15),
) -> AssemblyProfile:
    """练习用途 Profile：题量 12–15，每知识点配额 3."""
    kps = kps or _POOL_KPS
    return compile_profile(
        profile_id="perf-practice",
        profile_version="1.0.0",
        purpose="practice",
        gradeband="M",
        kp_codes=kps,
        purpose_overlay={"item_count_range": list(count)},
    )


# ────────────────────────────────────────────────────────────────────
# pytest fixture
# ────────────────────────────────────────────────────────────────────

import pytest  # noqa: E402  (置后避免循环 import 嫌疑，实为风格统一)


@pytest.fixture
def large_pool() -> list[CandidateItem]:
    """200 题候选池（4 知识点 × 50 题）."""
    return build_large_pool()


@pytest.fixture
def practice_profile() -> AssemblyProfile:
    """练习 Profile（题量 12–15）."""
    return build_practice_profile()


# ────────────────────────────────────────────────────────────────────
# 统计工具（供测试与报告生成共用）
# ────────────────────────────────────────────────────────────────────

def percentile(values: list[float], pct: float) -> float:
    """线性插值百分位（pct ∈ [0, 100]）.

    为什么自己实现：statistics 无 percentile；避免引入 numpy 依赖（X8）。
    """
    if not values:
        return float("nan")
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    k = (len(s) - 1) * (pct / 100.0)
    lo = int(k)
    hi = min(lo + 1, len(s) - 1)
    frac = k - lo
    return s[lo] + (s[hi] - s[lo]) * frac


def latency_histogram(values: list[float], bins: int = 10) -> list[tuple[float, float, int]]:
    """构造延迟分布直方图，返回 [(bin_lo, bin_hi, count), ...]."""
    if not values:
        return []
    lo, hi = min(values), max(values)
    if hi == lo:
        return [(lo, hi, len(values))]
    width = (hi - lo) / bins
    counts = [0] * bins
    for v in values:
        idx = min(int((v - lo) / width), bins - 1)
        counts[idx] += 1
    return [
        (lo + i * width, lo + (i + 1) * width, counts[i])
        for i in range(bins)
    ]
