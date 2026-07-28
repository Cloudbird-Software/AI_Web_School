"""T-W4-024 在线播放器服务：限次播放策略（架构 v2 §4.6 / S5）.

模拟考试场景：听力只允许听 1–2 遍，第 3 次播放被拒（403）。播放次数按
(audio_id, session_id) 维度计数，同一 session 内同一音频累计播放。

为什么用可注入的 PlayCountStore：生产环境用 Redis（原子 INCR + TTL），
单元测试用 InMemoryPlayCountStore（确定性、无外部依赖）。与
AudioStorageWriter / TTSEngine 同模式——副作用边界可注入。

为什么 MAX_PLAYS=2：ADR §4.6「限次播放，模拟考试只听 1–2 遍」——允许 2 次，
第 3 次调用返回 403 拒绝。MAX_PLAYS 是允许的成功播放次数上限。

宪法 A5/X6：不 import 学科包/学段包。
宪法 D7：audio_url 由上层调用方从 AudioAsset.url 传入，本模块不感知 PII。
"""
from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field


# ════════════════════════════════════════════════════════════════════
# 常量
# ════════════════════════════════════════════════════════════════════

# 最大允许播放次数（第 MAX_PLAYS+1 次调用被拒）
# 为什么 2：ADR §4.6「模拟考试只听 1–2 遍」
MAX_PLAYS: int = 2


# ════════════════════════════════════════════════════════════════════
# 异常
# ════════════════════════════════════════════════════════════════════


class PlayLimitExceededError(Exception):
    """播放次数超限（同一 session 内同一音频播放次数已达上限）.

    API 层应捕获此异常并返回 403 Forbidden。
    """

    def __init__(self, audio_id: str, session_id: str, play_count: int, max_plays: int) -> None:
        self.audio_id = audio_id
        self.session_id = session_id
        self.play_count = play_count
        self.max_plays = max_plays
        super().__init__(
            f"播放超限：audio_id={audio_id!r} session_id={session_id!r} "
            f"已播放 {play_count} 次（上限 {max_plays} 次）"
        )


# ════════════════════════════════════════════════════════════════════
# 播放次数存储（可注入的副作用边界）
# ════════════════════════════════════════════════════════════════════


class PlayCountStore(Protocol):
    """播放次数存储契约（生产替换为 Redis 适配器）.

    语义：increment 是原子的（并发安全），返回递增后的新计数。
    生产 Redis 实现：INCR key + EXPIRE key ttl（session 级 TTL）。
    """

    def increment(self, audio_id: str, session_id: str) -> int:
        """原子递增 (audio_id, session_id) 的播放计数，返回新值."""
        ...

    def get_count(self, audio_id: str, session_id: str) -> int:
        """获取当前播放计数（不递增）."""
        ...

    def reset(self, audio_id: str, session_id: str) -> None:
        """重置计数（测试隔离用）."""
        ...


class InMemoryPlayCountStore:
    """内存播放次数存储（测试用，生产替换为 Redis）.

    为什么用 dict 而非 Redis：单元测试需 hermetic，不依赖 Redis 实例。
    生产替换为 RedisPlayCountStore（INCR 原子操作 + TTL 过期）。
    """

    def __init__(self) -> None:
        self._counts: dict[tuple[str, str], int] = {}

    def increment(self, audio_id: str, session_id: str) -> int:
        key = (audio_id, session_id)
        self._counts[key] = self._counts.get(key, 0) + 1
        return self._counts[key]

    def get_count(self, audio_id: str, session_id: str) -> int:
        return self._counts.get((audio_id, session_id), 0)

    def reset(self, audio_id: str, session_id: str) -> None:
        self._counts.pop((audio_id, session_id), None)


# ════════════════════════════════════════════════════════════════════
# 播放结果
# ════════════════════════════════════════════════════════════════════


class PlayResult(BaseModel):
    """播放结果（验收 #1）.

    - audio_id / session_id：播放维度标识。
    - url：音频可访问 URL（来自 AudioAsset.url，消费层透传）。
    - play_count：本次播放后的累计次数（含本次）。
    - max_plays：允许的最大播放次数。
    - remaining：剩余播放次数（max_plays - play_count）。
    """

    model_config = ConfigDict(extra="forbid")

    audio_id: str
    session_id: str
    url: str
    play_count: int = Field(ge=1, description="本次播放后累计次数")
    max_plays: int = Field(ge=1, description="允许的最大播放次数")
    remaining: int = Field(ge=0, description="剩余播放次数")


# ════════════════════════════════════════════════════════════════════
# 公共入口
# ════════════════════════════════════════════════════════════════════

# 模块级共享存储（默认实例；测试可注入独立实例隔离）
_default_store: PlayCountStore = InMemoryPlayCountStore()


def get_default_store() -> PlayCountStore:
    """获取模块级默认播放计数存储（测试隔离用）."""
    return _default_store


def set_default_store(store: PlayCountStore) -> None:
    """替换模块级默认存储（测试注入用）."""
    global _default_store
    _default_store = store


def play(
    audio_id: str,
    session_id: str,
    *,
    audio_url: str,
    store: PlayCountStore | None = None,
    max_plays: int = MAX_PLAYS,
) -> PlayResult:
    """播放音频：返回音频 URL，同一 session 第 max_plays+1 次调用被拒（验收 #1）.

    限次策略（ADR §4.6「限次播放，模拟考试只听 1–2 遍」）：
    1. increment (audio_id, session_id) 计数（原子操作）。
    2. 新计数 > max_plays → 抛 PlayLimitExceededError（API 层转 403）。
    3. 新计数 ≤ max_plays → 返回 PlayResult（含 url + 剩余次数）。

    为什么 increment-then-check 而非 check-then-increment：increment 是原子操作，
    避免并发场景下的 check-then-act 竞态。第 3 次调用 increment → 3 > 2 → 拒绝，
    计数保持 3（记录尝试次数，后续调用同样被拒）。

    Args:
        audio_id: 音频素材内容寻址 id（来自 AudioAsset.audio_id）。
        session_id: 会话 id（同一 session 内累计播放次数）。
        audio_url: 音频可访问 URL（来自 AudioAsset.url，上层调用方传入）。
        store: 播放计数存储（None → 模块级默认 InMemoryPlayCountStore）。
        max_plays: 最大允许播放次数（默认 MAX_PLAYS=2）。

    Returns:
        PlayResult：含 url / play_count / remaining。

    Raises:
        PlayLimitExceededError: 播放次数超限（第 max_plays+1 次及以后）。
    """
    s = store if store is not None else _default_store
    new_count = s.increment(audio_id, session_id)

    if new_count > max_plays:
        raise PlayLimitExceededError(
            audio_id=audio_id,
            session_id=session_id,
            play_count=new_count,
            max_plays=max_plays,
        )

    return PlayResult(
        audio_id=audio_id,
        session_id=session_id,
        url=audio_url,
        play_count=new_count,
        max_plays=max_plays,
        remaining=max_plays - new_count,
    )


__all__ = [
    "MAX_PLAYS",
    "PlayLimitExceededError",
    "PlayCountStore",
    "InMemoryPlayCountStore",
    "PlayResult",
    "play",
    "get_default_store",
    "set_default_store",
]
