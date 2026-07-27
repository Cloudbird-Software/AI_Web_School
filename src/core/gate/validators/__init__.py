"""T-W2-009 通用验证器 v1.

三个平台通用验证器（架构 v2 §4.3 学科验证器清单·通用平台）：
- SchemaValidator：结构 / JSON Schema 校验。
- LicenseValidator：素材/语料许可校验（存在 + approved + 未过期）。
- DuplicatePlaceholderValidator：规范化哈希查重占位（重复提示 review，不阻断）。

本模块 import 时注册覆盖 T-W2-008 loader.py 中的桩声明。

宪法 A5/X6：核心域零学科特判。
"""
from src.core.gate.validators.generic import (
    DuplicatePlaceholderValidator,
    LicenseValidator,
    SchemaValidator,
)

__all__ = [
    "DuplicatePlaceholderValidator",
    "LicenseValidator",
    "SchemaValidator",
]
