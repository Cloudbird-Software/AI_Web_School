"""§2.2 item_version 不可变内容快照 ORM + 六大块 Pydantic 子模型（T-W1-003）.

宪法 D1：item_version 行永不 UPDATE/DELETE（只增账本）；
宪法 D3：item_version_id = §3 公式一/二的内容寻址哈希，同内容必同 id；
宪法 A7：tier 在 lineage 内作为谱系字段，A/B/C/D 四级结构一致。

列与 alembic/versions/0002_item_model.py::_create_item_version 逐字对齐：
- item_version_id text PK
- item_id text NOT NULL FK→item
- status item_version_status_enum NOT NULL
- 六大块 JSONB（objective/interaction_ref/content/scoring_ref/error_bindings/lineage）NOT NULL
- rendered_snapshot jsonb nullable（quarantined 前必填，DB CHECK 兜底）
- gate_certificate_id text nullable（唯一真源，lineage 内不重复存储）
- published_at timestamptz nullable（DB CHECK 强制非空必伴随 gate_certificate_id）
- retired_at timestamptz nullable
- created_at timestamptz NOT NULL server_default now()

六大块 Pydantic 子模型对应契约 §5.1/§5.2 机器可校验 schema。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, RootModel
from sqlalchemy import DateTime, ForeignKey, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from src.core.models._base import Base, item_version_status_enum


# ════════════════════════════════════════════════════════════════════
# 六大块 Pydantic 子模型（契约 §5.1 objective / §5.2 lineage + §2.2 其余）
# ════════════════════════════════════════════════════════════════════

# ── objective 子模型 ────────────────────────────────────────────────

class KpRef(BaseModel):
    """知识点引用（契约 §2.2.1 kp_set 元素）.

    dimension：知识维度（'kp' 等，供图谱分面查询）。
    code：知识点编码（如 'math.nal.decimal.compare'）。
    """

    model_config = ConfigDict(extra="forbid")

    dimension: str
    code: str


class StepRef(BaseModel):
    """分步过程题的步骤级标注（R-Q-15）."""

    model_config = ConfigDict(extra="forbid")

    step_id: str
    kp: list[str]


class Objective(BaseModel):
    """§2.2.1 objective 块：知识标注集 + 认知层级 + 学段 + 图谱版本.

    对应契约 §5.1 机器可校验 JSON Schema；required 字段缺一即校验失败。
    """

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
    """§2.2 interaction_ref 块：交互类型 + 交互参数.

    interaction_id 必须在 registries/interaction.yaml 注册（D4）；
    interaction_params 的具体结构由交互类型决定，本处保持 dict 不细化。
    """

    model_config = ConfigDict(extra="forbid")

    interaction_id: str
    interaction_params: dict[str, Any]


# ── content 块 ──────────────────────────────────────────────────────

class Content(BaseModel):
    """§2.2 content 块：题面语义 AST + 素材版本引用.

    为什么 blocks 用 list[dict] 而非强类型：题面 AST 因交互类型而异（选择题/
    填空题/拖拽题块结构各不相同），D4 注册表承载具体校验；本处保持 permissive
    让所有交互类型都能通过，是契约 §5 未对 content 强制 JSON Schema 的体现。
    """

    model_config = ConfigDict(extra="allow")

    blocks: list[dict[str, Any]]


# ── scoring_ref 块 ──────────────────────────────────────────────────

class ScoringRef(BaseModel):
    """§2.2 scoring_ref 块：评分器 + 评分参数.

    scorer_id 必须在 registries/scorer.yaml 注册（D4）；
    scorer_params 的结构由评分器决定，本处保持 dict 不细化。
    """

    model_config = ConfigDict(extra="forbid")

    scorer_id: str
    scorer_params: dict[str, Any]


# ── error_bindings 块（顶层是数组） ─────────────────────────────────

class ErrorBindings(RootModel[list[dict[str, Any]]]):
    """§2.2 error_bindings 块：选项/评分维度 → 错误类型 + 置信规则（R-Q-06/07）.

    为什么用 RootModel：error_bindings 在 JSONB 中顶层是数组（list[dict]），
    每个元素是一个错误绑定；用 RootModel 让 Pydantic 模型直接对应数组，
    而非 {bindings: [...]} 的包装对象，与 DB 存储结构对齐。
    为什么元素是 permissive dict：契约 §5 未对 error_bindings 单元素结构
    强制 JSON Schema，R-Q-06/07 的具体结构由错误类型注册表承载。
    """

    root: list[dict[str, Any]]


# ── lineage 子模型 ──────────────────────────────────────────────────

class Pipeline(BaseModel):
    """生产线标识（lineage.pipeline）。"""

    model_config = ConfigDict(extra="forbid")

    id: str
    version: str


class CorpusRef(BaseModel):
    """语料库版本引用（lineage.corpus_refs 元素）。"""

    model_config = ConfigDict(extra="forbid")

    corpus_version_id: str
    digest: str


class Lineage(BaseModel):
    """§2.2.2 lineage 块：生产谱系（R-Q-22）.

    对应契约 §5.2 机器可校验 JSON Schema；
    required = [tier, pipeline, signed_by, signed_at]；
    tier ∈ {A,B} 时 template_version_id 与 params 必填（应用层校验，schema 不强制）；
    tier ∈ {C,D} 且经 AI 起草时 ai_ledger_refs 非空（同上）。
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


# ════════════════════════════════════════════════════════════════════
# ItemVersion ORM
# ════════════════════════════════════════════════════════════════════

class ItemVersion(Base):
    """§2.2 item_version 不可变内容快照 ORM 映射.

    一行 = 一个题目的某个版本快照（item_version_id），永不 UPDATE/DELETE；
    六大块 JSONB 列承载全部内容与谱系；gate_certificate_id 为门证书唯一真源。

    宪法 D1：本表只增；宪法 D3：item_version_id 为内容寻址哈希。
    """

    __tablename__ = "item_version"

    item_version_id: Mapped[str] = mapped_column(Text, primary_key=True)
    item_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("item.item_id", name="fk_iv_item"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(
        item_version_status_enum, nullable=False
    )
    # 六大块 JSONB（§1/§2.2），均为 NOT NULL
    objective: Mapped[dict] = mapped_column(JSONB, nullable=False)
    interaction_ref: Mapped[dict] = mapped_column(JSONB, nullable=False)
    content: Mapped[dict] = mapped_column(JSONB, nullable=False)
    scoring_ref: Mapped[dict] = mapped_column(JSONB, nullable=False)
    # error_bindings 顶层为 list[dict]（R-Q-06/07），ORM 列类型仍是 JSONB
    error_bindings: Mapped[list] = mapped_column(JSONB, nullable=False)
    lineage: Mapped[dict] = mapped_column(JSONB, nullable=False)
    # §2.2 rendered_snapshot：quarantined 前必填（DB CHECK ck_iv_quarantine_requires_rendered 兜底）
    rendered_snapshot: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    # §2.2 gate_certificate_id：唯一真源；FK 待 T-W1-006 替换占位表后补
    gate_certificate_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    published_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    retired_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    def __repr__(self) -> str:  # pragma: no cover - 调试辅助
        return (
            f"ItemVersion(item_version_id={self.item_version_id!r}, "
            f"item_id={self.item_id!r}, status={self.status!r})"
        )


# ── ItemVersion Pydantic（聚合六大块） ──────────────────────────────

class ItemVersionPydantic(BaseModel):
    """ItemVersion 的 Pydantic 表示（聚合六大块强类型子模型）.

    用于 API 入参校验/序列化；created_at 等服务端填字段保持 Optional。
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
    published_at: Optional[datetime] = None
    retired_at: Optional[datetime] = None
    created_at: Optional[datetime] = None


__all__ = [
    "ItemVersion",
    "ItemVersionPydantic",
    # 六大块 Pydantic 子模型
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
