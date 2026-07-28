"""T-W4-022 TTS 音频产线 + 内容寻址版本化 单元测试.

验收对照：
  #1 produce_audio 返回音频素材对象（url/content_hash/duration/tts_metadata）
  #2 内容寻址 id：相同输入相同 id；任何参数变更产生新 id
  #3 批量生产 produce_audio_batch 异步处理，返回任务状态与结果列表
  #4 make accept 全绿；使用 mock TTS
  #5 不 import 学科包/学段包

测试隔离：每个测试前 clear_cache()，避免 TTS 总线缓存污染内容寻址断言。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from src.core.ai.tts import clear_cache
from src.core.audio import (
    AudioAsset,
    AudioProduceJob,
    BatchResult,
    MockAudioStorageWriter,
    compute_audio_content_id,
    compute_content_hash,
    produce_audio,
    produce_audio_batch,
)


@pytest.fixture(autouse=True)
def _clear_tts_cache() -> None:
    """每个测试前后清空 TTS 缓存，隔离内容寻址验证."""
    clear_cache()
    yield
    clear_cache()


# ── 验收 #1：返回音频素材对象 ──────────────────────────────────────


def test_produce_audio_returns_audio_asset() -> None:
    """produce_audio 返回 AudioAsset 实例."""
    asset = produce_audio("Hello world", None, "M")
    assert isinstance(asset, AudioAsset)


def test_produce_audio_has_url() -> None:
    """素材含 url（对象存储可访问地址）."""
    asset = produce_audio("一段听力文本", None, "L")
    assert asset.url.startswith("http://")
    assert asset.audio_id in asset.url


def test_produce_audio_has_content_hash() -> None:
    """素材含 content_hash（音频字节流哈希，sha256 前缀）."""
    asset = produce_audio("文本", None, "M")
    assert asset.content_hash.startswith("sha256:")
    assert len(asset.content_hash) == len("sha256:") + 64


def test_produce_audio_has_duration() -> None:
    """素材含 duration_ms（正数）."""
    asset = produce_audio("一段有一定长度的听力文本", None, "H")
    assert asset.duration_ms > 0


def test_produce_audio_has_tts_metadata() -> None:
    """素材含 tts_metadata（wpm/voice/voice_id/engine/grade_band）."""
    asset = produce_audio("听力文本", None, "M")
    md = asset.tts_metadata
    assert md["wpm"] == 140
    assert md["voice"] == "female_standard"
    assert md["voice_id"] == "voice-female-standard"
    assert md["engine"] == "mock"
    assert md["grade_band"] == "M"


def test_produce_audio_has_audio_id() -> None:
    """素材含 audio_id（32 位 hex，内容寻址）."""
    asset = produce_audio("文本", None, "M")
    assert isinstance(asset.audio_id, str)
    assert len(asset.audio_id) == 32
    int(asset.audio_id, 16)  # 合法 hex


# ── 验收 #2：内容寻址 id ──────────────────────────────────────────


def test_same_inputs_same_audio_id() -> None:
    """相同 text+voice+grade_band → 相同 audio_id（D3 确定性）."""
    a1 = produce_audio("同一段听力文本", None, "M")
    a2 = produce_audio("同一段听力文本", None, "M")
    assert a1.audio_id == a2.audio_id
    assert a1.url == a2.url
    assert a1.content_hash == a2.content_hash


def test_different_text_different_audio_id() -> None:
    """不同文本 → 不同 audio_id."""
    a1 = produce_audio("文本A", None, "M")
    a2 = produce_audio("文本B", None, "M")
    assert a1.audio_id != a2.audio_id


def test_different_grade_band_different_audio_id() -> None:
    """相同文本+不同学段 → 不同 audio_id（wpm 不同）."""
    a1 = produce_audio("同一段文本", None, "L")
    a2 = produce_audio("同一段文本", None, "H")
    assert a1.audio_id != a2.audio_id


def test_different_voice_different_audio_id() -> None:
    """相同文本+学段+不同音色 → 不同 audio_id."""
    a1 = produce_audio("文本", "female_standard", "M")
    a2 = produce_audio("文本", "female_news", "M")
    assert a1.audio_id != a2.audio_id


def test_audio_id_matches_formula() -> None:
    """audio_id 等于 compute_audio_content_id 公式结果（码内可回溯）."""
    asset = produce_audio("回溯用文本", "female_standard", "M")
    expected = compute_audio_content_id(
        "回溯用文本", "female_standard", 140, "mock"
    )
    assert asset.audio_id == expected


def test_audio_id_matches_tts_bus_content_id() -> None:
    """产线 audio_id 与 TTS 总线 content_id 一致（D3 一致性）."""
    from src.core.ai.tts import tts_synthesize

    text = "一致性校验文本"
    tts_result = tts_synthesize(text, "M", "female_standard")
    asset = produce_audio(text, "female_standard", "M")
    assert asset.audio_id == tts_result.content_id


def test_compute_audio_content_id_param_change() -> None:
    """公式：任一参数变更产生新 id."""
    base = compute_audio_content_id("t", "v", 120, "mock")
    assert compute_audio_content_id("t2", "v", 120, "mock") != base
    assert compute_audio_content_id("t", "v2", 120, "mock") != base
    assert compute_audio_content_id("t", "v", 140, "mock") != base
    assert compute_audio_content_id("t", "v", 120, "azure-tts") != base


def test_content_hash_deterministic() -> None:
    """compute_content_hash 对相同字节返回相同哈希."""
    h1 = compute_content_hash(b"audio-bytes")
    h2 = compute_content_hash(b"audio-bytes")
    assert h1 == h2
    assert compute_content_hash(b"different") != h1


# ── 验收 #3：批量生产 ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_produce_audio_batch_completed() -> None:
    """批量生产：全部成功 → status=completed，结果按序返回."""
    jobs = [
        AudioProduceJob(text="第一句听力", grade_band="L"),
        AudioProduceJob(text="第二句听力", grade_band="M"),
        AudioProduceJob(text="第三句听力", grade_band="H"),
    ]
    result = await produce_audio_batch(jobs)
    assert isinstance(result, BatchResult)
    assert result.status == "completed"
    assert result.total == 3
    assert result.succeeded == 3
    assert result.failed == 0
    assert len(result.results) == 3
    assert all(isinstance(a, AudioAsset) for a in result.results)


@pytest.mark.asyncio
async def test_produce_audio_batch_order_preserved() -> None:
    """批量结果顺序与 jobs 顺序一致（便于调用方按 index 对齐）."""
    jobs = [
        AudioProduceJob(text="AAA", grade_band="M"),
        AudioProduceJob(text="BBB", grade_band="M"),
    ]
    result = await produce_audio_batch(jobs)
    assert result.results[0].text == "AAA"
    assert result.results[1].text == "BBB"


@pytest.mark.asyncio
async def test_produce_audio_batch_empty() -> None:
    """空批量 → completed，0 结果."""
    result = await produce_audio_batch([])
    assert result.status == "completed"
    assert result.total == 0
    assert result.succeeded == 0
    assert result.results == []


@pytest.mark.asyncio
async def test_produce_audio_batch_distinct_ids() -> None:
    """批量内不同文本产出不同 audio_id."""
    jobs = [
        AudioProduceJob(text="unique-1", grade_band="M"),
        AudioProduceJob(text="unique-2", grade_band="M"),
    ]
    result = await produce_audio_batch(jobs)
    ids = {a.audio_id for a in result.results}
    assert len(ids) == 2


# ── 验收 #4：mock TTS + 可注入 writer ─────────────────────────────


def test_mock_storage_writer_deterministic_url() -> None:
    """MockAudioStorageWriter 返回确定性 URL（同 id 同 URL）."""
    w = MockAudioStorageWriter()
    url1 = w.write("abc123", b"x")
    url2 = w.write("abc123", b"y")
    assert url1 == url2
    assert url1 == "http://localhost:9000/audio-listening/abc123.mp3"


def test_produce_audio_injected_writer() -> None:
    """produce_audio 支持注入自定义 writer."""

    class _CapturingWriter:
        def __init__(self) -> None:
            self.written: list[tuple[str, bytes]] = []

        def write(self, audio_id: str, audio: bytes) -> str:
            self.written.append((audio_id, audio))
            return f"custom://{audio_id}"

    cap = _CapturingWriter()
    asset = produce_audio("注入测试", None, "M", writer=cap)
    assert asset.url.startswith("custom://")
    assert len(cap.written) == 1
    assert cap.written[0][0] == asset.audio_id


# ── 验收 #5：不 import 学科包/学段包 ──────────────────────────────


def test_no_subject_pack_imports_in_audio() -> None:
    """src/core/audio/ 禁止 import 学科包/学段包（宪法 A5/X6）."""
    audio_dir = (
        Path(__file__).resolve().parent.parent.parent
        / "src"
        / "core"
        / "audio"
    )
    assert audio_dir.is_dir()
    pattern = re.compile(
        r"^\s*(?:from\s+(?:packs|src\.packs)"
        r"|import\s+(?:packs|src\.packs))",
        re.MULTILINE,
    )
    violations: list[str] = []
    for py_file in sorted(audio_dir.rglob("*.py")):
        text = py_file.read_text(encoding="utf-8")
        if pattern.findall(text):
            violations.append(str(py_file.relative_to(audio_dir)))
    assert not violations, f"audio 存在学科包 import（违反 A5）：{violations}"
