"""T-W2-011 门证书签发服务.

提供 issue_certificate()——给定一组验证器运行结果，若全部阻断项 pass 则签发
GateCertificate + 关联 GateRun/GateVerdict 落库；任一阻断项未 pass 抛
CertificateIssuanceError，不签发任何东西。

定位（与 T-W2-010 编排器的分工）：
- 编排器（orchestrator.run_gate）：调用验证器链 + 决定 final_verdict + 内部
  落库（pass 时签证书 + 三表 INSERT；fail/review 时只 INSERT gate_run/verdict
  到占位 cert_id='cert:none'）。
- 本服务（certifier.issue_certificate）：将「签发证书」从编排器中抽出为可复用
  服务——供已有验证结果的调用方（如内容写入服务 publish 时复检、批量补校验、
  退役签发）独立签发证书。本服务不调用验证器，只做签发与留痕。

落地契约 §4 规则 1（门强制）：
- 内容写入服务 publish_item_version 在 status='published' 时要求 gate_certificate_id
  非空；该 id 必须由本服务 issue_certificate 路径产出。
- DB 层兜底：item_version.ck_iv_published_requires_gate_cert CHECK 约束 +
  serving_reader 角色无 INSERT 权限（迁移 0006）+ append-only 触发器三层防护。

宪法 D1：三本账只增不改——本服务仅 INSERT，无 UPDATE/DELETE。
宪法 A5/X6：核心域零学科特判，本模块不 import 任何学科包/学段包。
"""
from __future__ import annotations

import json
import ulid
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.gate.validator import ValidatorResult


# ────────────────────────────────────────────────────────────────────
# 异常
# ────────────────────────────────────────────────────────────────────


class CertificateIssuanceError(ValueError):
    """签发失败：阻断项未全部 pass 或参数非法。

    调用方应捕获此异常并保留失败现场（不签发证书），由人工或上层流程裁决。
    """


# ────────────────────────────────────────────────────────────────────
# 公共入口
# ────────────────────────────────────────────────────────────────────

# 验证器结果 + 阻断标记的元组类型（与 orchestrator.run_gate 内部一致）。
# blocking=True 表示该验证器是阻断项——fail 时不允许签发证书。
IssuableRun = tuple[ValidatorResult, bool]


async def issue_certificate(
    *,
    artifact_ref: str,
    cert_type: str,
    policy_version: str,
    issued_by: str,
    runs: list[IssuableRun],
    db: AsyncSession,
) -> str:
    """签发门证书：仅在全部阻断项 pass 时生成 cert_id 并 INSERT 三表.

    签发规则（契约 §4 规则 1 / §4.3 物理阻断）：
    1. runs 非空——空 runs 视为「未跑任何验证器」，禁止签发。
    2. 全部阻断项 verdict == 'pass'——任一阻断 fail/review 都拒绝签发
       （review 需人工裁决，未通过门强制；与 orchestrator._compute_final_verdict
       对 pass 的判定一致）。
    3. INSERT gate_certificate（cert_id = 'cert_<ULID>'）。
    4. 关联每个 run 落 gate_run + gate_verdict（certificate_id = 新 cert_id）。
    5. commit；返回 cert_id。

    为什么不在签发后再 UPDATE gate_run 关联 cert_id：D1 三本账只增不改，
    gate_run 不允许 UPDATE。本服务一次性 INSERT cert + runs + verdicts，
    避免「先 INSERT 临时 cert_id 后 UPDATE 替换」的反模式。

    Args:
        artifact_ref: 被签发的产物引用（如 item_version_id）。
        cert_type: 证书类型（'publish'/'retire'）。
        policy_version: 门策略版本（落 gate_run.policy_version）。
        issued_by: 签发人 id（落 gate_certificate.issued_by）。
        runs: 已运行的验证器结果列表 [(ValidatorResult, blocking), ...]。
            blocking=True 表示该验证器是阻断项。
        db: AsyncSession（写入 gate_* 三表）。

    Returns:
        cert_id（已签发的证书 id，'cert_<ULID>' 格式）。

    Raises:
        CertificateIssuanceError:
            - runs 为空（无验证器运行记录）。
            - 任一阻断项 verdict != 'pass'（含 fail 与 review）。
    """
    if not runs:
        raise CertificateIssuanceError(
            "签发失败：runs 为空（必须至少有一个验证器运行记录）"
        )

    # 1. 校验：全部阻断项必须 pass
    for result, blocking in runs:
        if blocking and result.verdict != "pass":
            raise CertificateIssuanceError(
                f"签发失败：阻断验证器 {result.validator_id!r} "
                f"verdict={result.verdict!r}（非 pass，禁止签发证书）"
            )

    # 2. 生成 cert_id 并 INSERT gate_certificate
    cert_id = "cert_" + str(ulid.new())
    await _insert_certificate(
        db,
        cert_id=cert_id,
        artifact_ref=artifact_ref,
        cert_type=cert_type,
        policy_version=policy_version,
        issued_by=issued_by,
    )

    # 3. 关联每个 run 落 gate_run + gate_verdict
    for result, blocking in runs:
        await _insert_run_and_verdict(
            db,
            cert_id=cert_id,
            policy_version=policy_version,
            result=result,
            blocking=blocking,
        )

    await db.commit()
    return cert_id


# ────────────────────────────────────────────────────────────────────
# 落库（与 orchestrator 同语义的私有实现；不复用以避免跨模块耦合）
# ────────────────────────────────────────────────────────────────────
# 为什么用裸 SQL 而非 ORM：避免与 ORM session 状态耦合；INSERT 逻辑简单，
# sqlalchemy.text 足够。D1 物理强制由 DB 触发器兜底（迁移 0004）。


async def _insert_certificate(
    db: AsyncSession,
    *,
    cert_id: str,
    artifact_ref: str,
    cert_type: str,
    policy_version: str,
    issued_by: str,
) -> None:
    """INSERT gate_certificate（签发证书行）."""
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
    cert_id: str,
    policy_version: str,
    result: ValidatorResult,
    blocking: bool,
) -> None:
    """INSERT gate_run + gate_verdict（关联到已签发的 cert_id）."""
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
            "cid": cert_id,
            "pv": policy_version,
            "vid": result.validator_id,
            "vv": result.version,
            "v": result.verdict,
            "ev": _to_jsonb_str(result.evidence),
            "cf": float(result.confidence),
            "cms": result.cost_ms,
            "ct": result.cost_tokens,
        },
    )
    # 为什么用 CAST(:detail AS jsonb) 而非 :detail::jsonb：SQLAlchemy text() 的
    # 绑定参数解析器会把 :detail::jsonb 整体当作参数名（含 ::），导致参数无法
    # 匹配；asyncpg dialect 生成的 SQL 留下字面量 :detail::jsonb 触发语法错误。
    # CAST(... AS jsonb) 是标准 SQL 语法，与 text() 绑定参数无歧义。
    await db.execute(
        text(
            "INSERT INTO gate_verdict (run_id, detail)"
            " VALUES (:rid, CAST(:detail AS jsonb))"
        ),
        {
            "rid": run_id,
            "detail": _detail_to_jsonb_str(result.evidence, blocking),
        },
    )


def _to_jsonb_str(obj: Any) -> str:
    """将 dict/list 转 JSONB 兼容的 JSON 字符串.

    为什么显式序列化：asyncpg 对 dict 参数会尝试 dict→jsonb 自动适配，
    但在不同 SQLAlchemy 版本上行为不一致；显式 json.dumps + 让 PG 端隐式
    cast 字符串到 jsonb 更稳。
    """
    return json.dumps(obj, ensure_ascii=False, default=str)


def _detail_to_jsonb_str(evidence: dict[str, Any], blocking: bool) -> str:
    """构造 gate_verdict.detail（含 evidence + blocking 标记）."""
    return json.dumps(
        {"evidence": evidence, "blocking": blocking},
        ensure_ascii=False,
        default=str,
    )


__all__ = ["CertificateIssuanceError", "issue_certificate"]
