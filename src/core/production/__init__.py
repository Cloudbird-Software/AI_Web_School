"""生产线域（架构 v2 §4.1 四条对等生产线）.

- B 线语料装配线（T-W2-017）：框架模板（结构参数化）+ 语料库填充 → 统一 ItemVersion dict。
- D 线命题工坊（T-W4-017）：命题蓝图 + 量规模板数据化，被 D 线流水线（T-W4-021）
  与 AI 量规评分器（T-W4-019）消费。
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
from src.core.production.blueprint_schema import (
    Blueprint,
    GradeBandSpec,
    WritingType,
    make_blueprint,
)
from src.core.production.rubric_template import (
    GradeBand,
    RubricDimension,
    RubricLevel,
    RubricTemplate,
)

__all__ = [
    "BAssemblerError",
    "Blueprint",
    "BlockSpec",
    "CorpusRef",
    "FrameworkTemplate",
    "GradeBand",
    "GradeBandSpec",
    "MissingCorpusError",
    "RubricDimension",
    "RubricLevel",
    "RubricTemplate",
    "SlotSpec",
    "SlotValidationError",
    "WritingType",
    "assemble",
    "make_blueprint",
]
