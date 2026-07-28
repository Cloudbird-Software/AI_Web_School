"""T-W4-022 TTS 音频产线 + 内容寻址版本化（架构 v2 §4.6 / S5）.

产线职责：
- produce_audio：经 TTS 总线（T-W4-011 tts_synthesize）合成音频 → 包装为音频素材
  对象（url/content_hash/duration/tts_metadata），写入对象存储；
- produce_audio_batch：异步批量生产，返回任务状态与结果列表。

为什么 audio_id 直接复用 tts_synthesize 的 content_id：D3 内容寻址——相同文本+
相同配置得相同 id。tts_synthesize 已按 H(text|voice|wpm|engine) 计算 content_id
（与 content_addressing.compute_audio_content_id 同公式），产线直接透传，保证
「总线 id == 产线 id == 存储寻址」，码内可回溯（架构 §4.6）。

为什么 writer 可注入：对象存储是副作用（写 MinIO/本地），单元测试用
MockAudioStorageWriter 返回确定性 URL，不触达真实存储；生产替换为 MinIO 适配器。

宪法 A5/X6：不 import 学科包/学段包；学段参数经 voice_profiles.yaml 注入（T-W4-011）。
宪法 D7：text 应已剥离 PII（剥离由上层调用方负责，本模块不感知 PII 语义）。
"""
from __future__ import annotations

import asyncio
from typing import Any, Literal, Optional, Protocol

from pydantic import BaseModel, ConfigDict, Field

from src.core.ai.tts import (
    MockTTSEngine,
    TTSResult,
    TTSEngine,
    tts_synthesize,
)
from src.core.audio.content_addressing import (
    compute_audio_content_id,
    compute_content_hash,
)

GradeBand = Literal["L", "M", "H"]


# ════════════════════════════════════════════════════════════════════
# 对象存储写入器（可注入的副作用边界）
# ════════════════════════════════════════════════════════════════════


class AudioStorageWriter(Protocol):
    """音频对象存储写入契约（生产替换为 MinIO/S3 适配器）."""

    def write(self, audio_id: str, audio: bytes) -> str:
        """写入音频字节，返回可访问 URL（内容寻址：同 audio_id 幂等）."""
        ...


class MockAudioStorageWriter:
    """桩存储写入器：不触达真实存储，返回确定性 URL（验收 #4：mock）.

    URL 设计：http://localhost:9000/audio-listening/{audio_id}.mp3
    ——audio_id 是内容寻址 id，同 id 同 URL（幂等），便于消费层（T-W4-024）
    按_id 定位音频。
    """

    BUCKET = "audio-listening"
    BASE_URL = "http://localhost:9000"

    def write(self, audio_id: str, audio: bytes) -> str:
        # 为什么不真正写 MinIO：单元测试需 hermetic；真实存储写入由生产适配器负责。
        # bytes 入参保留契约形态（生产适配器会真正 upload）。
        del audio  # mock 不读字节，仅按 id 生成 URL
        return f"{self.BASE_URL}/{self.BUCKET}/{audio_id}.mp3"


# ════════════════════════════════════════════════════════════════════
# 音频素材对象（产线产物）
# ════════════════════════════════════════════════════════════════════


class AudioAsset(BaseModel):
    """音频素材对象（验收 #1：url/content_hash/duration/tts_metadata）.

    - audio_id：内容寻址 id（D3，与 TTS 总线 content_id 一致）。
    - url：对象存储可访问 URL（消费层播放/二维码/点读的源头）。
    - content_hash：音频字节流哈希（存储去重与完整性校验）。
    - duration_ms：音频时长（毫秒，TTS 总线估算）。
    - tts_metadata：TTS 合成元数据（wpm/voice/voice_id/engine/grade_band 等）。
    - text/voice_profile/grade_band：溯源字段（便于校验门与组卷引用）。
    """

    model_config = ConfigDict(extra="forbid")

    audio_id: str
    url: str
    content_hash: str
    duration_ms: int
    tts_metadata: dict[str, Any]
    text: str
    voice_profile: str
    grade_band: GradeBand
    # 引擎实际产出的字节（消费层点读/播放需要；存储后可置空以释放内存）
    audio: bytes = b""


# ════════════════════════════════════════════════════════════════════
# 批量生产
# ════════════════════════════════════════════════════════════════════


class AudioProduceJob(BaseModel):
    """批量生产任务单元."""

    model_config = ConfigDict(extra="forbid")

    text: str
    voice_profile: Optional[str] = None  # None → 学段默认音色
    grade_band: GradeBand


class BatchFailure(BaseModel):
    """单个任务失败记录（不中断整批，记录后继续）."""

    model_config = ConfigDict(extra="forbid")

    job_index: int
    text: str
    error: str


class BatchResult(BaseModel):
    """批量生产结果：任务状态 + 成功产物列表 + 失败列表.

    - status：'completed'（全部成功）/ 'partial'（部分失败）。
    - results：成功的 AudioAsset 列表（按 job 顺序）。
    - failures：失败的 BatchFailure 列表（含错误原因）。
    """

    model_config = ConfigDict(extra="forbid")

    status: Literal["completed", "partial"]
    results: list[AudioAsset] = Field(default_factory=list)
    failures: list[BatchFailure] = Field(default_factory=list)
    total: int
    succeeded: int
    failed: int


# ════════════════════════════════════════════════════════════════════
# 公共入口
# ════════════════════════════════════════════════════════════════════


def produce_audio(
    text: str,
    voice_profile: Optional[str],
    grade_band: GradeBand,
    *,
    engine: Optional[TTSEngine] = None,
    writer: Optional[AudioStorageWriter] = None,
) -> AudioAsset:
    """合成单个音频素材（验收 #1/#2）.

    Args:
        text: 待合成文本（应已剥离 PII，D7）。
        voice_profile: 音色名（None → 学段默认音色，由 TTS 总线解析）。
        grade_band: 学段 L/M/H，决定语速与默认音色。
        engine: TTS 引擎（None → MockTTSEngine，验收 #4）。
        writer: 对象存储写入器（None → MockAudioStorageWriter）。

    Returns:
        AudioAsset：含 audio_id/url/content_hash/duration_ms/tts_metadata。
    """
    tts: TTSResult = tts_synthesize(
        text, grade_band, voice_profile, engine=engine
    )
    md = tts.metadata
    w = writer if writer is not None else MockAudioStorageWriter()
    url = w.write(tts.content_id, tts.audio)

    # 防御性断言：产线 id 必须与总线 id 一致（D3 一致性，码内可回溯）
    expected_id = compute_audio_content_id(
        text, md["voice"], md["wpm"], md["engine"]
    )
    if tts.content_id != expected_id:
        raise RuntimeError(
            "内容寻址 id 不一致：产线公式与 TTS 总线结果分歧"
            f"（tts={tts.content_id} != producer={expected_id}）"
        )

    return AudioAsset(
        audio_id=tts.content_id,
        url=url,
        content_hash=compute_content_hash(tts.audio),
        duration_ms=int(md["duration_ms"]),
        tts_metadata=dict(md),
        text=text,
        voice_profile=md["voice"],
        grade_band=grade_band,
        audio=tts.audio,
    )


async def produce_audio_batch(
    jobs: list[AudioProduceJob],
    *,
    engine: Optional[TTSEngine] = None,
    writer: Optional[AudioStorageWriter] = None,
) -> BatchResult:
    """异步批量生产音频素材（验收 #3）.

    用 asyncio.to_thread 并发执行同步 produce_audio（TTS 合成是 CPU/IO 混合，
    to_thread 释放事件循环阻塞）。任一任务失败不中断整批，记录到 failures。

    Args:
        jobs: 批量任务单元列表。
        engine / writer：透传给 produce_audio（同批共享引擎与存储写入器）。

    Returns:
        BatchResult：status + results + failures + 计数。
    """
    eng = engine if engine is not None else MockTTSEngine()
    w = writer if writer is not None else MockAudioStorageWriter()

    async def _run(idx: int, job: AudioProduceJob) -> tuple[int, AudioAsset | Exception]:
        try:
            asset = await asyncio.to_thread(
                produce_audio,
                job.text,
                job.voice_profile,
                job.grade_band,
                engine=eng,
                writer=w,
            )
            return idx, asset
        except Exception as exc:  # noqa: BLE001 — 批量任务需容错继续
            return idx, exc

    outcomes = await asyncio.gather(*(_run(i, j) for i, j in enumerate(jobs)))

    indexed: dict[int, AudioAsset | Exception] = dict(outcomes)
    results: list[AudioAsset] = []
    failures: list[BatchFailure] = []
    for i in range(len(jobs)):
        out = indexed[i]
        if isinstance(out, AudioAsset):
            results.append(out)
        else:
            failures.append(
                BatchFailure(job_index=i, text=jobs[i].text, error=str(out))
            )

    status: Literal["completed", "partial"] = (
        "completed" if not failures else "partial"
    )
    return BatchResult(
        status=status,
        results=results,
        failures=failures,
        total=len(jobs),
        succeeded=len(results),
        failed=len(failures),
    )


__all__ = [
    "GradeBand",
    "AudioStorageWriter",
    "MockAudioStorageWriter",
    "AudioAsset",
    "AudioProduceJob",
    "BatchFailure",
    "BatchResult",
    "produce_audio",
    "produce_audio_batch",
]
