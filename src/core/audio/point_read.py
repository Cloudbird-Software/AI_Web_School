"""T-W4-024 低段题面点读：逐词/逐句播放接口（架构 v2 §4.6 / S5）.

低段（1–2 年级）听力题面支持点读：学生点击某个词，播放该词对应的音频片段。
本模块根据文本与音频时长估算每个词的时间戳范围，返回 (start_ms, end_ms)。

为什么是估算而非精确时间戳：MockTTSEngine 不产出 word-level timestamps，
真实 TTS 引擎（如 Azure）可返回 SSML word boundary——本模块设计为：
1. 若 tts_metadata 含 word_timings → 直接使用精确时间戳。
2. 否则 → 按文本长度均匀分配时间（估算）。
扩展点：未来 TTS 适配器在 tts_metadata 注入 word_timings 即生效，无需改本模块。

分词策略：
- 中文（含 CJK 字符）：每个字作为一个 word（低段点读粒度=单字）。
- 英文：按空格分词。
- 混合文本：CJK 部分逐字、非 CJK 部分按空格，保持原序。

宪法 A5/X6：不 import 学科包/学段包；分词规则是语言无关的字符级判断，
不依赖英语/语文学科包的语料库。
"""
from __future__ import annotations

import re
import unicodedata
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


# ════════════════════════════════════════════════════════════════════
# 异常
# ════════════════════════════════════════════════════════════════════


class PointReadError(Exception):
    """点读错误：word_index 越界或音频元数据缺失."""


# ════════════════════════════════════════════════════════════════════
# 点读结果
# ════════════════════════════════════════════════════════════════════


class PointReadResult(BaseModel):
    """点读结果（验收 #3）.

    - audio_id：音频素材 id。
    - word_index：请求的词序号（0-based）。
    - word：该位置的文本片段。
    - start_ms / end_ms：该词在音频中的时间戳范围（毫秒）。
    - audio_url：音频可访问 URL（客户端播放 [start_ms, end_ms] 片段）。
    - method：时间戳来源（'word_timings' 精确 / 'even_split' 估算）。
    """

    model_config = ConfigDict(extra="forbid")

    audio_id: str
    word_index: int = Field(ge=0)
    word: str
    start_ms: int = Field(ge=0)
    end_ms: int = Field(ge=0)
    audio_url: str
    method: str = Field(description="时间戳来源：word_timings / even_split")


# ════════════════════════════════════════════════════════════════════
# 分词
# ════════════════════════════════════════════════════════════════════

# CJK 统一表意文字范围检测（用于中文逐字分词）
# 为什么用 unicodedata 而非硬编码范围：NFKC 归一化后用 category 判断更稳健
_CJK_PATTERN = re.compile(
    r"[\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff"  # CJK 统一表意文字
    r"\u3040-\u309f\u30a0-\u30ff]"  # 平假名/片假名（日文兼容）
)


def _is_cjk(ch: str) -> bool:
    """判断字符是否为 CJK 字符（中文/日文）."""
    return bool(_CJK_PATTERN.match(ch))


def split_words(text: str) -> list[str]:
    """将文本分词（语言无关的字符级分词）.

    策略：
    - CJK 字符：逐字作为一个 word（低段点读粒度=单字）。
    - 非 CJK（英文/数字）：按空格分词，保留连续非空格非 CJK 序列。
    - 空白字符：跳过（不产生 word）。

    为什么 CJK 逐字：中文无空格分词，低段点读需要单字粒度（如「苹果」→「苹」「果」）。
    为什么英文按空格：英文单词以空格分隔，点读粒度=单词。

    Args:
        text: 待分词文本。

    Returns:
        词列表（保持原序，不含纯空白）。

    Examples:
        >>> split_words("苹果 banana")
        ['苹', '果', 'banana']
        >>> split_words("Hello World")
        ['Hello', 'World']
    """
    words: list[str] = []
    buffer: list[str] = []

    for ch in text:
        if _is_cjk(ch):
            # CJK 字符：先 flush buffer，再逐字入列
            if buffer:
                words.append("".join(buffer))
                buffer = []
            words.append(ch)
        elif ch.isspace():
            # 空白：flush buffer
            if buffer:
                words.append("".join(buffer))
                buffer = []
        else:
            # 非 CJK 非空白：累积到 buffer（英文单词/数字等）
            buffer.append(ch)

    # 尾部 flush
    if buffer:
        words.append("".join(buffer))

    return words


# ════════════════════════════════════════════════════════════════════
# 公共入口
# ════════════════════════════════════════════════════════════════════


def point_read(
    audio_id: str,
    word_index: int,
    *,
    text: str,
    duration_ms: int,
    audio_url: str,
    tts_metadata: dict[str, Any] | None = None,
) -> PointReadResult:
    """点读：返回指定词的音频时间戳范围（验收 #3）.

    流程：
    1. 分词（split_words）。
    2. word_index 越界 → 抛 PointReadError。
    3. 若 tts_metadata 含 word_timings → 用精确时间戳。
    4. 否则 → 按词数均匀分配 duration_ms（估算）。

    为什么接受 text/duration_ms 而非 AudioAsset：解耦——调用方从 AudioAsset
    传入所需字段，本模块不依赖 AudioAsset 类型（与 player_service 同模式）。

    Args:
        audio_id: 音频素材 id。
        word_index: 词序号（0-based）。
        text: 音频对应的原始文本（用于分词）。
        duration_ms: 音频总时长（毫秒，来自 AudioAsset.duration_ms）。
        audio_url: 音频 URL（来自 AudioAsset.url）。
        tts_metadata: TTS 元数据（可选，含 word_timings 时用精确时间戳）。

    Returns:
        PointReadResult：含 word / start_ms / end_ms / method。

    Raises:
        PointReadError: word_index 越界，或 text 为空，或 duration_ms ≤ 0。
    """
    if not text or not text.strip():
        raise PointReadError("text 为空，无法分词")
    if duration_ms <= 0:
        raise PointReadError(f"duration_ms={duration_ms} 非法（必须 > 0）")

    words = split_words(text)
    if word_index < 0 or word_index >= len(words):
        raise PointReadError(
            f"word_index={word_index} 越界（文本分词后共 {len(words)} 个词，"
            f"有效范围 0..{len(words) - 1}）"
        )

    word = words[word_index]

    # ── 精确时间戳路径（TTS 适配器注入 word_timings 时生效）──
    md = tts_metadata or {}
    word_timings: list[dict[str, Any]] | None = md.get("word_timings")
    if word_timings and word_index < len(word_timings):
        timing = word_timings[word_index]
        return PointReadResult(
            audio_id=audio_id,
            word_index=word_index,
            word=word,
            start_ms=int(timing["start_ms"]),
            end_ms=int(timing["end_ms"]),
            audio_url=audio_url,
            method="word_timings",
        )

    # ── 估算路径：均匀分配时间 ──
    # 每个词的时间片 = duration_ms / len(words)
    # 为什么用整除：毫秒级精度足够，浮点会引入不必要复杂度
    per_word_ms = duration_ms // len(words)
    start_ms = word_index * per_word_ms
    # 最后一个词取到音频末尾（避免整除丢尾）
    if word_index == len(words) - 1:
        end_ms = duration_ms
    else:
        end_ms = start_ms + per_word_ms

    return PointReadResult(
        audio_id=audio_id,
        word_index=word_index,
        word=word,
        start_ms=start_ms,
        end_ms=end_ms,
        audio_url=audio_url,
        method="even_split",
    )


def list_words(text: str) -> list[str]:
    """分词并返回词列表（点读 UI 展示用）.

    便于上层在渲染点读界面时获取所有可点词列表。
    """
    return split_words(text)


__all__ = [
    "PointReadError",
    "PointReadResult",
    "point_read",
    "split_words",
    "list_words",
]
