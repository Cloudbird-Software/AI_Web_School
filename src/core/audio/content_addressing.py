"""T-W4-022 音频素材内容寻址（架构 v2 §4.6 / 宪法 D3）.

D3 内容寻址身份：同一内容必得同一 id。音频素材的「内容」由四个维度唯一决定：
    audio_id = H(text, voice_profile, speed, engine_digest)

为什么纳入 engine_digest：不同 TTS 引擎合成质量不同，即使文本/音色/语速相同，
音频内容也不同——engine 是内容寻址的一部分（与 T-W4-011 compute_content_id
同语义，确保产线 id 与总线 id 一致，便于回溯）。

为什么 speed 而非 wpm：speed 是学段配置的语速（wpm），此处用通用名 speed
表达「语速参数」，落值为 wpm 整数。命名解耦避免音频层硬绑 TTS 术语。

本模块与 T-W4-011 的 compute_content_id 保持同公式（text|voice|wpm|engine），
使 produce_audio 产出的 audio_id 与 tts_synthesize 返回的 content_id 一致——
码内可回溯（架构 §4.6）。
"""
from __future__ import annotations

import hashlib


def compute_audio_content_id(
    text: str, voice_profile: str, speed: int, engine_digest: str
) -> str:
    """音频素材内容寻址 id（D3）.

    公式：sha256("{text}|{voice_profile}|{speed}|{engine_digest}") 前 32 位 hex。

    Args:
        text: 已剥离 PII 的合成文本（D7：PII 剥离由上层调用方负责）。
        voice_profile: 音色名（如 female_standard，对应 voice_profiles.yaml）。
        speed: 语速 wpm（学段配置：L=120/M=140/H=160）。
        engine_digest: 引擎标识（如 'mock'；生产为真实适配器注册名）。

    Returns:
        32 位 hex 字符串（与 T-W4-011 compute_content_id 同公式同结果）。

    Notes:
        相同输入返回相同 id；任何参数变更（text/voice/speed/engine）产生新 id。
    """
    payload = f"{text}|{voice_profile}|{speed}|{engine_digest}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def compute_content_hash(audio: bytes) -> str:
    """音频字节流的规范化哈希（对象存储去重与完整性校验用）.

    为什么与 audio_id 分离：audio_id 是「输入参数寻址」（同参数同 id，命中缓存）；
    content_hash 是「产物字节寻址」（同字节同哈希，用于存储层去重与校验完整性）。
    两者维度不同——同 audio_id 必同 content_hash（确定性合成），但同 content_hash
    可能来自不同参数（理论上不会，但分离语义更清晰）。

    Returns:
        'sha256:' + 64 位 hex（与 gate validators 的 _canonical_hash 前缀风格一致）。
    """
    return "sha256:" + hashlib.sha256(audio).hexdigest()


__all__ = ["compute_audio_content_id", "compute_content_hash"]
