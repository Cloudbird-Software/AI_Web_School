"""确定性实例化引擎（T-W2-004）。

按母题 spec + params 确定性产出 ItemVersion dict：
  1. 解析 spec 为 ItemTemplateSpec（六大块强类型，T-W2-001）
  2. normalize_params：按 slot.type 规范化为 JSON 兼容、确定性表示
     （decimal→Decimal-str、fraction→Fraction-str），避免浮点漂移
  3. 求 answer_program：用安全表达式求值器（T-W2-002）算正解
  4. 生成 distractors：每条 rule 调用 distractor 生成器（T-W2-003）
  5. 装配 content：presentation.blocks 用 {slot_name} 插值
  6. 装配 interaction_ref / scoring_ref / error_bindings / lineage / objective
  7. compute_instance_id（公式一）= H(tvd, normalized_params, pack_digest,
     engine_digest, corpus_digests, locale)

返回 dict（不写 DB；DB 写入由 content writer 承载，本模块只做纯计算）。
同一 (template_version, params, pack_digest, engine_digest, corpus_digests, locale)
任意次实例化必得同一 item_version_id（D3 可复现）。

宪法 X6：本模块不 import 任何学科包/学段包。
"""
from src.core.instantiation.engine.engine import (
    ENGINE_DIGEST,
    ItemVersionResult,
    instantiate,
    normalize_params,
)

__all__ = [
    "ENGINE_DIGEST",
    "ItemVersionResult",
    "instantiate",
    "normalize_params",
]
