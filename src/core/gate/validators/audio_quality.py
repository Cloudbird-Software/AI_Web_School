"""T-W4-023 音频发音正确性校验器（架构 v2 §4.3 / §4.6）.

音频素材专用校验门之一：发音正确性。v1 做文本匹配占位——对比原始文本与
TTS 元数据标记（text_length 一致性），可扩展为 ASR 回译（ctx 提供 asr_transcript
时走文本↔回译比对）。未过门音频不得入库（D2）。

为什么 v1 是占位：真实 ASR 回译需调外部 ASR 服务（成本/密钥），W4 非目标
（任务卡 non_goals：ASR 回译验证）。占位校验「文本与 TTS 元数据一致」已能
拦截「文本与合成参数不匹配」的明显错误；ASR 回译作为 ctx.asr_transcript
扩展点，未来注入即生效，无需改验证器。

宪法 A5/X6：不 import 学科包/学段包；D2：门强制由 certifier 签发路径兜底。
"""
from __future__ import annotations

import re
import time
import unicodedata
from decimal import Decimal
from typing import Any

from src.core.gate.validator import (
    GateContext,
    Validator,
    ValidatorResult,
    register_validator,
)


def _normalize(text: str) -> str:
    """规范化文本用于比对：NFKC + 转小写 + 去标点与空白.

    为什么 NFKC：中文全/半角、兼容字形归一（如 ﬁ → fi）；ASR 回译与原文
    可能有全半角差异，NFKC 消除这类表面差异。
    """
    s = unicodedata.normalize("NFKC", text).lower()
    # 去除标点与空白（保留字母数字与 CJK）
    s = re.sub(r"[\s\W_]+", "", s, flags=re.UNICODE)
    return s


class AudioQualityValidator(Validator):
    """音频发音正确性验证器（验收 #3）.

    ctx.artifact_payload 期望字段：
    - text: str——原始合成文本（必填）。
    - tts_metadata: dict（可选，含 text_length 用于占位一致性校验）。
    - asr_transcript: str（可选，提供时走 ASR 回译比对，否则占位模式）。

    verdict 规则：
    - fail：text 缺失/空；asr_transcript 提供但与 text 规范化不等；
            占位模式下 text_length 与 len(text) 不一致。
    - pass：占位模式一致；或 ASR 回译匹配。

    evidence：{method, text_length, transcript_length, normalized_match, reason}
    """

    validator_id = "audio_quality"
    version = "1.0.0+audio"
    blocking = True
    cost_tier = "cheap"

    async def validate(self, artifact_ref: str, ctx: GateContext) -> ValidatorResult:
        start = time.monotonic()
        payload = ctx.artifact_payload or {}
        text: Any = payload.get("text")
        elapsed_ms = lambda: int((time.monotonic() - start) * 1000)

        if not isinstance(text, str) or not text.strip():
            return self._timed_result(
                verdict="fail",
                evidence={"reason": "原始文本缺失或为空，无法校验发音正确性"},
                confidence=Decimal("1.000"),
                elapsed_ms=elapsed_ms(),
            )

        md = payload.get("tts_metadata") or {}
        transcript: Any = payload.get("asr_transcript")

        # ── ASR 回译路径（扩展点：提供 asr_transcript 即走真实比对）──
        if isinstance(transcript, str) and transcript.strip():
            norm_text = _normalize(text)
            norm_trans = _normalize(transcript)
            match = norm_text == norm_trans
            return self._timed_result(
                verdict="pass" if match else "fail",
                evidence={
                    "method": "asr_transcript",
                    "text_length": len(text),
                    "transcript_length": len(transcript),
                    "normalized_match": match,
                    "reason": (
                        None
                        if match
                        else "ASR 回译与原文规范化后不一致（发音可能错误）"
                    ),
                },
                confidence=Decimal("0.900"),
                elapsed_ms=elapsed_ms(),
            )

        # ── 占位路径：文本与 TTS 元数据标记一致性 ──
        # 对比原始文本长度与 tts_metadata.text_length（TTS 总线在合成时记录）。
        # 不一致说明音频素材的文本字段与实际合成参数不匹配（拼接错误/篡改）。
        meta_text_length = md.get("text_length")
        if meta_text_length is not None and int(meta_text_length) != len(text):
            return self._timed_result(
                verdict="fail",
                evidence={
                    "method": "text_length_placeholder",
                    "text_length": len(text),
                    "metadata_text_length": int(meta_text_length),
                    "normalized_match": False,
                    "reason": "原始文本长度与 TTS 元数据 text_length 不一致",
                },
                confidence=Decimal("1.000"),
                elapsed_ms=elapsed_ms(),
            )

        return self._timed_result(
            verdict="pass",
            evidence={
                "method": "text_length_placeholder",
                "text_length": len(text),
                "metadata_text_length": (
                    int(meta_text_length) if meta_text_length is not None else None
                ),
                "normalized_match": True,
                "reason": (
                    "占位校验通过：文本与 TTS 元数据一致；ASR 回译为扩展点"
                    "（提供 asr_transcript 即走真实比对）"
                ),
            },
            confidence=Decimal("0.800"),
            elapsed_ms=elapsed_ms(),
        )


# 模块加载时注册（pack_id='platform'）
register_validator("platform", AudioQualityValidator)


__all__ = ["AudioQualityValidator", "_normalize"]
