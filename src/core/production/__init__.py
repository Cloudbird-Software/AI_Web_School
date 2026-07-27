"""B 线语料装配线（T-W2-017）.

地位：架构 v2 §4.1 四条对等生产线之一的 B 线入口。
模式：框架模板（结构参数化）+ 语料库填充 → 统一 ItemVersion dict。
"""
from src.core.production.b_assembler import (
    BAssemblerError,
    BlockSpec,
    CorpusRef,
    FrameworkTemplate,
    MissingCorpusError,
    SlotSpec,
    SlotValidationError,
    assemble,
)

__all__ = [
    "BAssemblerError",
    "BlockSpec",
    "CorpusRef",
    "FrameworkTemplate",
    "MissingCorpusError",
    "SlotSpec",
    "SlotValidationError",
    "assemble",
]
