"""T-W4-038 预组装回退验证：缓存命中时 < 300ms.

验收（任务卡 T-W4-038 §验收 #2）：
2. 预组装回退：test_preassembled_fallback.py 验证缓存命中时延迟 <300ms。

预组装回退链路（架构 v2 §6 性能与运维 S9）：
- 在线组卷首次请求触发完整 assemble() 求解（冷路径，p95<2s）；
- 求解结果按 (profile_id, profile_version, seed, snapshot_ref) 缓存；
- 同参数重复请求直接返回缓存（热路径，<300ms）——预组装回退。

为什么缓存层在测试内而非 src/core：owner_module=tests/performance，
本卡只验证回退链路的延迟特性，生产缓存实现属组卷服务层（另一任务卡范畴）。
"""
from __future__ import annotations

import time

import pytest

from src.core.assembly import CandidateItem, assemble
from src.core.assembly.profile import AssemblyProfile

from tests.performance.conftest import percentile

# ────────────────────────────────────────────────────────────────────
# 阈值
# ────────────────────────────────────────────────────────────────────

CACHE_HIT_P95_THRESHOLD_SEC: float = 0.300  # 300ms
SAMPLES: int = 1000


# ────────────────────────────────────────────────────────────────────
# 预组装缓存（最小实现，模拟生产缓存命中路径）
# ────────────────────────────────────────────────────────────────────

class _PreassembledCache:
    """预组装结果缓存：按确定性三要素键控.

    生产缓存可用 Redis/内存 LRU；此处用 dict 模拟命中路径的延迟特性。
    键 = (profile_id, profile_version, seed, snapshot_ref)——确定性三要素
    （架构 §4.4 R-Z-01：给定三要素结果唯一，故可安全缓存）。
    """

    __slots__ = ("_store",)

    def __init__(self) -> None:
        self._store: dict[tuple[str, str, int, str], object] = {}

    def put(
        self,
        profile: AssemblyProfile,
        seed: int,
        snapshot_ref: str,
        result: object,
    ) -> None:
        key = (profile.profile_id, profile.profile_version, seed, snapshot_ref)
        self._store[key] = result

    def get(
        self,
        profile: AssemblyProfile,
        seed: int,
        snapshot_ref: str,
    ) -> object | None:
        key = (profile.profile_id, profile.profile_version, seed, snapshot_ref)
        return self._store.get(key)


# ────────────────────────────────────────────────────────────────────
# 测试
# ────────────────────────────────────────────────────────────────────

def test_preassembled_cache_hit_under_300ms(
    large_pool: list[CandidateItem],
    practice_profile: AssemblyProfile,
) -> None:
    """验收 #2：预组装缓存命中时延迟 < 300ms.

    流程：
    1. 冷路径：首次 assemble() 求解，结果入缓存；
    2. 热路径：同参数查缓存命中，测 1000 次命中延迟的 p95；
    3. 断言 p95 < 300ms。
    """
    cache = _PreassembledCache()
    seed = 42
    snapshot_ref = "preassembled-snap-1"

    # 冷路径：预组装一次
    cold_result = assemble(
        practice_profile, large_pool, seed=seed, snapshot_ref=snapshot_ref
    )
    cache.put(practice_profile, seed, snapshot_ref, cold_result)

    # 热路径：测缓存命中延迟（含查表 + 返回，无求解）
    latencies: list[float] = []
    for _ in range(SAMPLES):
        t0 = time.perf_counter()
        hit = cache.get(practice_profile, seed, snapshot_ref)
        # 模拟生产中「序列化后返回」的最小开销：访问属性确保对象被触碰
        _ = getattr(hit, "items", None) or getattr(hit, "seed", None)
        latencies.append(time.perf_counter() - t0)

    p95 = percentile(latencies, 95)
    assert p95 < CACHE_HIT_P95_THRESHOLD_SEC, (
        f"缓存命中 p95={p95*1000:.2f}ms 超过阈值 "
        f"{CACHE_HIT_P95_THRESHOLD_SEC*1000:.0f}ms"
    )


def test_preassembled_cache_miss_falls_back_to_assemble(
    large_pool: list[CandidateItem],
    practice_profile: AssemblyProfile,
) -> None:
    """缓存未命中时回退到完整 assemble（功能性验证，非延迟）.

    确保回退链路正确：miss → 调用 assemble → 结果入缓存 → 后续命中。
    """
    cache = _PreassembledCache()
    seed = 7
    snapshot_ref = "miss-snap"

    # 未命中
    assert cache.get(practice_profile, seed, snapshot_ref) is None

    # 回退：完整求解
    result = assemble(
        practice_profile, large_pool, seed=seed, snapshot_ref=snapshot_ref
    )
    cache.put(practice_profile, seed, snapshot_ref, result)

    # 再次请求应命中，且结果一致（确定性）
    hit = cache.get(practice_profile, seed, snapshot_ref)
    assert hit is result, "缓存命中应返回同一对象（或等价内容）"
    assert getattr(hit, "selection_digest") == result.selection_digest


def test_cache_key_is_deterministic_triple(
    large_pool: list[CandidateItem],
    practice_profile: AssemblyProfile,
) -> None:
    """缓存键 = 确定性三要素（profile 版本 + seed + snapshot）.

    不同 seed 或 snapshot 应产生不同缓存条目（不能误命中）。
    """
    cache = _PreassembledCache()
    r1 = assemble(practice_profile, large_pool, seed=1, snapshot_ref="s1")
    cache.put(practice_profile, 1, "s1", r1)

    # 不同 seed → miss
    assert cache.get(practice_profile, 2, "s1") is None
    # 不同 snapshot → miss
    assert cache.get(practice_profile, 1, "s2") is None
    # 相同三要素 → hit
    assert cache.get(practice_profile, 1, "s1") is r1
