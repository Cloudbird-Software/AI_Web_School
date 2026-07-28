"""T-W4-022 英语听力音频产线（架构 v2 §4.6 / S5）.

音频产线 = TTS 总线合成（T-W4-011）→ 内容寻址版本化音频素材 → 对象存储。
本包是 S5 听力链的源头：产出可被校验门（T-W4-023）、消费层（T-W4-024）、
组卷 overlay（T-W4-025）、端到端（T-W4-026）复用的音频素材对象。

宪法 A5/X6：核心域零学科特判——本包不 import 任何学科包/学段包；
学段参数经 TTS 总线的 voice_profiles.yaml 注入（T-W4-011）。
宪法 D3：内容寻址——相同文本+相同配置得相同 id（content_addressing 落地）。
"""
from src.core.audio.content_addressing import (
    compute_audio_content_id,
    compute_content_hash,
)
from src.core.audio.producer import (
    AudioAsset,
    AudioProduceJob,
    AudioStorageWriter,
    BatchResult,
    MockAudioStorageWriter,
    produce_audio,
    produce_audio_batch,
)

__all__ = [
    "AudioAsset",
    "AudioProduceJob",
    "AudioStorageWriter",
    "BatchResult",
    "MockAudioStorageWriter",
    "compute_audio_content_id",
    "compute_content_hash",
    "produce_audio",
    "produce_audio_batch",
]
