"""T-W2-042 预览网格组件：渲染按轴抽样 20 例的实例化结果.

非路由组件：由 template_form.py 的预览页调用，把 generate_variants 的结果
渲染为 HTML 网格。本组件只做数据→HTML 的转换，不调用引擎、不读 DB。

宪法 A5/X6：不 import 学科包；调用 src/core/instantiation 的纯计算接口。
"""
from __future__ import annotations

from typing import Any

from src.core.instantiation.engine import ItemVersionResult
from src.core.instantiation.variation.certificate import VariantCertificate


def extract_preview_fields(variant: ItemVersionResult) -> dict[str, Any]:
    """从 ItemVersionResult 提取预览所需的字段.

    返回 {item_version_id, stem, answer, options, error_bindings}.
    - stem：content.blocks 中 kind=stem/text 的 rendered 文本
    - answer：scoring_ref.scorer_params.answer（若有）
    - options：error_bindings 的 option_value/label 列表
    """
    stem_parts: list[str] = []
    for block in variant.content.get("blocks", []):
        rendered = block.get("rendered") or block.get("template", "")
        if rendered:
            stem_parts.append(rendered)
    stem = " ".join(stem_parts) if stem_parts else "(空题面)"

    answer = variant.scoring_ref.get("scorer_params", {}).get("answer", "")
    options = []
    for binding in variant.error_bindings:
        options.append(
            {
                "value": binding.get("option_value"),
                "label": binding.get("label") or binding.get("option_value"),
                "error_type_id": binding.get("error_type_id"),
            }
        )

    return {
        "item_version_id": variant.item_version_id,
        "stem": stem,
        "answer": str(answer) if answer is not None else "",
        "options": options,
    }


def render_variant_cards(variants: list[ItemVersionResult]) -> list[dict[str, Any]]:
    """批量提取 variants 的预览字段，返回 cards 列表供模板渲染."""
    return [extract_preview_fields(v) for v in variants]


def certificate_summary(cert: VariantCertificate) -> dict[str, Any]:
    """提取 VariantCertificate 的关键字段供模板展示.

    axis_slots / frozen_slots / objective_signature 在 invariant_evidence 内
    （见 VariantCertificate 模型定义），不是顶层字段。
    """
    evidence = cert.invariant_evidence or {}
    return {
        "certified": cert.certified,
        "operator_id": cert.operator_id,
        "reason": cert.reason,
        "axis_id": cert.axis_id,
        "axis_slots": list(evidence.get("axis_slots", [])),
        "frozen_slots": list(evidence.get("frozen_slots", [])),
        "variant_count": len(cert.variant_ids),
        "objective_signature": evidence.get("objective_signature", ""),
    }


__all__ = [
    "certificate_summary",
    "extract_preview_fields",
    "render_variant_cards",
]
