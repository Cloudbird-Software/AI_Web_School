"""§2.3 item_template_version 母题版本表 ORM + Pydantic（T-W1-003）.

宪法 D1：item_template_version 行永不 UPDATE/DELETE；
宪法 D3：template_version_id = sha256 of spec（版本即内容寻址）。

列与 alembic/versions/0002_item_model.py::_create_item_template_version 逐字对齐：
- template_version_id text PK（sha256 of spec）
- template_id text NOT NULL FK→item_template
- dsl_version text NOT NULL（DSL 语法版本，DSL 自身版本化）
- spec jsonb NOT NULL（母题定义六大块：objective/slots/variation_axes/
  presentation/answer_program/distractor_rules）
- status item_template_version_status_enum NOT NULL（draft/published/retired，
  无 quarantined——母题不直接过门）
- created_at timestamptz NOT NULL server_default now()
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict
from sqlalchemy import DateTime, ForeignKey, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from src.core.models._base import Base, item_template_version_status_enum


class ItemTemplateVersion(Base):
    """§2.3 item_template_version 母题版本 ORM 映射.

    一行 = 一个母题的某个版本快照（template_version_id = sha256 of spec）；
    母题不直接过门，status 仅 draft/published/retired（无 quarantined）。
    """

    __tablename__ = "item_template_version"

    template_version_id: Mapped[str] = mapped_column(Text, primary_key=True)
    template_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("item_template.template_id", name="fk_itv_template"),
        nullable=False,
    )
    dsl_version: Mapped[str] = mapped_column(Text, nullable=False)
    spec: Mapped[dict] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(
        item_template_version_status_enum, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    def __repr__(self) -> str:  # pragma: no cover - 调试辅助
        return (
            f"ItemTemplateVersion(template_version_id={self.template_version_id!r}, "
            f"template_id={self.template_id!r}, status={self.status!r})"
        )


class ItemTemplateVersionPydantic(BaseModel):
    """ItemTemplateVersion 的 Pydantic 表示.

    spec 字段保持 dict 不细化：母题 DSL 结构随 DSL 版本演化，本处不强约束。
    """

    model_config = ConfigDict(extra="forbid")

    template_version_id: str
    template_id: str
    dsl_version: str
    spec: dict[str, Any]
    status: Literal["draft", "published", "retired"]
    created_at: Optional[datetime] = None


__all__ = ["ItemTemplateVersion", "ItemTemplateVersionPydantic"]
