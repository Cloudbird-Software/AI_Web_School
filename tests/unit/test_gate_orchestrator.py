"""T-W2-010 门编排引擎单元测试.

对照任务卡验收标准逐条覆盖：
1. run_gate(artifact_ref, artifact_type, pack_id, ctx) 返回最终 verdict 与 cert_id（若通过）。
2. 失败短路：链中第一个 fail 之后的验证器不被调用（mock 验证）。
3. 通过时写入 GateCertificate + GateRun + GateVerdict；失败时只写入 GateRun + GateVerdict。
4. 单元测试覆盖 pass/fail/short-circuit/review 四种情况。

附加覆盖：
- 廉价先行排序：cheap cost_tier 先于 expensive 调用（同 tier 内保持声明顺序）。
- run_gate_async 异步占位接口：返回伪任务 id（W2 不部署真实 Redis worker）。
- DB 落库验证：GateCertificate/GateRun/GateVerdict 行数与字段一致。
- short_circuited 未调用验证器：返回 GateRunRecord 标记 short_circuited=True，不入库。

宪法 A5/X6：核心域零学科特判；D1：三本账只增不改——本测试只 INSERT + TRUNCATE，
不 UPDATE/DELETE 已落库行（DB 触发器物理强制兜底）。
"""
from __future__ import annotations

import os
import re
from decimal import Decimal
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.gate.models import GateCertificate, GateRun, GateVerdict
from src.core.gate.orchestrator import GateOutcome, GateRunRecord, run_gate
from src.core.gate.orchestrator.orchestrator import run_gate_async
from src.core.gate.policy.loader import (
    ChainEntry,
    GatePolicy,
    ValidatorStep,
    _ensure_generic_validator_stubs,
)
from src.core.gate.validator import (
    GateContext,
    Validator,
    ValidatorResult,
    list_validators,
    register_validator,
    reset_registry,
)
from src.core.gate.validators.generic import (
    DuplicatePlaceholderValidator,
    LicenseValidator,
    SchemaValidator,
)


# ────────────────────────────────────────────────────────────────────
# Mock 验证器工厂
# ────────────────────────────────────────────────────────────────────
# 为什么自造 mock 而非用 generic 真实实现：编排测试要确定性地触发 pass/fail/review
# 与 short-circuit，真实验证器依赖 DB 状态（license 表/published 版本）难以精确控制。
# mock 通过共享 call_log 列表记录调用顺序，便于断言「短路后未调用」。

# 模块级共享调用日志：所有 mock 验证器实例共享，由 fixture 在每测试前清空。
# 为什么用模块级而非类级：每个 mock 验证器是动态生成的不同子类，类属性不共享；
# 用模块级 list 让所有 mock 写入同一日志，便于跨实例断言调用顺序。
_SHARED_CALL_LOG: list[str] = []


def _make_mock_validator_class(
    vid: str,
    verdict: str = "pass",
    blocking: bool = True,
    cost_tier: str = "cheap",
    evidence: dict[str, Any] | None = None,
) -> type[Validator]:
    """工厂：构造一个 mock Validator 子类.

    Args:
        vid: 验证器 id（同 pack 内唯一）。
        verdict: 固定返回的 verdict（pass/fail/review）。
        blocking: 是否阻断（编排器据此短路）。
        cost_tier: cheap/expensive（编排器据此排序）。
        evidence: 证据 dict（默认空）。

    Returns:
        Validator 子类。所有实例共享 _SHARED_CALL_LOG（模块级），便于跨实例断言调用顺序。
    """

    class _MockValidator(Validator):
        validator_id = vid
        version = f"test-1.0.0+{vid}"
        # blocking / cost_tier 在类创建后赋值（不能在类体内直接用闭包变量）

        async def validate(self, artifact_ref: str, ctx: GateContext) -> ValidatorResult:
            _SHARED_CALL_LOG.append(vid)
            return self._timed_result(
                verdict=verdict,  # 闭包捕获
                evidence=evidence or {"mock_id": vid, "verdict": verdict},
                confidence=Decimal("1.000"),
                elapsed_ms=5,
                cost_tokens=0,
            )

    _MockValidator.blocking = blocking
    _MockValidator.cost_tier = cost_tier
    return _MockValidator


# ────────────────────────────────────────────────────────────────────
# 测试隔离 fixtures
# ────────────────────────────────────────────────────────────────────

# 注册表隔离：每测试前重置，注册 mock + 通用验证器（policy loader 校验需要）
@pytest.fixture(autouse=True)
def _baseline_registry():
    reset_registry()
    # 清空共享调用日志
    _SHARED_CALL_LOG.clear()
    # 注册 platform 的三个通用验证器（policy loader 默认策略校验需要声明存在）
    register_validator("platform", SchemaValidator)
    register_validator("platform", LicenseValidator)
    register_validator("platform", DuplicatePlaceholderValidator)
    _ensure_generic_validator_stubs()  # 兜底：若上面没注册成功则补桩
    # 真实实现覆盖桩
    register_validator("platform", SchemaValidator)
    register_validator("platform", LicenseValidator)
    register_validator("platform", DuplicatePlaceholderValidator)
    yield
    reset_registry()
    _SHARED_CALL_LOG.clear()
    register_validator("platform", SchemaValidator)
    _ensure_generic_validator_stubs()


# DB 隔离：每测试前 TRUNCATE 三表 + 插入 cert:none 占位行
# 为什么需要 cert:none 占位：编排器失败时 gate_run.certificate_id 用 'cert:none' 作 FK
# 目标（orchestrator.py 注释说明 W2 占位方案）；占位行需在测试 setup 中预插。
@pytest_asyncio.fixture(autouse=True)
async def _truncate_and_seed(async_session: AsyncSession):
    await async_session.execute(
        text(
            "TRUNCATE TABLE gate_verdict, gate_run, gate_certificate RESTART IDENTITY CASCADE"
        )
    )
    # 插入 cert:none 占位行（失败 run 的 FK 目标）
    await async_session.execute(
        text(
            "INSERT INTO gate_certificate (cert_id, artifact_ref, cert_type,"
            " policy_version, issued_by)"
            " VALUES ('cert:none', 'placeholder-for-failed-run', 'publish',"
            " 'no-policy', 'system')"
        )
    )
    await async_session.commit()
    yield


# ────────────────────────────────────────────────────────────────────
# 辅助：构造测试用 policy
# ────────────────────────────────────────────────────────────────────

def _build_policy(steps: list[tuple[str, bool | None, str]]) -> GatePolicy:
    """构造测试用 GatePolicy（pack_id='test-orch', artifact_type='item'）.

    Args:
        steps: [(validator_id, blocking, cost_tier), ...]
        blocking=None 表示让编排器取验证器类属性。

    Returns:
        GatePolicy 对象。
    """
    entries = [
        ChainEntry(
            pack_id="test-orch",
            artifact_type="item",
            validators=[
                ValidatorStep(validator_id=vid, blocking=blk)
                for vid, blk, _ in steps
            ],
        )
    ]
    return GatePolicy(
        policy_version="test-policy-v1",
        status="frozen-candidate",
        description="测试策略",
        chains=entries,
    )


def _register_mocks(specs: list[tuple[str, str, bool, str]]) -> dict[str, type[Validator]]:
    """注册一组 mock 验证器到 test-orch pack.

    Args:
        specs: [(vid, verdict, blocking, cost_tier), ...]

    Returns:
        {vid: validator_class} 字典。
    """
    classes: dict[str, type[Validator]] = {}
    for vid, verdict, blocking, cost_tier in specs:
        cls = _make_mock_validator_class(
            vid=vid, verdict=verdict, blocking=blocking, cost_tier=cost_tier
        )
        register_validator("test-orch", cls)
        classes[vid] = cls
    return classes


# ────────────────────────────────────────────────────────────────────
# §1 pass 场景：全部阻断项 pass → 签发证书 + 全部 run 落库
# ────────────────────────────────────────────────────────────────────

async def test_run_gate_pass_signs_certificate(async_session: AsyncSession):
    """验收 #1/#3：全部 pass → final_verdict='pass'，cert_id 非空，三表落库."""
    # 注册 3 个全 pass 的 mock（含一个 expensive 验证廉价先行）
    mocks = _register_mocks([
        ("v1", "pass", True, "cheap"),
        ("v2", "pass", True, "expensive"),
        ("v3", "pass", False, "cheap"),  # 非阻断也 pass
    ])
    policy = _build_policy([
        ("v1", True, "cheap"),
        ("v2", True, "expensive"),
        ("v3", False, "cheap"),
    ])
    ctx = GateContext(
        artifact_type="item",
        pack_id="test-orch",
        artifact_payload={"objective": {}},
    )

    outcome = await run_gate(
        artifact_ref="sha256:item-v1",
        artifact_type="item",
        pack_id="test-orch",
        ctx=ctx,
        policy=policy,
        db=async_session,
        issued_by="test-issuer",
    )

    # 验收 #1：final_verdict + cert_id
    assert outcome.final_verdict == "pass"
    assert outcome.cert_id is not None
    assert outcome.cert_id.startswith("cert_")
    assert outcome.policy_version == "test-policy-v1"
    assert outcome.artifact_ref == "sha256:item-v1"
    assert outcome.short_circuit_at is None

    # 3 个验证器都被调用，无 short_circuited 记录
    assert len(outcome.runs) == 3
    assert all(not r.short_circuited for r in outcome.runs)
    # 调用顺序：廉价先行（cheap: v1, v3 声明顺序）→ expensive (v2)
    call_order = [r.validator_id for r in outcome.runs if not r.short_circuited]
    # cheap 先于 expensive：v1/v3 应在 v2 之前
    assert call_order.index("v2") > call_order.index("v1")
    assert call_order.index("v2") > call_order.index("v3")

    # 验收 #3：GateCertificate 落库（含 cert:none + 新签发的 = 2 行）
    cert_count = await async_session.scalar(
        text("SELECT count(*) FROM gate_certificate")
    )
    assert cert_count == 2  # cert:none + 新证书
    # 新证书可回读
    new_cert = await async_session.scalar(
        text("SELECT artifact_ref FROM gate_certificate WHERE cert_id = :cid"),
        {"cid": outcome.cert_id},
    )
    assert new_cert == "sha256:item-v1"

    # GateRun 落库：3 条 run（不含 cert:none 占位行）
    run_count = await async_session.scalar(
        text("SELECT count(*) FROM gate_run WHERE certificate_id = :cid"),
        {"cid": outcome.cert_id},
    )
    assert run_count == 3

    # GateVerdict 落库：3 条（每 run 一条）
    verdict_count = await async_session.scalar(
        text(
            "SELECT count(*) FROM gate_verdict v"
            " JOIN gate_run r ON v.run_id = r.run_id"
            " WHERE r.certificate_id = :cid"
        ),
        {"cid": outcome.cert_id},
    )
    assert verdict_count == 3

    # mock 共享调用日志验证：三个都被调用，且 cheap 先于 expensive
    assert set(_SHARED_CALL_LOG) == {"v1", "v2", "v3"}
    assert _SHARED_CALL_LOG.index("v2") > _SHARED_CALL_LOG.index("v1")
    assert _SHARED_CALL_LOG.index("v2") > _SHARED_CALL_LOG.index("v3")


# ────────────────────────────────────────────────────────────────────
# §2 fail + short-circuit 场景
# ────────────────────────────────────────────────────────────────────

async def test_run_gate_fail_short_circuits(async_session: AsyncSession):
    """验收 #2/#3：第一个阻断 fail → 后续验证器不被调用，不签证书，run 落库."""
    mocks = _register_mocks([
        ("v1", "pass", True, "cheap"),
        ("v2_fail", "fail", True, "cheap"),   # 第二个阻断 fail
        ("v3", "pass", True, "expensive"),     # 不应被调用
        ("v4", "pass", False, "cheap"),        # 不应被调用
    ])
    policy = _build_policy([
        ("v1", True, "cheap"),
        ("v2_fail", True, "cheap"),
        ("v3", True, "expensive"),
        ("v4", False, "cheap"),
    ])
    ctx = GateContext(
        artifact_type="item",
        pack_id="test-orch",
        artifact_payload={"objective": {}},
    )

    outcome = await run_gate(
        artifact_ref="sha256:item-fail",
        artifact_type="item",
        pack_id="test-orch",
        ctx=ctx,
        policy=policy,
        db=async_session,
    )

    # 验收 #1：final_verdict='fail'，cert_id=None
    assert outcome.final_verdict == "fail"
    assert outcome.cert_id is None
    assert outcome.short_circuit_at == "v2_fail"

    # 验收 #2：v3/v4 未被调用
    called = [r.validator_id for r in outcome.runs if not r.short_circuited]
    assert "v3" not in called
    assert "v4" not in called
    assert called == ["v1", "v2_fail"]  # cheap-first + 声明顺序

    # 未调用的记录在 outcome.runs 中标记 short_circuited=True
    sc_records = [r for r in outcome.runs if r.short_circuited]
    assert {r.validator_id for r in sc_records} == {"v3", "v4"}
    for r in sc_records:
        assert r.verdict == "review"
        assert r.cost_ms == 0
        assert "未被调用" in r.evidence["reason"]

    # mock 共享调用日志验证：v3/v4 未被调用
    assert "v3" not in _SHARED_CALL_LOG
    assert "v4" not in _SHARED_CALL_LOG
    assert _SHARED_CALL_LOG == ["v1", "v2_fail"]  # 顺序：cheap 先行 + 声明顺序

    # 验收 #3：未签发新证书（仅 cert:none 占位）
    cert_count = await async_session.scalar(
        text("SELECT count(*) FROM gate_certificate")
    )
    assert cert_count == 1  # 只有 cert:none

    # 失败 run 落库到 cert:none（2 条：v1 + v2_fail）
    run_count = await async_session.scalar(
        text("SELECT count(*) FROM gate_run WHERE certificate_id = 'cert:none'")
    )
    assert run_count == 2

    # v3/v4 未入库（short_circuited 不落库）
    v3_runs = await async_session.scalar(
        text("SELECT count(*) FROM gate_run WHERE validator_id = 'v3'")
    )
    assert v3_runs == 0


async def test_run_gate_fail_first_validator_short_circuits(async_session: AsyncSession):
    """验收 #2 边界：链首即 fail → 后续全部短路."""
    _register_mocks([
        ("v1_fail", "fail", True, "cheap"),
        ("v2", "pass", True, "cheap"),
        ("v3", "pass", True, "cheap"),
    ])
    policy = _build_policy([
        ("v1_fail", True, "cheap"),
        ("v2", True, "cheap"),
        ("v3", True, "cheap"),
    ])
    ctx = GateContext(
        artifact_type="item", pack_id="test-orch", artifact_payload={}
    )

    outcome = await run_gate(
        artifact_ref="sha256:item-fail-first",
        artifact_type="item",
        pack_id="test-orch",
        ctx=ctx,
        policy=policy,
        db=async_session,
    )

    assert outcome.final_verdict == "fail"
    assert outcome.cert_id is None
    assert outcome.short_circuit_at == "v1_fail"

    # 只有 v1 入库
    called = [r.validator_id for r in outcome.runs if not r.short_circuited]
    assert called == ["v1_fail"]
    sc = {r.validator_id: r for r in outcome.runs if r.short_circuited}
    assert set(sc.keys()) == {"v2", "v3"}


# ────────────────────────────────────────────────────────────────────
# §3 review 场景：非阻断 review → 不签证书
# ────────────────────────────────────────────────────────────────────

async def test_run_gate_review_no_certificate(async_session: AsyncSession):
    """验收 #4：非阻断 review → final_verdict='review'，不签证书."""
    _register_mocks([
        ("v1", "pass", True, "cheap"),
        ("v2_review", "review", False, "cheap"),  # 非阻断 review
        ("v3", "pass", True, "cheap"),
    ])
    policy = _build_policy([
        ("v1", True, "cheap"),
        ("v2_review", False, "cheap"),
        ("v3", True, "cheap"),
    ])
    ctx = GateContext(
        artifact_type="item", pack_id="test-orch", artifact_payload={}
    )

    outcome = await run_gate(
        artifact_ref="sha256:item-review",
        artifact_type="item",
        pack_id="test-orch",
        ctx=ctx,
        policy=policy,
        db=async_session,
    )

    # review：不签证书
    assert outcome.final_verdict == "review"
    assert outcome.cert_id is None
    assert outcome.short_circuit_at is None  # review 不短路

    # 三个验证器都被调用（review 不阻断不短路）
    called = [r.validator_id for r in outcome.runs if not r.short_circuited]
    assert set(called) == {"v1", "v2_review", "v3"}

    # v2_review 的 verdict='review' 落库
    v2_verdict = await async_session.scalar(
        text(
            "SELECT verdict FROM gate_run"
            " WHERE certificate_id = 'cert:none' AND validator_id = 'v2_review'"
        )
    )
    assert v2_verdict == "review"


async def test_run_gate_blocking_review_does_not_short_circuit(async_session: AsyncSession):
    """阻断项返回 review（不是 fail）：不短路，但最终 verdict=review（不签证书）."""
    _register_mocks([
        ("v1", "pass", True, "cheap"),
        ("v2_review_blocking", "review", True, "cheap"),  # 阻断 review
        ("v3", "pass", True, "cheap"),  # 仍应被调用（review 不短路，只 fail 短路）
    ])
    policy = _build_policy([
        ("v1", True, "cheap"),
        ("v2_review_blocking", True, "cheap"),
        ("v3", True, "cheap"),
    ])
    ctx = GateContext(
        artifact_type="item", pack_id="test-orch", artifact_payload={}
    )

    outcome = await run_gate(
        artifact_ref="sha256:item-review-block",
        artifact_type="item",
        pack_id="test-orch",
        ctx=ctx,
        policy=policy,
        db=async_session,
    )

    # 阻断 review 仍不签证书（只有 pass 才签）
    assert outcome.final_verdict == "review"
    assert outcome.cert_id is None
    # review 不触发短路（只有 fail + blocking 才短路）
    assert outcome.short_circuit_at is None

    # v3 仍被调用（review 不阻断后续）
    called = [r.validator_id for r in outcome.runs if not r.short_circuited]
    assert "v3" in called


# ────────────────────────────────────────────────────────────────────
# §4 廉价先行排序
# ────────────────────────────────────────────────────────────────────

async def test_run_gate_cheap_first_ordering(async_session: AsyncSession):
    """廉价先行：cheap cost_tier 先于 expensive 调用，同 tier 保持声明顺序."""
    mocks = _register_mocks([
        ("expensive_a", "pass", True, "expensive"),
        ("cheap_a", "pass", True, "cheap"),
        ("cheap_b", "pass", True, "cheap"),
        ("expensive_b", "pass", True, "expensive"),
    ])
    # 声明顺序：expensive_a, cheap_a, cheap_b, expensive_b
    # 廉价先行后：cheap_a, cheap_b, expensive_a, expensive_b
    policy = _build_policy([
        ("expensive_a", True, "expensive"),
        ("cheap_a", True, "cheap"),
        ("cheap_b", True, "cheap"),
        ("expensive_b", True, "expensive"),
    ])
    ctx = GateContext(
        artifact_type="item", pack_id="test-orch", artifact_payload={}
    )

    outcome = await run_gate(
        artifact_ref="sha256:item-order",
        artifact_type="item",
        pack_id="test-orch",
        ctx=ctx,
        policy=policy,
        db=async_session,
    )

    assert outcome.final_verdict == "pass"

    # 调用顺序：cheap_a, cheap_b（声明顺序）→ expensive_a, expensive_b（声明顺序）
    call_order = [r.validator_id for r in outcome.runs if not r.short_circuited]
    assert call_order == ["cheap_a", "cheap_b", "expensive_a", "expensive_b"]


# ────────────────────────────────────────────────────────────────────
# §5 异步占位接口
# ────────────────────────────────────────────────────────────────────

async def test_run_gate_async_returns_task_id(async_session: AsyncSession):
    """run_gate_async 异步占位：返回伪任务 id（cert_id 或 task_ULID）."""
    _register_mocks([
        ("v1", "pass", True, "cheap"),
    ])
    policy = _build_policy([("v1", True, "cheap")])
    ctx = GateContext(
        artifact_type="item", pack_id="test-orch", artifact_payload={}
    )

    task_id = await run_gate_async(
        artifact_ref="sha256:item-async",
        artifact_type="item",
        pack_id="test-orch",
        ctx=ctx,
        policy=policy,
        db=async_session,
    )
    # W2 占位：pass 时返回 cert_id
    assert task_id is not None
    assert task_id.startswith("cert_")


async def test_run_gate_async_returns_task_id_on_fail(async_session: AsyncSession):
    """run_gate_async 在失败时返回伪 task_id（task_ULID）."""
    _register_mocks([
        ("v1_fail", "fail", True, "cheap"),
    ])
    policy = _build_policy([("v1_fail", True, "cheap")])
    ctx = GateContext(
        artifact_type="item", pack_id="test-orch", artifact_payload={}
    )

    task_id = await run_gate_async(
        artifact_ref="sha256:item-async-fail",
        artifact_type="item",
        pack_id="test-orch",
        ctx=ctx,
        policy=policy,
        db=async_session,
    )
    # W2 占位：fail 时返回 task_ULID
    assert task_id.startswith("task_")


# ────────────────────────────────────────────────────────────────────
# §6 无策略链报错
# ────────────────────────────────────────────────────────────────────

async def test_run_gate_raises_when_no_chain(async_session: AsyncSession):
    """pack_id+artifact_type 无策略链（连 platform 回退都没有）→ ValueError."""
    policy = GatePolicy(
        policy_version="empty-v1",
        status="frozen-candidate",
        description="空策略",
        chains=[
            ChainEntry(
                pack_id="other-pack",
                artifact_type="item",
                validators=[ValidatorStep(validator_id="v1")],
            )
        ],
    )
    # 还要注册 v1 让 policy 校验通过
    _register_mocks([("v1", "pass", True, "cheap")])

    ctx = GateContext(
        artifact_type="item", pack_id="test-orch", artifact_payload={}
    )

    with pytest.raises(ValueError, match="门策略未配置链"):
        await run_gate(
            artifact_ref="sha256:no-chain",
            artifact_type="item",
            pack_id="test-orch",
            ctx=ctx,
            policy=policy,
            db=async_session,
        )


# ────────────────────────────────────────────────────────────────────
# §7 落库字段一致性
# ────────────────────────────────────────────────────────────────────

async def test_run_gate_run_records_match_db(async_session: AsyncSession):
    """落库字段与 outcome.runs 一致：validator_id/verdict/confidence/cost."""
    _register_mocks([
        ("v1", "pass", True, "cheap"),
        ("v2", "pass", True, "cheap"),
    ])
    policy = _build_policy([
        ("v1", True, "cheap"),
        ("v2", True, "cheap"),
    ])
    ctx = GateContext(
        artifact_type="item", pack_id="test-orch", artifact_payload={}
    )

    outcome = await run_gate(
        artifact_ref="sha256:item-fields",
        artifact_type="item",
        pack_id="test-orch",
        ctx=ctx,
        policy=policy,
        db=async_session,
        issued_by="alice",
    )

    # 取出 DB 中的 run
    rows = (
        await async_session.execute(
            text(
                "SELECT validator_id, validator_version, verdict, confidence,"
                " cost_ms, cost_tokens"
                " FROM gate_run WHERE certificate_id = :cid"
                " ORDER BY validator_id"
            ),
            {"cid": outcome.cert_id},
        )
    ).all()

    db_records = {r[0]: r for r in rows}
    for record in outcome.runs:
        if record.short_circuited:
            continue
        db = db_records[record.validator_id]
        assert db[1] == record.validator_version
        assert db[2] == record.verdict
        assert Decimal(str(db[3])) == record.confidence
        assert db[4] == record.cost_ms
        assert db[5] == record.cost_tokens

    # 证书 issued_by 字段正确
    issuer = await async_session.scalar(
        text("SELECT issued_by FROM gate_certificate WHERE cert_id = :cid"),
        {"cid": outcome.cert_id},
    )
    assert issuer == "alice"

    # 证书 policy_version 与 outcome 一致
    pv = await async_session.scalar(
        text("SELECT policy_version FROM gate_certificate WHERE cert_id = :cid"),
        {"cid": outcome.cert_id},
    )
    assert pv == outcome.policy_version


# ────────────────────────────────────────────────────────────────────
# §8 核心域不 import 学科包/学段包
# ────────────────────────────────────────────────────────────────────

def test_no_subject_pack_imports_in_orchestrator():
    """宪法 A5/X6：src/core/gate/orchestrator/ 不 import 任何学科包/学段包."""
    orch_dir = os.path.join("src", "core", "gate", "orchestrator")
    pattern = re.compile(
        r"^\s*(?:from\s+(?:packs|subject_|gradeband)|import\s+(?:packs|subject_|gradeband))",
        re.MULTILINE,
    )
    violations: list[tuple[str, list[str]]] = []
    for fname in os.listdir(orch_dir):
        if not fname.endswith(".py"):
            continue
        fpath = os.path.join(orch_dir, fname)
        with open(fpath, encoding="utf-8") as f:
            content = f.read()
        matches = pattern.findall(content)
        if matches:
            violations.append((fname, matches))
    assert not violations, f"src/core/gate/orchestrator/ 存在学科包 import：{violations}"


# ────────────────────────────────────────────────────────────────────
# §9 cert_type 参数（publish/retire）
# ────────────────────────────────────────────────────────────────────

async def test_run_gate_cert_type_retire(async_session: AsyncSession):
    """cert_type='retire' 时签发的证书类型为 retire."""
    _register_mocks([("v1", "pass", True, "cheap")])
    policy = _build_policy([("v1", True, "cheap")])
    ctx = GateContext(
        artifact_type="item", pack_id="test-orch", artifact_payload={}
    )

    outcome = await run_gate(
        artifact_ref="sha256:item-retire",
        artifact_type="item",
        pack_id="test-orch",
        ctx=ctx,
        policy=policy,
        db=async_session,
        cert_type="retire",
    )

    assert outcome.final_verdict == "pass"
    cert_type = await async_session.scalar(
        text("SELECT cert_type FROM gate_certificate WHERE cert_id = :cid"),
        {"cid": outcome.cert_id},
    )
    assert cert_type == "retire"
