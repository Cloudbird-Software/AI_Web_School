"""T-W2-009 通用验证器 v1 + T-W4-023 音频校验器.

三个平台通用验证器（架构 v2 §4.3 学科验证器清单·通用平台）：
- SchemaValidator：结构 / JSON Schema 校验。
- LicenseValidator：素材/语料许可校验（存在 + approved + 未过期）。
- DuplicatePlaceholderValidator：规范化哈希查重占位（重复提示 review，不阻断）。

T-W4-023 音频素材专用验证器（架构 v2 §4.6，S5 听力链）：
- AudioAgeCheckValidator：语速适龄（L 120±12 / M 140±14 / H 160±16 wpm）。
- AudioQualityValidator：发音正确性（v1 文本匹配占位，可扩展 ASR 回译）。

本模块 import 时注册覆盖 T-W2-008 loader.py 中的桩声明。

宪法 A5/X6：核心域零学科特判。
"""
from src.core.gate.validators.audio_age_check import (
    AudioAgeCheckValidator,
    GRADE_BAND_TARGET_WPM,
    TOLERANCE_PCT,
)
from src.core.gate.validators.audio_quality import AudioQualityValidator
from src.core.gate.validators.generic import (
    DuplicatePlaceholderValidator,
    LicenseValidator,
    SchemaValidator,
)

__all__ = [
    "DuplicatePlaceholderValidator",
    "LicenseValidator",
    "SchemaValidator",
    "AudioAgeCheckValidator",
    "AudioQualityValidator",
    "GRADE_BAND_TARGET_WPM",
    "TOLERANCE_PCT",
]
