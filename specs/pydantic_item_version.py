"""item_version 导入用 Pydantic 模型（W0-1）.

与 specs/item_version_import_schema.json 一一对应，
与 src/core/models/item_version.py::ItemVersionPydantic 结构对齐。
用于适配器/加载器的入参校验，避免直接 import ORM 层（学科零特判）。
"""
from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, RootModel


# ── objective 块子模型 ──────────────────────────────────────────────


class KpRef(BaseModel):
    """知识点引用 element: {dimension, code}."""

    model_config = ConfigDict(extra="forbid")

    dimension: str
    code: str


class StepRef(BaseModel):
    """分步过程题的步骤级标注."""

    model_config = ConfigDict(extra="forbid")

    step_id: str
    kp: list[str]


class Objective(BaseModel):
    """§2.2.1 objective 块：知识标注集 + 认知层级 + 学段 + 图谱版本."""

    model_config = ConfigDict(extra="forbid")

    kp_set: list[KpRef]
    kp_set_mode: Literal["single", "all_required", "compensatory"]
    cognitive_level: Literal[
        "remember", "understand", "apply", "analyze", "evaluate", "create"
    ]
    gradeband: Literal["L", "M", "H"]
    graph_release: str
    steps: Optional[list[StepRef]] = None


# ── interaction_ref 块 ──────────────────────────────────────────────


class InteractionRef(BaseModel):
    """交互类型引用块.

    interaction_id 必须在 registries/interaction.yaml 注册（宪法 D4）.
    interaction_params 结构由交互类型决定，保持 dict 不细化.
    """

    model_config = ConfigDict(extra="forbid")

    interaction_id: str
    interaction_params: dict[str, Any]


# ── content 块 ──────────────────────────────────────────────────────


class Content(BaseModel):
    """题面语义 AST 块.

    blocks 用 list[dict] 而非强类型：题面 AST 因交互类型而异，
    per-interaction 校验由注册表承载，此处保持 permissive（与 schema 一致）.
    """

    model_config = ConfigDict(extra="allow")

    blocks: list[dict[str, Any]]


# ── scoring_ref 块 ──────────────────────────────────────────────────


class ScoringRef(BaseModel):
    """评分器引用块.

    scorer_id 必须在 registries/scorer.yaml 注册（宪法 D4）.
    scorer_params 结构由评分器决定，保持 dict 不细化.
    """

    model_config = ConfigDict(extra="forbid")

    scorer_id: str
    scorer_params: dict[str, Any]


# ── error_bindings 块（顶层是数组） ─────────────────────────────────


class ErrorBindings(RootModel[list[dict[str, Any]]]):
    """错误绑定：选项/评分维度 → 错误类型 + 置信规则.

    顶层为数组（list[dict]），每个元素是一个错误绑定.
    元素是 permissive dict：单元素结构由错误类型注册表承载.
    """

    root: list[dict[str, Any]]


# ── lineage 块 ──────────────────────────────────────────────────────


class Pipeline(BaseModel):
    """生产线标识（lineage.pipeline）：{id, version}."""

    model_config = ConfigDict(extra="forbid")

    id: str
    version: str


class CorpusRef(BaseModel):
    """语料库版本引用（lineage.corpus_refs 元素）."""

    model_config = ConfigDict(extra="forbid")

    corpus_version_id: str
    digest: str


class Lineage(BaseModel):
    """生产谱系块（R-Q-22）.

    required = [tier, pipeline, signed_by, signed_at].
    tier ∈ {A,B} 时 template_version_id 与 params 通常必填（应用层校验）.
    tier ∈ {C,D} 且经 AI 起草时 ai_ledger_refs 非空（同上）.
    """

    model_config = ConfigDict(extra="forbid")

    tier: Literal["A", "B", "C", "D"]
    pipeline: Pipeline
    template_version_id: Optional[str] = None
    params: Optional[dict[str, Any]] = None
    seed: Optional[int] = None
    corpus_refs: Optional[list[CorpusRef]] = None
    ai_ledger_refs: Optional[list[str]] = None
    signed_by: str
    signed_at: str


# ── 顶层 ItemVersionImport 模型 ─────────────────────────────────────


class ItemVersionImport(BaseModel):
    """item_version 导入契约聚合模型.

    对应 specs/item_version_import_schema.json.
    服务端字段（rendered_snapshot / gate_certificate_id / published_at /
    retired_at / created_at）保持 Optional，导入时通常为空.
    """

    model_config = ConfigDict(extra="forbid")

    item_version_id: str
    item_id: str
    status: Literal["draft", "quarantined", "published", "retired"]
    objective: Objective
    interaction_ref: InteractionRef
    content: Content
    scoring_ref: ScoringRef
    error_bindings: ErrorBindings
    lineage: Lineage
    rendered_snapshot: Optional[dict[str, Any]] = None
    gate_certificate_id: Optional[str] = None
    published_at: Optional[str] = None
    retired_at: Optional[str] = None
    created_at: Optional[str] = None


__all__ = [
    "ItemVersionImport",
    "Objective",
    "KpRef",
    "StepRef",
    "InteractionRef",
    "Content",
    "ScoringRef",
    "ErrorBindings",
    "Lineage",
    "Pipeline",
    "CorpusRef",
]
