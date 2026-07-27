"""受控变式引擎（T-W2-005）.

按母题 spec 的 variation_axes 取槽子集重采样，其余槽冻结；
产出 n 个实例 + VariantCertificate（目标不变性证据）。

两条建模纪律（ADR §4.1）：
  ①凡改变考查目标的参数必须拆母题，不得作为变式维度；
  ②优先"按构造必然合法"的生成器设计。

AI 自由改写产物正确标记 UNPROVEN，不得产出已认证 VariantCertificate。

宪法 X6：本模块不 import 任何学科包/学段包。
"""
from src.core.instantiation.variation.certificate import (
    VariantCertificate,
    compute_objective_signature,
    mark_unproven,
)
from src.core.instantiation.variation.engine import (
    generate_variants,
    mark_ai_free_rewrite,
)

__all__ = [
    "VariantCertificate",
    "compute_objective_signature",
    "generate_variants",
    "mark_ai_free_rewrite",
    "mark_unproven",
]
