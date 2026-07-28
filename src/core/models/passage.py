"""C 线语篇 passage 表 ORM + Pydantic（T-W4-012）.

架构 v2 §4.1 C 线：语篇 = 体裁×知识点×难度×学段×学科 + 正文 + 许可 + 难度指标。
Passage 是 C 线素材工坊的核心产物，独立于 item/material/corpus——承载语篇特有
字段（体裁 genre / 正文 body / 难度指标 difficulty_metrics），支撑 AI 起草
（T-W4-013）与语篇校验门（T-W4-014）。

宪法 D1 三本账只增不改：Passage 行 status 走 draft→quarantined→published→retired
（无回边），新版本=新行，旧行不改不删。
宪法 D2 门强制：published 必须持合法 gate_certificate_id——DB CHECK
ck_passage_published_requires_gate 兜底，绕过写入服务直写 published 行必失败。
宪法 A5/X6：本模块不 import 任何学科包/学段包；genre 用通用体裁枚举，学科语义
由 pack 侧 overlay 补充（本表只列跨学科通用体裁）。

列设计依据 tasks/w4/T-W4-012.md 验收 #1（字段全覆盖）：
- passage_id / content_hash / body / genre / kp_refs / difficulty_metrics /
  license(license_id) / grade_band / subject / created_at 全覆盖；
- status / gate_certificate_id / published_at 落地 D2 门强制与状态机。

为什么 genre 用 CHECK 而非 PG ENUM：体裁可能随学科包扩展（语文童话/英语
dialogue），ENUM 新增值需 ALTER TYPE，CHECK 约束更灵活（迁移加值即可），
且与 0016 迁移的 purpose_scope CHECK 风格一致。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from src.core.models._base import Base


# ────────────────────────────────────────────────────────────────────
# 取值域（ORM __table_args__ 与迁移 0018 共同引用的字面量来源）
# ────────────────────────────────────────────────────────────────────
# 迁移 0018 里以字符串字面量重复声明（迁移自包含、不 import ORM，与 0002/0016 风格一致）。
# 通用体裁：覆盖语文/英语阅读常见体裁；学科包特有体裁由 pack 侧 overlay 扩展。
GENRE_VALUES: tuple[str, ...] = (
    "narrative",       # 记叙文
    "expository",      # 说明文
    "argumentative",   # 议论文
    "poetry",          # 诗歌
    "fable",           # 寓言
    "fairy_tale",      # 童话
    "dialogue",        # 对话
    "news_report",     # 新闻报道
    "letter",          # 书信
    "diary",           # 日记
)

GRADE_BAND_VALUES: tuple[str, ...] = ("L", "M", "H")
PASSAGE_STATUS_VALUES: tuple[str, ...] = (
    "draft", "quarantined", "published", "retired",
)


# ────────────────────────────────────────────────────────────────────
# 难度指标 Pydantic 子模型（架构 v2 §4.1：字频/句长/生词率）
# ────────────────────────────────────────────────────────────────────
# 由 T-W4-013 difficulty_analyzer 产出，落 passage.difficulty_metrics JSONB。
# extra='allow' 允许分析器扩展指标（如体裁标签、词汇丰富度），契约不强收紧。


class DifficultyMetrics(BaseModel):
    """语篇难度指标.

    - avg_sentence_length：平均句长（字/句），适龄参考（低段短句为主）。
    - oov_rate：生词率（课标词表外词占比，0.0~1.0），越低越适龄。
    - total_chars / total_sentences：基础统计量，供难度门比对。
    - char_freq：字符频次分布（字→出现次数），供字频分析留档。
    """

    model_config = ConfigDict(extra="allow")

    avg_sentence_length: float = Field(..., ge=0, description="平均句长（字/句）")
    oov_rate: float = Field(..., ge=0, le=1, description="生词率（0.0~1.0）")
    total_chars: int = Field(..., ge=0, description="总字数")
    total_sentences: int = Field(..., ge=0, description="总句数")
    char_freq: dict[str, int] = Field(
        default_factory=dict, description="字符频次分布"
    )


# ────────────────────────────────────────────────────────────────────
# Passage ORM
# ────────────────────────────────────────────────────────────────────


class Passage(Base):
    """C 线语篇 ORM 映射（T-W4-012）.

    一行 = 一篇语篇的一个版本快照；status 走
    draft→quarantined→published→retired（无回边，D1 只增）。
    published 必须持合法 gate_certificate_id（D2，DB CHECK 兜底）。
    """

    __tablename__ = "passage"

    passage_id: Mapped[str] = mapped_column(
        Text, primary_key=True, comment="语篇版本 id（应用层 ULID）"
    )
    content_hash: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="正文内容寻址哈希（sha256:...），同正文必同 hash（D3 精神）",
    )
    body: Mapped[str] = mapped_column(Text, nullable=False, comment="语篇正文")
    genre: Mapped[str] = mapped_column(Text, nullable=False, comment="体裁")
    kp_refs: Mapped[list] = mapped_column(
        JSONB, nullable=False, comment="知识点引用列表（KpRef 结构）"
    )
    difficulty_metrics: Mapped[dict] = mapped_column(
        JSONB, nullable=False, comment="难度指标（字频/句长/生词率）"
    )
    license_id: Mapped[Optional[str]] = mapped_column(
        Text,
        ForeignKey("material_license.license_id", name="fk_passage_license"),
        nullable=True,
        comment="许可登记 id（published 语篇由门策略强制非空）",
    )
    grade_band: Mapped[str] = mapped_column(
        Text, nullable=False, comment="学段 L/M/H"
    )
    subject: Mapped[str] = mapped_column(
        Text, nullable=False, comment="学科 pack_id（如 subject-chinese）"
    )
    status: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        server_default="draft",
        comment="状态机 draft/quarantined/published/retired",
    )
    gate_certificate_id: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True, comment="门证书 id（published 时必填，D2）"
    )
    published_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    __table_args__ = (
        # D2 门强制：published 必须持 gate_certificate_id（与 item_version 同构）
        CheckConstraint(
            "status <> 'published' OR gate_certificate_id IS NOT NULL",
            name="ck_passage_published_requires_gate",
        ),
        CheckConstraint(
            "genre IN ('narrative','expository','argumentative','poetry',"
            "'fable','fairy_tale','dialogue','news_report','letter','diary')",
            name="ck_passage_genre_domain",
        ),
        CheckConstraint(
            "grade_band IN ('L','M','H')",
            name="ck_passage_grade_band_domain",
        ),
        CheckConstraint(
            "status IN ('draft','quarantined','published','retired')",
            name="ck_passage_status_domain",
        ),
        Index("ix_passage_content_hash", "content_hash"),
        Index("ix_passage_subject_grade_band", "subject", "grade_band"),
    )

    def __repr__(self) -> str:  # pragma: no cover - 调试辅助
        return (
            f"Passage(passage_id={self.passage_id!r}, "
            f"genre={self.genre!r}, grade_band={self.grade_band!r}, "
            f"status={self.status!r})"
        )


# ────────────────────────────────────────────────────────────────────
# Passage Pydantic（API 入参/序列化）
# ────────────────────────────────────────────────────────────────────


class PassagePydantic(BaseModel):
    """Passage 的 Pydantic 表示.

    用于 API 入参校验/序列化；服务端填字段（published_at/created_at）保持 Optional。
    """

    model_config = ConfigDict(extra="forbid")

    passage_id: str
    content_hash: str
    body: str
    genre: str
    kp_refs: list[dict[str, Any]]
    difficulty_metrics: dict[str, Any]
    license_id: Optional[str] = None
    grade_band: str
    subject: str
    status: Literal["draft", "quarantined", "published", "retired"] = "draft"
    gate_certificate_id: Optional[str] = None
    published_at: Optional[datetime] = None
    created_at: Optional[datetime] = None


__all__ = [
    "Passage",
    "PassagePydantic",
    "DifficultyMetrics",
    "GENRE_VALUES",
    "GRADE_BAND_VALUES",
    "PASSAGE_STATUS_VALUES",
]
