"""T-W4-022/024 英语听力音频产线 + 消费层（架构 v2 §4.6 / S5）.

音频产线 = TTS 总线合成（T-W4-011）→ 内容寻址版本化音频素材 → 对象存储。
消费层 = 在线播放器（限次）+ 卷面二维码（签名 URL）+ 低段点读（逐词时间戳）。
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
from src.core.audio.player_service import (
    MAX_PLAYS,
    InMemoryPlayCountStore,
    PlayCountStore,
    PlayLimitExceededError,
    PlayResult,
    play,
)
from src.core.audio.point_read import (
    PointReadError,
    PointReadResult,
    list_words,
    point_read,
    split_words,
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
from src.core.audio.qr_generator import (
    DEFAULT_BASE_URL,
    DEFAULT_VALIDITY_HOURS,
    QRSignedUrl,
    QRSignatureError,
    generate_qr,
    verify_qr_url,
)

__all__ = [
    # 产线（T-W4-022）
    "AudioAsset",
    "AudioProduceJob",
    "AudioStorageWriter",
    "BatchResult",
    "MockAudioStorageWriter",
    "compute_audio_content_id",
    "compute_content_hash",
    "produce_audio",
    "produce_audio_batch",
    # 消费层（T-W4-024）
    "MAX_PLAYS",
    "PlayCountStore",
    "InMemoryPlayCountStore",
    "PlayLimitExceededError",
    "PlayResult",
    "play",
    "DEFAULT_BASE_URL",
    "DEFAULT_VALIDITY_HOURS",
    "QRSignatureError",
    "QRSignedUrl",
    "generate_qr",
    "verify_qr_url",
    "PointReadError",
    "PointReadResult",
    "point_read",
    "split_words",
    "list_words",
]
