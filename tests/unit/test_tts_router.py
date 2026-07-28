"""T-W4-011 TTS 总线档单元测试.

验收对照：
  #1 tts_synthesize 返回音频字节流 + 内容寻址 id + 元数据（语速/音色/时长）
  #2 学段配置：L=120wpm / M=140wpm / H=160wpm
  #3 相同文本+相同配置返回相同 content_id（缓存命中）
  #4 make accept 全绿；使用 mock TTS 引擎
  #5 不 import 学科包；学段参数通过配置注入

测试隔离：每个测试前 clear_cache()，避免缓存污染。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from src.core.ai.tts.router import (
    MockTTSEngine,
    TTSResult,
    compute_content_id,
    estimate_duration_ms,
    get_profiles,
    tts_synthesize,
    clear_cache,
)


@pytest.fixture(autouse=True)
def _clear_tts_cache() -> None:
    """每个测试前清空 TTS 缓存，隔离缓存命中验证."""
    clear_cache()
    yield
    clear_cache()


# ── 验收 #1：返回音频字节流 + 内容寻址 id + 元数据 ──────────────────

def test_tts_synthesize_returns_audio_bytes() -> None:
    """tts_synthesize 返回 bytes 音频."""
    result = tts_synthesize("Hello world", "M")
    assert isinstance(result.audio, bytes)
    assert len(result.audio) > 0


def test_tts_synthesize_returns_content_id() -> None:
    """tts_synthesize 返回内容寻址 id（32 位 hex）."""
    result = tts_synthesize("你好", "L")
    assert isinstance(result.content_id, str)
    assert len(result.content_id) == 32
    # hex 字符串
    int(result.content_id, 16)


def test_tts_synthesize_returns_metadata() -> None:
    """元数据含 wpm/voice/voice_id/engine/duration_ms（验收 #1）."""
    result = tts_synthesize("一段测试文本", "M")
    md = result.metadata
    assert md["wpm"] == 140
    assert md["voice"] == "female_standard"  # M 段默认音色
    assert md["voice_id"] == "voice-female-standard"
    assert md["engine"] == "mock"
    assert md["duration_ms"] > 0
    assert md["text_length"] == 6
    assert md["grade_band"] == "M"


# ── 验收 #2：学段语速配置 ─────────────────────────────────────────

def test_grade_band_l_slow_speed() -> None:
    """L(1-2) 慢速 120 wpm."""
    result = tts_synthesize("测试", "L")
    assert result.metadata["wpm"] == 120
    assert result.metadata["grade_band"] == "L"


def test_grade_band_m_medium_speed() -> None:
    """M(3-4) 中速 140 wpm."""
    result = tts_synthesize("测试", "M")
    assert result.metadata["wpm"] == 140


def test_grade_band_h_normal_speed() -> None:
    """H(5-6) 常速 160 wpm."""
    result = tts_synthesize("测试", "H")
    assert result.metadata["wpm"] == 160


def test_grade_band_unknown_raises() -> None:
    """未知学段报 ValueError."""
    with pytest.raises(ValueError):
        tts_synthesize("测试", "X")  # type: ignore[arg-type]


def test_grade_band_default_voice() -> None:
    """每个学段有默认音色（L=female_gentle / M=female_standard / H=male_standard）."""
    for band, expected_voice in [
        ("L", "female_gentle"),
        ("M", "female_standard"),
        ("H", "male_standard"),
    ]:
        result = tts_synthesize("测试", band)
        assert result.metadata["voice"] == expected_voice


# ── 验收 #3：相同文本+相同配置返回相同 content_id（缓存命中） ──────

def test_same_text_config_same_content_id() -> None:
    """相同文本+相同学段+相同音色 → 相同 content_id（D3 确定性）."""
    r1 = tts_synthesize("同一段文本", "M")
    r2 = tts_synthesize("同一段文本", "M")
    assert r1.content_id == r2.content_id


def test_different_text_different_content_id() -> None:
    """不同文本 → 不同 content_id."""
    r1 = tts_synthesize("文本A", "M")
    r2 = tts_synthesize("文本B", "M")
    assert r1.content_id != r2.content_id


def test_different_grade_band_different_content_id() -> None:
    """相同文本+不同学段 → 不同 content_id（wpm 不同）."""
    r1 = tts_synthesize("同一段文本", "L")
    r2 = tts_synthesize("同一段文本", "H")
    assert r1.content_id != r2.content_id


def test_different_voice_different_content_id() -> None:
    """相同文本+相同学段+不同音色 → 不同 content_id."""
    r1 = tts_synthesize("文本", "M", "female_standard")
    r2 = tts_synthesize("文本", "M", "female_news")
    assert r1.content_id != r2.content_id


# ── 验收 #3：缓存命中（不重复调用引擎） ───────────────────────────

def test_cache_hit_skips_engine_call() -> None:
    """相同 content_id 命中缓存，引擎不被重复调用."""
    call_count = {"n": 0}

    class _CountingEngine:
        ENGINE_NAME = "mock"

        def synthesize(self, text: str, *, voice_id: str, wpm: int) -> bytes:
            call_count["n"] += 1
            return f"audio:{voice_id}:{wpm}:{text}".encode("utf-8")

    engine = _CountingEngine()
    r1 = tts_synthesize("缓存的文本", "M", engine=engine)
    assert call_count["n"] == 1
    r2 = tts_synthesize("缓存的文本", "M", engine=engine)
    assert call_count["n"] == 1, "缓存命中不应重复调用引擎"
    assert r1.content_id == r2.content_id
    assert r1.audio == r2.audio


def test_cache_miss_different_text_calls_engine() -> None:
    """不同文本缓存未命中，引擎被调用."""
    call_count = {"n": 0}

    class _CountingEngine:
        ENGINE_NAME = "mock"

        def synthesize(self, text: str, *, voice_id: str, wpm: int) -> bytes:
            call_count["n"] += 1
            return b"x"

    engine = _CountingEngine()
    tts_synthesize("文本A", "M", engine=engine)
    tts_synthesize("文本B", "M", engine=engine)
    assert call_count["n"] == 2


# ── 验收 #4：mock TTS 引擎 ────────────────────────────────────────

def test_mock_engine_returns_deterministic_bytes() -> None:
    """MockTTSEngine 返回确定性字节（含 voice_id/wpm/text）."""
    engine = MockTTSEngine()
    audio = engine.synthesize("hello", voice_id="voice-x", wpm=140)
    assert audio == b"audio:voice-x:140:hello"


def test_mock_engine_protocol_satisfied() -> None:
    """MockTTSEngine 满足 TTSEngine Protocol（鸭子类型）."""
    engine = MockTTSEngine()
    assert callable(engine.synthesize)


# ── 验收 #5：不 import 学科包/学段包 ───────────────────────────────

def test_no_subject_pack_imports_in_tts() -> None:
    """src/core/ai/tts/ 禁止 import 学科包/学段包（宪法 A5/X6）."""
    tts_dir = (
        Path(__file__).resolve().parent.parent.parent
        / "src"
        / "core"
        / "ai"
        / "tts"
    )
    assert tts_dir.is_dir()
    pattern = re.compile(
        r"^\s*(?:from\s+(?:packs|src\.packs)"
        r"|import\s+(?:packs|src\.packs))",
        re.MULTILINE,
    )
    violations: list[str] = []
    for py_file in sorted(tts_dir.rglob("*.py")):
        text = py_file.read_text(encoding="utf-8")
        if pattern.findall(text):
            violations.append(str(py_file.relative_to(tts_dir)))
    assert not violations, f"ai/tts 存在学科包 import（违反 A5）：{violations}"


# ── 内容寻址 id 计算单元 ──────────────────────────────────────────

def test_compute_content_id_deterministic() -> None:
    """compute_content_id 确定性：相同输入相同输出."""
    cid1 = compute_content_id("text", "voice", 120, "mock")
    cid2 = compute_content_id("text", "voice", 120, "mock")
    assert cid1 == cid2


def test_compute_content_id_engine_sensitive() -> None:
    """engine 不同 → content_id 不同（不同引擎音频质量不同）."""
    cid1 = compute_content_id("text", "voice", 120, "mock")
    cid2 = compute_content_id("text", "voice", 120, "azure-tts")
    assert cid1 != cid2


# ── 时长估算 ──────────────────────────────────────────────────────

def test_estimate_duration_ms() -> None:
    """时长估算：len(text) * 60000 / wpm."""
    assert estimate_duration_ms("12345", 120) == int(5 * 60000 / 120)
    assert estimate_duration_ms("", 120) == 0
    assert estimate_duration_ms("x", 0) == 0  # wpm=0 防除零


# ── voice_profiles.yaml 配置完整性 ────────────────────────────────

def test_profiles_have_three_grade_bands() -> None:
    """voice_profiles.yaml 含 L/M/H 三学段（验收 #2）."""
    profiles = get_profiles()
    assert set(profiles["grade_bands"].keys()) == {"L", "M", "H"}
    assert profiles["grade_bands"]["L"]["wpm"] == 120
    assert profiles["grade_bands"]["M"]["wpm"] == 140
    assert profiles["grade_bands"]["H"]["wpm"] == 160


def test_profiles_have_multiple_voices() -> None:
    """voice_profiles.yaml 含多个音色（多音色配置，架构 §4.6）."""
    profiles = get_profiles()
    voices = profiles["voices"]
    assert len(voices) >= 3, "至少 3 个音色"
    for vname, vcfg in voices.items():
        assert "voice_id" in vcfg, f"音色 {vname} 缺 voice_id"
        assert "engine" in vcfg, f"音色 {vname} 缺 engine"


def test_unknown_voice_profile_raises() -> None:
    """未知 voice_profile 报 ValueError."""
    with pytest.raises(ValueError):
        tts_synthesize("测试", "M", "nonexistent_voice")
