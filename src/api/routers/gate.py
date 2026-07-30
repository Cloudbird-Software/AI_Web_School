"""T-W2-039 门证书只读路由：GET /gate_certificates/{cert_id}.

返回门证书 + 关联的 gate_run 列表（含 verdicts）。

宪法 D1：仅 SELECT；宪法 A5/X6：不 import 学科包。
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.api.deps import get_async_session, require_auth
from src.core.gate.models import GateCertificate, GateRun, GateVerdict

router = APIRouter(prefix="", tags=["gate"])


# ────────────────────────────────────────────────────────────────────
# 响应模型
# ────────────────────────────────────────────────────────────────────
# 为什么自定义 Pydantic 模型而非复用 core.gate.models 的 Create schema：
# core 端只暴露写入用 Create schema（D1 只增不改，无 Update）；读取响应需独立
# 的 Read schema 承载 SELECT 出的 ORM 字段（含 issued_at/run_at 等服务端填字段）。


class GateVerdictRead(BaseModel):
    """门判定明细（读取响应）."""

    model_config = ConfigDict(extra="forbid")

    verdict_id: int
    run_id: str
    detail: dict[str, Any]
    created_at: Optional[datetime] = None


class GateRunRead(BaseModel):
    """单次验证器运行记录（读取响应）."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    certificate_id: str
    policy_version: str
    validator_id: str
    validator_version: str
    verdict: str
    evidence: dict[str, Any]
    confidence: Decimal
    cost_ms: int
    cost_tokens: int
    run_at: Optional[datetime] = None
    created_at: Optional[datetime] = None
    verdicts: list[GateVerdictRead] = Field(default_factory=list)


class GateCertificateRead(BaseModel):
    """门证书 + 关联 runs（GET /gate_certificates/{cert_id} 响应）."""

    model_config = ConfigDict(extra="forbid")

    cert_id: str
    artifact_ref: str
    cert_type: str
    policy_version: str
    issued_by: str
    issued_at: Optional[datetime] = None
    created_at: Optional[datetime] = None
    runs: list[GateRunRead] = Field(default_factory=list)


# ────────────────────────────────────────────────────────────────────
# 端点
# ────────────────────────────────────────────────────────────────────


@router.get(
    "/gate_certificates/{cert_id}",
    response_model=GateCertificateRead,
    summary="查询门证书与运行记录",
    responses={404: {"description": "cert_id 不存在"}},
)
async def get_gate_certificate(
    cert_id: str,
    session: AsyncSession = Depends(get_async_session),
    _auth: None = Depends(require_auth),
) -> GateCertificateRead:
    """返回门证书 + 关联的所有 gate_run（含 gate_verdict 明细）."""
    stmt = (
        select(GateCertificate)
        .where(GateCertificate.cert_id == cert_id)
        .options(
            selectinload(GateCertificate.runs).selectinload(GateRun.verdicts)
        )
    )
    result = await session.execute(stmt)
    cert = result.scalar_one_or_none()
    if cert is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"cert_id={cert_id} 不存在",
        )

    runs_read: list[GateRunRead] = []
    for run in cert.runs:
        verdicts_read = [
            GateVerdictRead(
                verdict_id=v.verdict_id,
                run_id=v.run_id,
                detail=v.detail,
                created_at=v.created_at,
            )
            for v in run.verdicts
        ]
        runs_read.append(
            GateRunRead(
                run_id=run.run_id,
                certificate_id=run.certificate_id,
                policy_version=run.policy_version,
                validator_id=run.validator_id,
                validator_version=run.validator_version,
                verdict=run.verdict,
                evidence=run.evidence,
                confidence=run.confidence,
                cost_ms=run.cost_ms,
                cost_tokens=run.cost_tokens,
                run_at=run.run_at,
                created_at=run.created_at,
                verdicts=verdicts_read,
            )
        )

    return GateCertificateRead(
        cert_id=cert.cert_id,
        artifact_ref=cert.artifact_ref,
        cert_type=cert.cert_type,
        policy_version=cert.policy_version,
        issued_by=cert.issued_by,
        issued_at=cert.issued_at,
        created_at=cert.created_at,
        runs=runs_read,
    )


__all__ = ["router"]
