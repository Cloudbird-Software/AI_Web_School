"""T-W2-010 门编排引擎.

落地架构 v2 §4.3「校验域」三要素中的「编排」：
- 按策略链顺序调用验证器；廉价先行（cheap 优先，expensive 后置）。
- 任一阻断项 fail → 短路（后续验证器不被调用）。
- 全部阻断项 pass 时签发门证书（写入 gate_certificate）；
  失败只写 gate_run + gate_verdict，不签证书。
- 支持同步与异步：同步直接调验证器；异步走 Redis 任务队列（W2 占位，
  实现接口 run_gate_async 入队，真实 worker 后续波次接入）。

落地契约 §4.3「物理阻断」：本模块是签发证书的合法路径——
内容写入服务（writer.py）要求 published 状态必须提供 gate_certificate_id；
该 id 必须由本模块 issue_certificate 路径产出，绕过即失败。

宪法 A5/X6：本模块不 import 任何学科包/学段包；
学科验证器由各学科包在 register_validator 注册，本模块只读注册表。
宪法 D1：三本账只增不改——GateCertificate/GateRun/GateVerdict 仅 INSERT。
"""
from __future__ import annotations

import time
import ulid
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.gate.models import (
    GateCertificate,
    GateRun,
    GateVerdict,
)
from src.core.gate.policy.loader import GatePolicy
from src.core.gate.validator import (
    GateContext,
    Validator,
    ValidatorResult,
    get_validator,
    list_validators,
)


# ────────────────────────────────────────────────────────────────────
# 数据结构
# ────────────────────────────────────────────────────────────────────


class GateRunRecord(BaseModel):
    """单次验证器运行的留痕记录（与 gate_run 表一一对应）.

    - validator_id / validator_version：验证器身份与版本。
    - verdict：pass/fail/review。
    - evidence / confidence / cost_ms / cost_tokens：验证器返回的契约字段。
    - blocking：本次是否阻断（结合策略链配置与验证器类属性）。
    - short_circuited：True 表示因前序阻断 fail 而未被调用（留痕用）.
    """

    model_config = ConfigDict(extra="forbid")

    validator_id: str = Field(..., min_length=1)
    validator_version: str = Field(..., min_length=1)
    verdict: str = Field(...)
    evidence: dict[str, Any] = Field(default_factory=dict)
    confidence: Decimal = Field(default=Decimal("1.000"))
    cost_ms: int = Field(default=0, ge=0)
    cost_tokens: int = Field(default=0, ge=0)
    blocking: bool = Field(default=True)
    short_circuited: bool = Field(
        default=False,
        description="是否因前序阻断 fail 而未被调用",
    )


class GateOutcome(BaseModel):
    """门编排最终结果.

    - final_verdict：综合判定。规则：任一阻断 fail → fail；
      全部阻断 pass 但有 review → review；全部 pass → pass。
    - cert_id：仅 final_verdict='pass' 时签发证书（非空）；
      review/fail 时为 None。
    - policy_version：本次编排所用策略版本（落 gate_run/gate_certificate.policy_version）.
    - runs：各验证器运行的留痕（含未调用的 short_circuited 记录）。
    - short_circuit_at：短路位置（第一个 fail 的 validator_id）；无则 None。
    """

    model_config = ConfigDict(extra="forbid")

    final_verdict: str = Field(..., description="综合判定：pass/fail/review")
    cert_id: str | None = Field(default=None, description="证书 id；仅 pass 时非空")
    policy_version: str = Field(..., min_length=1)
    artifact_ref: str = Field(..., min_length=1)
    runs: list[GateRunRecord] = Field(default_factory=list)
    short_circuit_at: str | None = Field(
        default=None,
        description="第一个 fail 的 validator_id；无则 None",
    )


# ────────────────────────────────────────────────────────────────────
# 同步编排入口
# ────────────────────────────────────────────────────────────────────


# 廉价优先排序权重：cheap=0，其余=1；同 cost_tier 内保持策略链声明顺序。
_COST_TIER_WEIGHT: dict[str, int] = {"cheap": 0, "expensive": 1}


def _cost_tier_weight(tier: str) -> int:
    """取 cost_tier 排序权重（未声明的 tier 当作 expensive 处理）.

    为什么不直接用 alphabetical sort：cheap 应永远先于 expensive，
    但 dict 顺序不稳定；显式映射 + 默认兜底保证语义。
    """
    return _COST_TIER_WEIGHT.get(tier, 1)


async def run_gate(
    artifact_ref: str,
    artifact_type: str,
    pack_id: str,
    ctx: GateContext,
    policy: GatePolicy,
    db: AsyncSession,
    *,
    issued_by: str = "system",
    cert_type: str = "publish",
) -> GateOutcome:
    """执行门编排：按策略链调用验证器，留痕 + 签发证书（若通过）.

    编排规则（架构 v2 §4.3）：
    1. 取链：policy.get_chain(pack_id, artifact_type)。
       精确匹配优先；学科包未配置时回退 platform 通用链。
    2. 廉价先行：按 cost_tier 排序（cheap→expensive），同 tier 内保持策略声明顺序。
    3. 顺序调用，阻断 fail 短路：后续验证器不被调用，但留 short_circuited=True 记录。
    4. 综合判定：
       - 任一阻断 fail → final_verdict='fail'，不签证书。
       - 全部阻断 pass 且有非阻断 review → final_verdict='review'，不签证书。
       - 全部 pass → final_verdict='pass'，签发 GateCertificate。
    5. 落库：
       - pass：INSERT gate_certificate + 每个验证器一条 gate_run + 一条 gate_verdict。
       - fail/review：不 INSERT gate_certificate；每个被调用验证器 INSERT gate_run
         + gate_verdict；未调用的验证器不留库（仅返回 GateRunRecord.short_circuited=True）。

    Args:
        artifact_ref: 被检产物引用（如 item_version_id）。
        artifact_type: 产物类型（item/material/corpus/group/blueprint/audio）。
        pack_id: 学科包 id（'platform' 或 'subject-math' 等）。
        ctx: 验证器运行上下文（产物载荷 + 扩展字段如 db）。
        policy: 已加载的门策略对象。
        db: 异步会话（写入 gate_* 三表 + 查 license 等用）。
        issued_by: 签发人 id（落 gate_certificate.issued_by）；默认 'system'。
        cert_type: 证书类型（'publish'/'retire'）；默认 'publish'。

    Returns:
        GateOutcome：综合判定 + 证书 id（若签发） + 各验证器留痕。

    Raises:
        ValueError: pack_id+artifact_type 无策略链（连 platform 回退都没有）。
    """
    start_total = time.monotonic()

    # 1. 取链
    chain = policy.get_chain(pack_id, artifact_type)
    if not chain:
        raise ValueError(
            f"门策略未配置链：pack_id={pack_id!r} artifact_type={artifact_type!r}"
            "（platform 回退链亦不存在）"
        )

    # 2. 廉价先行排序：稳定排序保持声明顺序
    indexed_chain = list(enumerate(chain))
    indexed_chain.sort(
        key=lambda iv: (
            _cost_tier_weight(_resolve_cost_tier(iv[1].validator_id, pack_id)),
            iv[0],  # 同 tier 内按声明顺序
        )
    )

    # 3. 顺序调用 + 短路
    results: list[tuple[ValidatorResult, bool]] = []  # (result, blocking)
    short_circuit_at: str | None = None
    short_circuited_rest: list[str] = []  # 短路后未调用的 validator_id

    for idx, step in indexed_chain:
        if short_circuit_at is not None:
            short_circuited_rest.append(step.validator_id)
            continue

        # 取验证器实例：先查本 pack，未命中查 platform（与 policy loader 一致）
        validator = _resolve_validator(step.validator_id, pack_id)
        blocking = (
            step.blocking if step.blocking is not None else validator.blocking
        )
        result = await validator.validate(artifact_ref, ctx)
        results.append((result, blocking))

        if result.verdict == "fail" and blocking:
            short_circuit_at = step.validator_id

    # 4. 综合判定
    final_verdict = _compute_final_verdict(results)

    # 5. 签发证书（仅 pass）
    cert_id: str | None = None
    if final_verdict == "pass":
        cert_id = _new_cert_id()
        await _insert_certificate(
            db,
            cert_id=cert_id,
            artifact_ref=artifact_ref,
            cert_type=cert_type,
            policy_version=policy.policy_version,
            issued_by=issued_by,
        )

    # 6. 落库：被调用的验证器写 gate_run + gate_verdict
    # 未调用的（short_circuited_rest）只在返回的 GateRunRecord 中标记 short_circuited=True，不入库
    for result, blocking in results:
        await _insert_run_and_verdict(
            db,
            cert_id=cert_id,  # None 时 gate_run.certificate_id 暂用占位（见下）
            policy_version=policy.policy_version,
            result=result,
            blocking=blocking,
        )

    await db.commit()

    # 7. 构造返回
    run_records: list[GateRunRecord] = []
    # 被调用的：从 results 取
    for (result, blocking) in results:
        run_records.append(
            GateRunRecord(
                validator_id=result.validator_id,
                validator_version=result.version,
                verdict=result.verdict,
                evidence=result.evidence,
                confidence=result.confidence,
                cost_ms=result.cost_ms,
                cost_tokens=result.cost_tokens,
                blocking=blocking,
                short_circuited=False,
            )
        )
    # 未调用的：占位记录（不入库）
    for vid in short_circuited_rest:
        run_records.append(
            GateRunRecord(
                validator_id=vid,
                validator_version="(not called)",
                verdict="review",
                evidence={"reason": "前序阻断验证器 fail，本验证器未被调用"},
                confidence=Decimal("0.000"),
                cost_ms=0,
                cost_tokens=0,
                blocking=False,
                short_circuited=True,
            )
        )

    return GateOutcome(
        final_verdict=final_verdict,
        cert_id=cert_id,
        policy_version=policy.policy_version,
        artifact_ref=artifact_ref,
        runs=run_records,
        short_circuit_at=short_circuit_at,
    )


# ────────────────────────────────────────────────────────────────────
# 辅助
# ────────────────────────────────────────────────────────────────────


def _resolve_validator(validator_id: str, pack_id: str) -> Validator:
    """取验证器实例：先查本 pack，未命中查 platform.

    与 policy loader 的 _validate_validator_ids_exist 保持一致：通用验证器
    在 platform 注册，学科包链可引用 platform + 本 pack 验证器。
    """
    try:
        return get_validator(pack_id, validator_id)
    except KeyError:
        return get_validator("platform", validator_id)


def _resolve_cost_tier(validator_id: str, pack_id: str) -> str:
    """取验证器的 cost_tier（用于廉价先行排序）."""
    try:
        v = _resolve_validator(validator_id, pack_id)
        return v.cost_tier
    except KeyError:
        # 未注册时默认 cheap（编排会因未实现抛 NotImplementedError，但排序不影响）
        return "cheap"


def _compute_final_verdict(
    results: list[tuple[ValidatorResult, bool]],
) -> str:
    """综合判定：任一阻断 fail → fail；全部阻断 pass 但有 review → review；否则 pass.

    为什么 review 不签证书：review=需人工复核，未通过门强制。
    架构 v2 §4.3「物理阻断：发布事务必须持门证书（全部阻断项通过）」——
    全部阻断项 pass 不代表 review 类通过；review 必须先人工裁决。
    """
    has_review = False
    for result, blocking in results:
        if result.verdict == "fail" and blocking:
            return "fail"
        if result.verdict == "review":
            has_review = True
    return "review" if has_review else "pass"


def _new_cert_id() -> str:
    """生成证书 id（ULID，与 writer.py 的 _new_id 同语义）."""
    return "cert_" + str(ulid.new())


# ────────────────────────────────────────────────────────────────────
# 落库
# ────────────────────────────────────────────────────────────────────
# 为什么用裸 SQL 而非 ORM：避免与 ORM session 状态耦合（特别在测试隔离场景）；
# 且 INSERT 逻辑简单，sqlalchemy.text 足够。D1 物理强制由 DB 触发器兜底。


async def _insert_certificate(
    db: AsyncSession,
    *,
    cert_id: str,
    artifact_ref: str,
    cert_type: str,
    policy_version: str,
    issued_by: str,
) -> None:
    """INSERT gate_certificate（仅 pass 时调用）."""
    await db.execute(
        text(
            "INSERT INTO gate_certificate"
            " (cert_id, artifact_ref, cert_type, policy_version, issued_by)"
            " VALUES (:cid, :aref, :ct, :pv, :ib)"
        ),
        {
            "cid": cert_id,
            "aref": artifact_ref,
            "ct": cert_type,
            "pv": policy_version,
            "ib": issued_by,
        },
    )


async def _insert_run_and_verdict(
    db: AsyncSession,
    *,
    cert_id: str | None,
    policy_version: str,
    result: ValidatorResult,
    blocking: bool,
) -> None:
    """INSERT gate_run + gate_verdict.

    为什么 cert_id 为 None 时用占位：gate_run.certificate_id 是 NOT NULL FK
    指向 gate_certificate。失败时不签证书，但 gate_run 仍需落库以留痕。
    W2 占位方案：失败时使用特殊占位 cert_id='cert:none'，gate_certificate 表
    中预插一行 ('cert:none', 'no-cert', 'publish', 'no-policy', 'system') 作为
    失败记录的归属。这样 gate_run.certificate_id 永远有合法 FK 目标。

    迁移 0006 后续会落地该占位行（W3 计划）。当前 W2 测试在 setup 中预插该行。
    """
    target_cert_id = cert_id if cert_id is not None else "cert:none"
    run_id = "run_" + str(ulid.new())
    await db.execute(
        text(
            "INSERT INTO gate_run"
            " (run_id, certificate_id, policy_version, validator_id,"
            " validator_version, verdict, evidence, confidence,"
            " cost_ms, cost_tokens)"
            " VALUES (:rid, :cid, :pv, :vid, :vv, :v, :ev, :cf, :cms, :ct)"
        ),
        {
            "rid": run_id,
            "cid": target_cert_id,
            "pv": policy_version,
            "vid": result.validator_id,
            "vv": result.version,
            "v": result.verdict,
            "ev": _evidence_to_jsonb(result.evidence),
            "cf": float(result.confidence),
            "cms": result.cost_ms,
            "ct": result.cost_tokens,
        },
    )
    # 为什么用 CAST(:detail AS jsonb) 而非 :detail::jsonb：SQLAlchemy text() 的
    # 绑定参数解析器会把 :detail::jsonb 整体当成一个参数名（含 ::），导致参数无法
    # 匹配；asyncpg dialect 最终生成的 SQL 留下字面量 :detail::jsonb 触发语法错误。
    # CAST(... AS jsonb) 是标准 SQL 语法，与 text() 绑定参数无歧义。
    await db.execute(
        text(
            "INSERT INTO gate_verdict (run_id, detail)"
            " VALUES (:rid, CAST(:detail AS jsonb))"
        ),
        {
            "rid": run_id,
            "detail": _detail_to_jsonb(result.evidence, blocking),
        },
    )


def _evidence_to_jsonb(evidence: dict[str, Any]) -> str:
    """将 evidence dict 转为 JSONB 兼容的 JSON 字符串.

    为什么显式转换：asyncpg 对 dict 参数会尝试 dict→jsonb 自动适配，
    但在不同 SQLAlchemy 版本上行为不一致；显式 to_jsonb 函数调用更稳。
    """
    import json

    return json.dumps(evidence, ensure_ascii=False, default=str)


def _detail_to_jsonb(evidence: dict[str, Any], blocking: bool) -> str:
    """构造 gate_verdict.detail（含 evidence + blocking 标记）."""
    import json

    return json.dumps(
        {"evidence": evidence, "blocking": blocking}, ensure_ascii=False, default=str
    )


# ────────────────────────────────────────────────────────────────────
# 异步队列占位（W2 仅接口，真实 worker 后续波次）
# ────────────────────────────────────────────────────────────────────
# 架构 v2 §4.3：「任务队列异步」——W2 不部署真实 Redis worker，仅留接口
# 供后续波次接入。当前调用方走同步 run_gate 即可。


async def run_gate_async(
    artifact_ref: str,
    artifact_type: str,
    pack_id: str,
    ctx: GateContext,
    policy: GatePolicy,
    db: AsyncSession,
    *,
    issued_by: str = "system",
    cert_type: str = "publish",
    queue: str = "gate.default",
) -> str:
    """异步门编排占位：将校验任务入队，返回任务 id.

    W2 占位实现：直接调同步 run_gate（不开 worker），返回伪任务 id。
    后续波次接入真实 Redis 队列后改为入队 + 返回任务 id。

    Returns:
        伪任务 id（W2 占位 = cert_id 或 run 集合的 ULID）。
    """
    outcome = await run_gate(
        artifact_ref=artifact_ref,
        artifact_type=artifact_type,
        pack_id=pack_id,
        ctx=ctx,
        policy=policy,
        db=db,
        issued_by=issued_by,
        cert_type=cert_type,
    )
    # W2 占位：用 cert_id（或 outcome ULID）作为任务 id
    return outcome.cert_id or "task_" + str(ulid.new())


__all__ = [
    "GateOutcome",
    "GateRunRecord",
    "run_gate",
    "run_gate_async",
]
