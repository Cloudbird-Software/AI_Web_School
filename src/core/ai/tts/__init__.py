"""T-W4-011 TTS 总线档 + 多音色语速学段配置.

经 AI 总线统一调用 TTS（架构 v2 §4.6/§4.8），支持多音色、语速按学段配置
（低段慢速/中段中速/高段常速）。输出音频素材内容寻址、版本化，为 S5 英语
听力产线提供语音合成服务。

宪法 A5：本包不 import 学科包；学段参数通过 voice_profiles.yaml 配置注入。
宪法 D3：内容寻址——相同文本+相同配置得相同 id（确定性）。
"""
from src.core.ai.tts.router import (
    MockTTSEngine,
    TTSResult,
    TTSEngine,
    clear_cache,
    tts_synthesize,
)

__all__ = [
    "MockTTSEngine",
    "TTSResult",
    "TTSEngine",
    "clear_cache",
    "tts_synthesize",
]
