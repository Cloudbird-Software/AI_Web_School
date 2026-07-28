"""T-W2-009 通用验证器 v1 + T-W4-014 语篇验证器.

三个平台通用验证器（架构 v2 §4.3 学科验证器清单·通用平台）：
- SchemaValidator：结构 / JSON Schema 校验。
- LicenseValidator：素材/语料许可校验（存在 + approved + 未过期）。
- DuplicatePlaceholderValidator：规范化哈希查重占位（重复提示 review，不阻断）。

T-W4-014 语篇专用验证器（pack_id='platform'，语篇为跨学科通用产物类型）：
- PassageFactCheckValidator：事实安全（政治敏感词/暴力词/常识转人工）。
- PassageAgeAppropriateValidator：适龄性（学段×体裁/主题/句长）。
- PassageDifficultyGateValidator：难度一致性（oov_rate 与目标区间比对）。

本模块 import 时注册覆盖 T-W2-008 loader.py 中的桩声明。

宪法 A5/X6：核心域零学科特判。
"""
from src.core.gate.validators.generic import (
    DuplicatePlaceholderValidator,
    LicenseValidator,
    SchemaValidator,
)
from src.core.gate.validators.passage_age_appropriate import (
    PassageAgeAppropriateValidator,
)
from src.core.gate.validators.passage_difficulty_gate import (
    PassageDifficultyGateValidator,
)
from src.core.gate.validators.passage_fact_check import (
    PassageFactCheckValidator,
)

__all__ = [
    "DuplicatePlaceholderValidator",
    "LicenseValidator",
    "PassageAgeAppropriateValidator",
    "PassageDifficultyGateValidator",
    "PassageFactCheckValidator",
    "SchemaValidator",
]
