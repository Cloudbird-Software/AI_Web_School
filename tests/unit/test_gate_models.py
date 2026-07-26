"""T-W1-006 校验域三表单元测试.

验收标准 #4 覆盖：
(a) gate_certificate 插入后可回读
(b) gate_run 关联 certificate_id 正确
(c) gate 三表间 JOIN 查询正常

附加覆盖：
- 三表 append-only 触发器：UPDATE/DELETE 在 DB 层被拒绝（D1 物理强制）
- Pydantic Create schema：extra='forbid' 拒绝未声明字段
- confidence/cost 等数值边界 CHECK 约束
- verdict enum 三值（pass/fail/review）可写入

宪法 D1 三本账只增不改：append-only 由迁移 0004 的 BEFORE UPDATE OR DELETE
触发器物理强制。本测试验证 ORM 模型 + Pydantic schema + DB 触发器协同正确。
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.gate.models import (
    GateCertificate,
    GateCertificateCreate,
    GateRun,
    GateRunCreate,
    GateVerdict,
    GateVerdictCreate,
)


# ────────────────────────────────────────────────────────────────────
# 测试隔离 fixture：每测试前 TRUNCATE 三表
# ────────────────────────────────────────────────────────────────────
# 为什么用 TRUNCATE 而非 DELETE：DELETE 会被 append-only 触发器拒绝；TRUNCATE
# 是 DDL 类操作，不触发 BEFORE UPDATE/DELETE 触发器。
# 为什么 CASCADE：gate_verdict → gate_run → gate_certificate 有 FK 链。
@pytest_asyncio.fixture(autouse=True)
async def _truncate_gate_tables(async_session: AsyncSession):
    await async_session.execute(
        text("TRUNCATE TABLE gate_verdict, gate_run, gate_certificate RESTART IDENTITY CASCADE")
    )
    await async_session.commit()
    yield


# ────────────────────────────────────────────────────────────────────
# 辅助构造函数
# ────────────────────────────────────────────────────────────────────

def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _ulid() -> str:
    """简化 ULID：用 uuid4 hex 代替（测试用，非生产 ULID 实现）."""
    return uuid4().hex


def _make_certificate_create(
    cert_id: str | None = None,
    artifact_ref: str = "sha256:item-v1",
    cert_type: str = "publish",
) -> GateCertificateCreate:
    return GateCertificateCreate(
        cert_id=cert_id or _ulid(),
        artifact_ref=artifact_ref,
        cert_type=cert_type,
        policy_version="gate-policy-v1",
        issued_by="issuer-test",
        issued_at=_now_utc(),
    )


def _make_run_create(
    certificate_id: str,
    run_id: str | None = None,
    verdict: str = "pass",
) -> GateRunCreate:
    return GateRunCreate(
        run_id=run_id or _ulid(),
        certificate_id=certificate_id,
        policy_version="gate-policy-v1",
        validator_id="format_check",
        validator_version="1.0.0+sha256:abc",
        verdict=verdict,
        evidence={"checked": ["blocks", "scoring_ref"], "passed": 2, "failed": 0},
        confidence=Decimal("0.950"),
        cost_ms=120,
        cost_tokens=0,
        run_at=_now_utc(),
    )


def _make_verdict_create(run_id: str) -> GateVerdictCreate:
    return GateVerdictCreate(
        run_id=run_id,
        detail={"rule": "block_completeness", "hit": True, "note": "六大块齐全"},
    )


async def _insert_certificate(
    async_session: AsyncSession,
    cert_id: str | None = None,
    cert_type: str = "publish",
) -> str:
    """插入一条证书，返回 cert_id."""
    create = _make_certificate_create(cert_id=cert_id, cert_type=cert_type)
    obj = GateCertificate(
        cert_id=create.cert_id,
        artifact_ref=create.artifact_ref,
        cert_type=create.cert_type,
        policy_version=create.policy_version,
        issued_by=create.issued_by,
        issued_at=create.issued_at,
    )
    async_session.add(obj)
    await async_session.commit()
    return create.cert_id


async def _insert_run(
    async_session: AsyncSession,
    certificate_id: str,
    run_id: str | None = None,
    verdict: str = "pass",
) -> str:
    """插入一条 run，返回 run_id."""
    create = _make_run_create(certificate_id=certificate_id, run_id=run_id, verdict=verdict)
    obj = GateRun(
        run_id=create.run_id,
        certificate_id=create.certificate_id,
        policy_version=create.policy_version,
        validator_id=create.validator_id,
        validator_version=create.validator_version,
        verdict=create.verdict,
        evidence=create.evidence,
        confidence=create.confidence,
        cost_ms=create.cost_ms,
        cost_tokens=create.cost_tokens,
        run_at=create.run_at,
    )
    async_session.add(obj)
    await async_session.commit()
    return create.run_id


# ────────────────────────────────────────────────────────────────────
# (a) gate_certificate 插入后可回读
# ────────────────────────────────────────────────────────────────────

async def test_certificate_insert_and_readback(async_session: AsyncSession):
    """验收 #4(a)：gate_certificate 插入后可 SELECT 回读全部字段."""
    cert_id = await _insert_certificate(async_session, cert_id="cert-test-1")

    result = await async_session.execute(
        select(GateCertificate).where(GateCertificate.cert_id == cert_id)
    )
    cert = result.scalar_one()
    assert cert.cert_id == "cert-test-1"
    assert cert.artifact_ref == "sha256:item-v1"
    assert cert.cert_type == "publish"
    assert cert.policy_version == "gate-policy-v1"
    assert cert.issued_by == "issuer-test"
    assert cert.issued_at is not None
    assert cert.created_at is not None


async def test_certificate_cert_type_domain(async_session: AsyncSession):
    """§4.3 cert_type 仅 'publish'/'retire'（DB CHECK 兜底）.

    走裸 SQL INSERT 绕开 Pydantic 校验（Pydantic Literal 在应用层已拦截），
    专门验证 DB 层 CHECK 约束 ck_gc_cert_type_domain 是否生效——
    这是 Pydantic 之外的兜底防线。
    """
    # publish 与 retire 均可写入（ORM 路径，Pydantic 已校验）
    await _insert_certificate(async_session, cert_id="cert-publish", cert_type="publish")
    await _insert_certificate(async_session, cert_id="cert-retire", cert_type="retire")

    # 非法 cert_type 应被 DB CHECK 拒绝（绕开 Pydantic 走裸 SQL）
    with pytest.raises(Exception) as exc_info:
        await async_session.execute(
            text(
                """
                INSERT INTO gate_certificate (cert_id, artifact_ref, cert_type, policy_version, issued_by, issued_at)
                VALUES (:cid, :aref, 'invalid', :pv, :ib, :ts)
                """
            ),
            {
                "cid": "cert-bad",
                "aref": "sha256:bad",
                "pv": "p1",
                "ib": "i1",
                "ts": _now_utc(),
            },
        )
        await async_session.commit()
    await async_session.rollback()
    err_msg = str(exc_info.value).lower()
    assert "ck_gc_cert_type_domain" in err_msg or "check" in err_msg, (
        f"应被 CHECK 拒绝，实际：{exc_info.value}"
    )


# ────────────────────────────────────────────────────────────────────
# (b) gate_run 关联 certificate_id 正确
# ────────────────────────────────────────────────────────────────────

async def test_run_associates_certificate(async_session: AsyncSession):
    """验收 #4(b)：gate_run.certificate_id 正确关联 gate_certificate.cert_id."""
    cert_id = await _insert_certificate(async_session, cert_id="cert-1")
    run_id = await _insert_run(async_session, certificate_id=cert_id, run_id="run-1")

    result = await async_session.execute(
        select(GateRun).where(GateRun.run_id == run_id)
    )
    run = result.scalar_one()
    assert run.run_id == "run-1"
    assert run.certificate_id == cert_id
    assert run.validator_id == "format_check"
    assert run.verdict == "pass"
    assert run.confidence == Decimal("0.950")
    assert run.cost_ms == 120
    assert run.cost_tokens == 0
    assert run.evidence == {
        "checked": ["blocks", "scoring_ref"],
        "passed": 2,
        "failed": 0,
    }


async def test_run_verdict_three_values(async_session: AsyncSession):
    """§4.3 verdict 三值（pass/fail/review）均可写入."""
    cert_id = await _insert_certificate(async_session, cert_id="cert-v")
    for i, verdict in enumerate(["pass", "fail", "review"]):
        await _insert_run(
            async_session,
            certificate_id=cert_id,
            run_id=f"run-{i}-{verdict}",
            verdict=verdict,
        )

    result = await async_session.execute(
        select(GateRun.verdict).where(GateRun.certificate_id == cert_id)
    )
    verdicts = {row[0] for row in result.fetchall()}
    assert verdicts == {"pass", "fail", "review"}


async def test_run_verdict_invalid_rejected(async_session: AsyncSession):
    """§4.3 verdict 仅三值，其他值在 DB 层被 enum 拒绝."""
    cert_id = await _insert_certificate(async_session, cert_id="cert-bad-v")
    with pytest.raises(Exception):
        await async_session.execute(
            text(
                """
                INSERT INTO gate_run (
                    run_id, certificate_id, policy_version,
                    validator_id, validator_version, verdict,
                    evidence, confidence, cost_ms, cost_tokens, run_at
                ) VALUES (
                    :rid, :cid, :pv,
                    :vid, :vv, 'rejected',
                    '{}'::jsonb, 1.0, 0, 0, :ts
                )
                """
            ),
            {
                "rid": "run-bad",
                "cid": cert_id,
                "pv": "p1",
                "vid": "v1",
                "vv": "1.0",
                "ts": _now_utc(),
            },
        )
        await async_session.commit()
    await async_session.rollback()


async def test_run_confidence_range(async_session: AsyncSession):
    """§4.3 confidence 必须 0.000~1.000（DB CHECK 兜底）."""
    cert_id = await _insert_certificate(async_session, cert_id="cert-conf")

    # 1.000 合法
    create = _make_run_create(certificate_id=cert_id, run_id="run-ok")
    obj = GateRun(
        run_id=create.run_id,
        certificate_id=create.certificate_id,
        policy_version=create.policy_version,
        validator_id=create.validator_id,
        validator_version=create.validator_version,
        verdict=create.verdict,
        evidence=create.evidence,
        confidence=Decimal("1.000"),
        cost_ms=0,
        cost_tokens=0,
        run_at=create.run_at,
    )
    async_session.add(obj)
    await async_session.commit()

    # 1.001 非法（CHECK 拒绝）
    create2 = _make_run_create(certificate_id=cert_id, run_id="run-bad")
    obj2 = GateRun(
        run_id=create2.run_id,
        certificate_id=create2.certificate_id,
        policy_version=create2.policy_version,
        validator_id=create2.validator_id,
        validator_version=create2.validator_version,
        verdict=create2.verdict,
        evidence=create2.evidence,
        confidence=Decimal("1.001"),  # 越界
        cost_ms=0,
        cost_tokens=0,
        run_at=create2.run_at,
    )
    async_session.add(obj2)
    with pytest.raises(Exception) as exc_info:
        await async_session.commit()
    await async_session.rollback()
    assert "ck_gr_confidence_range" in str(exc_info.value).lower() or "check" in str(exc_info.value).lower()


async def test_run_cost_nonneg(async_session: AsyncSession):
    """§4.3 cost_ms/cost_tokens 必须 ≥0（DB CHECK 兜底）."""
    cert_id = await _insert_certificate(async_session, cert_id="cert-cost")
    with pytest.raises(Exception):
        await async_session.execute(
            text(
                """
                INSERT INTO gate_run (
                    run_id, certificate_id, policy_version,
                    validator_id, validator_version, verdict,
                    evidence, confidence, cost_ms, cost_tokens, run_at
                ) VALUES (
                    :rid, :cid, :pv,
                    :vid, :vv, 'pass',
                    '{}'::jsonb, 1.0, -1, 0, :ts
                )
                """
            ),
            {
                "rid": "run-neg",
                "cid": cert_id,
                "pv": "p1",
                "vid": "v1",
                "vv": "1.0",
                "ts": _now_utc(),
            },
        )
        await async_session.commit()
    await async_session.rollback()


# ────────────────────────────────────────────────────────────────────
# (c) gate 三表间 JOIN 查询正常
# ────────────────────────────────────────────────────────────────────

async def test_three_table_join(async_session: AsyncSession):
    """验收 #4(c)：gate_certificate JOIN gate_run JOIN gate_verdict 查询正常.

    场景：1 个证书 → 1 次 run → 2 条 verdict 明细。
    """
    cert_id = await _insert_certificate(async_session, cert_id="cert-join")
    run_id = await _insert_run(async_session, certificate_id=cert_id, run_id="run-join")

    # 插入 2 条 verdict
    for i in range(2):
        create = _make_verdict_create(run_id=run_id)
        obj = GateVerdict(
            run_id=create.run_id,
            detail={"step": i, "rule": f"rule-{i}", "hit": True},
        )
        async_session.add(obj)
    await async_session.commit()

    # 三表 JOIN 查询
    result = await async_session.execute(
        text(
            """
            SELECT c.cert_id, c.cert_type, c.artifact_ref,
                   r.run_id, r.validator_id, r.verdict, r.confidence,
                   v.verdict_id, v.detail
            FROM gate_certificate c
            JOIN gate_run r ON r.certificate_id = c.cert_id
            JOIN gate_verdict v ON v.run_id = r.run_id
            WHERE c.cert_id = :cid
            ORDER BY v.verdict_id
            """
        ),
        {"cid": cert_id},
    )
    rows = result.fetchall()
    assert len(rows) == 2
    # 验证 JOIN 字段对齐
    for row in rows:
        assert row[0] == "cert-join"  # cert_id
        assert row[1] == "publish"     # cert_type
        assert row[2] == "sha256:item-v1"  # artifact_ref
        assert row[3] == "run-join"    # run_id
        assert row[4] == "format_check"  # validator_id
        assert row[5] == "pass"         # verdict
        assert row[6] == Decimal("0.950")  # confidence
        assert row[7] is not None       # verdict_id
        assert "rule" in row[8]         # detail


async def test_verdict_insert_and_readback(async_session: AsyncSession):
    """§4.3 gate_verdict 插入后可回读（含 verdict_id 自增主键）."""
    cert_id = await _insert_certificate(async_session, cert_id="cert-vd")
    run_id = await _insert_run(async_session, certificate_id=cert_id, run_id="run-vd")

    create = _make_verdict_create(run_id=run_id)
    obj = GateVerdict(
        run_id=create.run_id,
        detail={"rule": "r1", "hit": True},
    )
    async_session.add(obj)
    await async_session.commit()

    result = await async_session.execute(
        select(GateVerdict).where(GateVerdict.run_id == run_id)
    )
    verdict = result.scalar_one()
    assert verdict.verdict_id is not None  # 自增主键已生成
    assert verdict.verdict_id > 0
    assert verdict.run_id == run_id
    assert verdict.detail == {"rule": "r1", "hit": True}
    assert verdict.created_at is not None


# ────────────────────────────────────────────────────────────────────
# (d) 三表 append-only：UPDATE/DELETE 在 DB 层被拒绝
# ────────────────────────────────────────────────────────────────────

async def test_certificate_update_rejected(async_session: AsyncSession):
    """D1：gate_certificate UPDATE 在 DB 层被 append-only 触发器拒绝."""
    await _insert_certificate(async_session, cert_id="cert-up")
    with pytest.raises(Exception, match="append-only"):
        await async_session.execute(
            text("UPDATE gate_certificate SET issued_by = 'hacker'")
        )
        await async_session.commit()
    await async_session.rollback()


async def test_certificate_delete_rejected(async_session: AsyncSession):
    """D1：gate_certificate DELETE 在 DB 层被 append-only 触发器拒绝."""
    await _insert_certificate(async_session, cert_id="cert-del")
    with pytest.raises(Exception, match="append-only"):
        await async_session.execute(text("DELETE FROM gate_certificate"))
        await async_session.commit()
    await async_session.rollback()


async def test_run_update_rejected(async_session: AsyncSession):
    """D1：gate_run UPDATE 在 DB 层被 append-only 触发器拒绝."""
    cert_id = await _insert_certificate(async_session, cert_id="cert-ru")
    await _insert_run(async_session, certificate_id=cert_id, run_id="run-ru")
    with pytest.raises(Exception, match="append-only"):
        await async_session.execute(
            text("UPDATE gate_run SET verdict = 'fail'")
        )
        await async_session.commit()
    await async_session.rollback()


async def test_run_delete_rejected(async_session: AsyncSession):
    """D1：gate_run DELETE 在 DB 层被 append-only 触发器拒绝."""
    cert_id = await _insert_certificate(async_session, cert_id="cert-rd")
    await _insert_run(async_session, certificate_id=cert_id, run_id="run-rd")
    with pytest.raises(Exception, match="append-only"):
        await async_session.execute(text("DELETE FROM gate_run"))
        await async_session.commit()
    await async_session.rollback()


async def test_verdict_update_rejected(async_session: AsyncSession):
    """D1：gate_verdict UPDATE 在 DB 层被 append-only 触发器拒绝."""
    cert_id = await _insert_certificate(async_session, cert_id="cert-vu")
    run_id = await _insert_run(async_session, certificate_id=cert_id, run_id="run-vu")
    create = _make_verdict_create(run_id=run_id)
    async_session.add(GateVerdict(run_id=create.run_id, detail=create.detail))
    await async_session.commit()

    with pytest.raises(Exception, match="append-only"):
        await async_session.execute(
            text("UPDATE gate_verdict SET detail = '{}'::jsonb")
        )
        await async_session.commit()
    await async_session.rollback()


async def test_verdict_delete_rejected(async_session: AsyncSession):
    """D1：gate_verdict DELETE 在 DB 层被 append-only 触发器拒绝."""
    cert_id = await _insert_certificate(async_session, cert_id="cert-vd2")
    run_id = await _insert_run(async_session, certificate_id=cert_id, run_id="run-vd2")
    create = _make_verdict_create(run_id=run_id)
    async_session.add(GateVerdict(run_id=create.run_id, detail=create.detail))
    await async_session.commit()

    with pytest.raises(Exception, match="append-only"):
        await async_session.execute(text("DELETE FROM gate_verdict"))
        await async_session.commit()
    await async_session.rollback()


# ────────────────────────────────────────────────────────────────────
# Pydantic Create schema：extra='forbid'
# ────────────────────────────────────────────────────────────────────

def test_certificate_create_rejects_extra_fields():
    """Pydantic Create schema 拒绝未声明字段（防止误传列名污染 INSERT）."""
    with pytest.raises(Exception):
        GateCertificateCreate(
            cert_id="c1",
            artifact_ref="a1",
            cert_type="publish",
            policy_version="p1",
            issued_by="i1",
            issued_at=_now_utc(),
            extra_field="should_be_rejected",  # 未声明字段
        )


def test_run_create_rejects_extra_fields():
    """Pydantic Create schema 拒绝未声明字段."""
    with pytest.raises(Exception):
        GateRunCreate(
            run_id="r1",
            certificate_id="c1",
            policy_version="p1",
            validator_id="v1",
            validator_version="1.0",
            verdict="pass",
            evidence={},
            confidence=Decimal("0.5"),
            cost_ms=0,
            cost_tokens=0,
            run_at=_now_utc(),
            bogus="rejected",
        )


def test_verdict_create_rejects_extra_fields():
    """Pydantic Create schema 拒绝未声明字段."""
    with pytest.raises(Exception):
        GateVerdictCreate(
            run_id="r1",
            detail={"k": "v"},
            unexpected=True,
        )


def test_certificate_create_validates_cert_type():
    """Pydantic 校验 cert_type 仅 publish/retire（在 DB 之前的快速失败）."""
    with pytest.raises(Exception):
        GateCertificateCreate(
            cert_id="c1",
            artifact_ref="a1",
            cert_type="bogus",  # 非法
            policy_version="p1",
            issued_by="i1",
            issued_at=_now_utc(),
        )


def test_run_create_validates_confidence_range():
    """Pydantic 校验 confidence 0~1（在 DB 之前的快速失败）."""
    with pytest.raises(Exception):
        GateRunCreate(
            run_id="r1",
            certificate_id="c1",
            policy_version="p1",
            validator_id="v1",
            validator_version="1.0",
            verdict="pass",
            evidence={},
            confidence=Decimal("1.5"),  # 越界
            cost_ms=0,
            cost_tokens=0,
            run_at=_now_utc(),
        )


def test_run_create_validates_cost_nonneg():
    """Pydantic 校验 cost_ms ≥0（在 DB 之前的快速失败）."""
    with pytest.raises(Exception):
        GateRunCreate(
            run_id="r1",
            certificate_id="c1",
            policy_version="p1",
            validator_id="v1",
            validator_version="1.0",
            verdict="pass",
            evidence={},
            confidence=Decimal("0.5"),
            cost_ms=-1,  # 越界
            cost_tokens=0,
            run_at=_now_utc(),
        )
