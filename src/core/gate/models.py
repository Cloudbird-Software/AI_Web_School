"""T-W1-006 校验域三表 ORM 映射 + Pydantic schema.

按迁移 0004 落地三表的 ORM 模型与写入用 Pydantic schema。

D1 三本账只增不改：本模块仅暴露 INSERT 路径（Pydantic Create schema）与
SELECT 路径（ORM 模型查询）。不提供 Update/Delete 方法——任何修改意图都应
在应用层被拒绝，DB 触发器（迁移 0004）是兜底。

为什么 ORM 模型不直接挂 async 写入方法：保持 ORM 与服务层分离——写入逻辑
应走专门的服务函数（W1 暂不实现校验器插件框架，T-W1-006 仅落表结构 + ORM）。
调用方通过 AsyncSession + ORM 模型执行 INSERT/SELECT 即可。
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Identity,
    Integer,
    Numeric,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import ENUM as PG_ENUM
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.core.models._base import Base


# ────────────────────────────────────────────────────────────────────
# ORM 模型（统一使用 src/core/models/_base.py 的 Base，T-W1-003 已合入同分支）
# ────────────────────────────────────────────────────────────────────

class GateCertificate(Base):
    """§4.3 门证书：签发后只增不改；item_version.gate_certificate_id 的合法来源.

    ORM 层仅暴露 INSERT/SELECT——本类不提供 update/delete 类方法。
    D1 物理强制由 DB 触发器（迁移 0004 trg_gate_certificate_append_only）兜底。
    """

    __tablename__ = "gate_certificate"

    cert_id: Mapped[str] = mapped_column(Text(), primary_key=True)
    artifact_ref: Mapped[str] = mapped_column(Text(), nullable=False)
    cert_type: Mapped[str] = mapped_column(Text(), nullable=False)
    policy_version: Mapped[str] = mapped_column(Text(), nullable=False)
    issued_by: Mapped[str] = mapped_column(Text(), nullable=False)
    issued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    # 关联：一个证书可有多次 run（多验证器协同）
    runs: Mapped[list["GateRun"]] = relationship(
        back_populates="certificate", cascade="save-update, merge"
    )

    def __repr__(self) -> str:
        return (
            f"GateCertificate(cert_id={self.cert_id!r}, "
            f"artifact_ref={self.artifact_ref!r}, cert_type={self.cert_type!r})"
        )


class GateRun(Base):
    """§4.3 一次校验运行记录：策略版本/验证器/判定/证据/成本.

    ORM 层仅暴露 INSERT/SELECT——本类不提供 update/delete 类方法。
    D1 物理强制由 DB 触发器（迁移 0004 trg_gate_run_append_only）兜底。
    """

    __tablename__ = "gate_run"

    run_id: Mapped[str] = mapped_column(Text(), primary_key=True)
    certificate_id: Mapped[str] = mapped_column(
        Text(), ForeignKey("gate_certificate.cert_id", name="fk_gr_certificate"), nullable=False
    )
    policy_version: Mapped[str] = mapped_column(Text(), nullable=False)
    validator_id: Mapped[str] = mapped_column(Text(), nullable=False)
    validator_version: Mapped[str] = mapped_column(Text(), nullable=False)
    verdict: Mapped[str] = mapped_column(
        PG_ENUM(
            "pass", "fail", "review",
            name="gate_run_verdict_enum",
            create_type=False,
        ),
        nullable=False,
    )
    evidence: Mapped[dict[str, Any]] = mapped_column(JSONB(), nullable=False)
    confidence: Mapped[Decimal] = mapped_column(Numeric(4, 3), nullable=False)
    cost_ms: Mapped[int] = mapped_column(Integer(), nullable=False)
    cost_tokens: Mapped[int] = mapped_column(Integer(), nullable=False)
    run_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    # 关联：所属证书
    certificate: Mapped["GateCertificate"] = relationship(back_populates="runs")
    # 关联：一次 run 可有多条 verdict
    verdicts: Mapped[list["GateVerdict"]] = relationship(
        back_populates="run", cascade="save-update, merge"
    )

    def __repr__(self) -> str:
        return (
            f"GateRun(run_id={self.run_id!r}, certificate_id={self.certificate_id!r}, "
            f"validator_id={self.validator_id!r}, verdict={self.verdict!r})"
        )


class GateVerdict(Base):
    """§4.3 验证器判定结果明细：一次 run 可有多条 verdict（多步骤/多规则）.

    ORM 层仅暴露 INSERT/SELECT——本类不提供 update/delete 类方法。
    D1 物理强制由 DB 触发器（迁移 0004 trg_gate_verdict_append_only）兜底。
    """

    __tablename__ = "gate_verdict"

    verdict_id: Mapped[int] = mapped_column(
        BigInteger(), Identity(always=True), primary_key=True
    )
    run_id: Mapped[str] = mapped_column(
        Text(), ForeignKey("gate_run.run_id", name="fk_gv_run"), nullable=False
    )
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB(), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    # 关联：所属 run
    run: Mapped["GateRun"] = relationship(back_populates="verdicts")

    def __repr__(self) -> str:
        return f"GateVerdict(verdict_id={self.verdict_id!r}, run_id={self.run_id!r})"


# ────────────────────────────────────────────────────────────────────
# Pydantic schema（写入用 Create schema；读取走 ORM 模型）
# ────────────────────────────────────────────────────────────────────
# 为什么只暴露 Create schema：三表只增不改，无 Update schema；
# 读取走 ORM 模型，无需单独 Read schema（调用方按需取字段）。
# 为什么 extra='forbid'：拒绝未声明字段，避免误传列名导致 INSERT 失败或字段污染。

Verdict = Literal["pass", "fail", "review"]
CertType = Literal["publish", "retire"]


class GateCertificateCreate(BaseModel):
    """门证书写入 schema（D1 append-only：仅 INSERT）.

    - cert_id：应用层 ULID 生成；全局唯一性由应用层保证。
    - artifact_ref：被签发的产物引用（如 item_version_id）。
    - cert_type：'publish'/'retire'（DB CHECK 约束兜底）。
    - issued_at：签发时间；默认 None → DB server_default now()。
    """

    model_config = ConfigDict(extra="forbid")

    cert_id: str = Field(..., min_length=1, description="证书 id（ULID）")
    artifact_ref: str = Field(..., min_length=1, description="被签发的产物引用")
    cert_type: CertType = Field(..., description="证书类型：publish/retire")
    policy_version: str = Field(..., min_length=1, description="门策略版本")
    issued_by: str = Field(..., min_length=1, description="签发人 id")
    issued_at: Optional[datetime] = Field(
        None, description="签发时间；None 则 DB server_default now()"
    )


class GateRunCreate(BaseModel):
    """校验运行写入 schema（D1 append-only：仅 INSERT）.

    - confidence：0.0~1.0（DB CHECK 约束兜底）。
    - cost_ms/cost_tokens：必须 ≥0（DB CHECK 约束兜底）。
    - run_at：运行时间；默认 None → DB server_default now()。
    """

    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(..., min_length=1, description="运行 id（ULID）")
    certificate_id: str = Field(..., min_length=1, description="关联证书 id")
    policy_version: str = Field(..., min_length=1, description="门策略版本")
    validator_id: str = Field(..., min_length=1, description="验证器 id（注册表）")
    validator_version: str = Field(..., min_length=1, description="验证器版本")
    verdict: Verdict = Field(..., description="判定：pass/fail/review")
    evidence: dict[str, Any] = Field(..., description="证据（验证器自描述结构）")
    confidence: Decimal = Field(
        ..., ge=Decimal("0"), le=Decimal("1"), description="置信度 0.000~1.000"
    )
    cost_ms: int = Field(..., ge=0, description="耗时（毫秒）")
    cost_tokens: int = Field(..., ge=0, description="token 成本")
    run_at: Optional[datetime] = Field(
        None, description="运行时间；None 则 DB server_default now()"
    )


class GateVerdictCreate(BaseModel):
    """验证器判定明细写入 schema（D1 append-only：仅 INSERT）.

    - verdict_id：自增主键，由 DB 生成；写入时不传。
    - detail：判定明细（jsonb），结构由验证器自定。
    """

    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(..., min_length=1, description="关联运行 id")
    detail: dict[str, Any] = Field(..., description="判定明细（验证器自描述结构）")
