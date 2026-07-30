"""T-W4-011 TTS 总线档路由.

tts_synthesize() 是 TTS 合成统一入口（架构 v2 §4.6/§4.8）：
- 按学段（grade_band）从 voice_profiles.yaml 读语速（wpm）与默认音色；
- 调用注入的 TTSEngine 合成音频（默认 MockTTSEngine，生产替换真实适配器）；
- 输出内容寻址 id（D3：相同文本+相同配置得相同 id）+ 元数据；
- 内存缓存：相同 content_id 命中缓存，不重复合成（验收 #3）。

为什么独立于 bus.router（LLM 路由）：TTS 是 AI 总线的另一档（§4.8），
与 LLM 调用契约不同（返回字节流而非文本）。本卡聚焦 TTS 合成与学段配置；
台账集成由上层调用方负责（可选注入 ledger 实例）。

宪法 A5：学段参数通过 voice_profiles.yaml 注入，不 import 学段包。
宪法 D3：内容寻址——content_id = sha256(text + voice + wpm + engine)。
"""
from __future__ import annotations

import hashlib
from collections import OrderedDict
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal, Optional, Protocol

import yaml

GradeBand = Literal["L", "M", "H"]

_PROFILES_PATH = Path(__file__).resolve().parent / "voice_profiles.yaml"


@lru_cache(maxsize=1)
def _load_profiles_cached() -> dict[str, Any]:
    """加载 voice_profiles.yaml（进程级缓存，热加载由 reload_profiles 触发）."""
    with _PROFILES_PATH.open(encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict) or "grade_bands" not in data or "voices" not in data:
        raise ValueError(
            "voice_profiles.yaml 结构非法：期望 grade_bands + voices"
        )
    return data


def reload_profiles() -> None:
    """失效配置缓存，下次 tts_synthesize 重新读盘（配置热加载）."""
    _load_profiles_cached.cache_clear()


def get_profiles() -> dict[str, Any]:
    """返回当前生效的音色配置（测试与上层消费用）."""
    return _load_profiles_cached()


def compute_content_id(
    text: str, voice: str, wpm: int, engine: str
) -> str:
    """内容寻址 id（D3）：相同输入得相同 id.

    为什么纳入 engine：不同引擎合成质量不同，即使文本/音色/语速相同，
    音频内容也不同——engine 是内容寻址的一部分。
    """
    payload = f"{text}|{voice}|{wpm}|{engine}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def estimate_duration_ms(text: str, wpm: int) -> int:
    """估算音频时长（毫秒）.

    中文按字数（每字视为一"词"），英文按空格分词。wpm=字/分钟。
    简化估算：duration_ms = len(text) * 60000 / wpm。
    真实时长由引擎决定，此处仅用于元数据与成本估算。
    """
    if wpm <= 0:
        return 0
    return int(len(text) * 60000 / wpm)


@dataclass(frozen=True)
class TTSResult:
    """TTS 合成结果（验收 #1）.

    Attributes:
        audio: 音频字节流（mock 引擎返回占位字节，生产返回 mp3/wav）。
        content_id: 内容寻址 id（D3，相同输入得相同 id）。
        metadata: 元数据 {wpm, voice, voice_id, engine, duration_ms, text_length}。
    """

    audio: bytes
    content_id: str
    metadata: dict[str, Any] = field(default_factory=dict)


class TTSEngine(Protocol):
    """TTS 引擎统一契约（生产适配器实现此协议）."""

    def synthesize(
        self, text: str, *, voice_id: str, wpm: int
    ) -> bytes:
        """合成音频，返回字节流."""
        ...


class MockTTSEngine:
    """默认桩 TTS 引擎：无外部依赖，返回占位字节（验收 #4：mock 引擎）.

    为什么需要桩：
    - 011 的 owner_module=src/core/ai/tts，不依赖外部 TTS 服务；
    - 真实 TTS key 在 CI 中不可用（X3）；
    - 桩返回确定性字节（含 voice_id/wpm/text 信息），便于测试断言。

    生产应替换为真实 TTS 适配器（如 Azure TTS / 火山引擎 TTS）。
    """

    ENGINE_NAME = "mock"

    def synthesize(
        self, text: str, *, voice_id: str, wpm: int
    ) -> bytes:
        # 占位音频：编码 voice_id/wpm/text，便于测试断言内容寻址一致性
        return f"audio:{voice_id}:{wpm}:{text}".encode("utf-8")


# ── 内存缓存：相同 content_id 不重复合成（验收 #3），LRU淘汰 maxsize=1024 ────
_cache: "OrderedDict[str, TTSResult]" = OrderedDict()
_CACHE_MAXSIZE = 1024


def clear_cache() -> None:
    """清空 TTS 缓存（测试隔离用）."""
    _cache.clear()


def tts_synthesize(
    text: str,
    grade_band: GradeBand,
    voice_profile: Optional[str] = None,
    *,
    engine: Optional[TTSEngine] = None,
) -> TTSResult:
    """TTS 合成统一入口（任务卡 T-W4-011 验收 #1/#2/#3）.

    Args:
        text: 待合成文本（D7：函数入口强制PII剥离）。
        grade_band: 学段 L/M/H，决定 wpm 与默认音色。
        voice_profile: 音色名（None 时用学段默认音色）。
        engine: TTS 引擎（None 时用 MockTTSEngine）。

    Returns:
        TTSResult：音频字节流 + 内容寻址 id + 元数据。

    Notes:
        - 学段配置：L(1-2) 120wpm / M(3-4) 140wpm / H(5-6) 160wpm（验收 #2）。
        - 内容寻址（D3）：相同 text + 相同配置 → 相同 content_id（验收 #3）。
        - 缓存命中：相同 content_id 直接返回缓存结果，不重复调用引擎。
    """
    pii_warnings: list[str] = []
    try:
        from src.core.ai.ledger.pii_filter import strip as pii_strip
    except ImportError:
        pii_warnings.append("pii_filter_unavailable")
    else:
        try:
            sanitized_text, stripped = pii_strip(text)
            text = sanitized_text
            if stripped:
                pii_warnings.append(f"stripped:{','.join(stripped)}")
        except Exception as exc:  # noqa: BLE001
            # D7 fail-closed：PII剥离失败禁止继续合成
            raise PermissionError(
                "PII filter failed, TTS synthesis blocked"
            ) from exc

    profiles = _load_profiles_cached()
    bands = profiles["grade_bands"]
    if grade_band not in bands:
        raise ValueError(
            f"未知 grade_band={grade_band}，配置仅含 {list(bands)}"
        )
    band_cfg = bands[grade_band]
    wpm = band_cfg["wpm"]
    voice_name = voice_profile or band_cfg["default_voice"]

    voices = profiles["voices"]
    if voice_name not in voices:
        raise ValueError(
            f"未知 voice_profile={voice_name}，配置仅含 {list(voices)}"
        )
    voice_cfg = voices[voice_name]
    engine_name = voice_cfg.get("engine", "mock")
    voice_id = voice_cfg["voice_id"]

    eng = engine if engine is not None else MockTTSEngine()
    content_id = compute_content_id(text, voice_name, wpm, engine_name)

    # 缓存命中（验收 #3，LRU：命中时移到末尾）
    if content_id in _cache:
        _cache.move_to_end(content_id)
        cached = _cache[content_id]
        meta = dict(cached.metadata)
        if pii_warnings:
            meta["pii_warnings"] = pii_warnings
            return TTSResult(
                audio=cached.audio,
                content_id=cached.content_id,
                metadata=meta,
            )
        return cached

    audio = eng.synthesize(text, voice_id=voice_id, wpm=wpm)
    duration_ms = estimate_duration_ms(text, wpm)
    result_metadata: dict[str, Any] = {
        "wpm": wpm,
        "voice": voice_name,
        "voice_id": voice_id,
        "engine": engine_name,
        "duration_ms": duration_ms,
        "text_length": len(text),
        "grade_band": grade_band,
    }
    if pii_warnings:
        result_metadata["pii_warnings"] = pii_warnings
    result = TTSResult(
        audio=audio,
        content_id=content_id,
        metadata=result_metadata,
    )

    # 写入 LRU 缓存：移到末尾，超过maxsize淘汰最旧
    _cache[content_id] = result
    _cache.move_to_end(content_id)
    if len(_cache) > _CACHE_MAXSIZE:
        _cache.popitem(last=False)

    # 台账记录（P0-10：TTS合成后记一笔）
    try:
        from src.core.ai.ledger.ledger import record_call as ledger_record_call
        ledger_record_call(
            task_level="L1",
            task_name="tts_synthesize",
            provider=engine_name,
            model=f"{voice_name}:{voice_id}",
            prompt=text,
            token_in=len(text),
            token_out=0,
            duration_ms=float(duration_ms),
            task_stage="tts",
            artifact_ref=None,
            raw_meta={
                "grade_band": grade_band,
                "content_id": content_id,
                "wpm": wpm,
                **({"pii_warnings": pii_warnings} if pii_warnings else {}),
            },
        )
    except Exception:  # noqa: BLE001
        pass

    return result
