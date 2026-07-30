"""T-W4-032 家长授权记录：版本化 / 范围 / 时间 / 撤回（append-only）.

落地架构 v2 §4.8 与宪法 D7 家长授权前置：
- 版本化：每次授权/撤回写新行（version 单调递增），旧版本保留。
- 范围：scope JSONB，必含 purpose（如 "practice" / "diagnosis" / "measurement"）；
  可含 subject（学科）/ time_period（时段）等扩展维度。
- 时间：grant 事件有 valid_from / valid_until；revoke 事件两者为 NULL。
- 撤回：revoke_consent 写新行 event_type='revoke'，原 grant 立即失效
  （check_consent 见最新 revoke 即返回 False），历史保留。
- append-only：DB 触发器物理强制 UPDATE/DELETE（迁移 0015）。

「旧版本标记过期时间戳」的承载方式：
- 不 UPDATE 旧行（违反 append-only）；
- 旧版本的有效截止 = 后续事件的 created_at（grant 的实际失效时刻 =
  min(valid_until, next_event.created_at)）。check_consent 查最新事件判定。

宪法 A5/X6：本模块不 import 任何学科包/学段包。
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional, Union

from sqlalchemy import func, text
from sqlalchemy.ext.asyncio import AsyncSession


# ────────────────────────────────────────────────────────────────────
# 异常
# ────────────────────────────────────────────────────────────────────

class ConsentScopeError(ValueError):
    """授权 scope 非法（缺 purpose / 类型错误）."""


class NoActiveConsentError(LookupError):
    """无有效授权可撤回（学生从未授权该 purpose）."""


# ────────────────────────────────────────────────────────────────────
# DTO
# ────────────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class ConsentStatus:
    """授权状态查询结果.

    - granted: 当前有有效授权（最新事件为 grant 且未过期）
    - revoked: 已撤回（最新事件为 revoke）
    - expired: 已过期（最新事件为 grant 但 valid_until < now）
    - missing: 从未授权
    """

    student_alias_id: uuid.UUID
    purpose: str
    state: str  # granted / revoked / expired / missing
    version: Optional[int] = None
    valid_from: Optional[datetime] = None
    valid_until: Optional[datetime] = None

    @property
    def is_valid(self) -> bool:
        """便捷谓词：当前是否有效授权."""
        return self.state == "granted"


# ────────────────────────────────────────────────────────────────────
# scope 规整
# ────────────────────────────────────────────────────────────────────

def _normalize_scope(scope: Union[str, dict[str, Any]]) -> dict[str, Any]:
    """规整 scope 为 dict：必含 purpose 键.

    - 字符串：视为 purpose，包装为 {"purpose": purpose}
    - dict：必须有 "purpose" 键
    """
    if isinstance(scope, str):
        if not scope:
            raise ConsentScopeError("purpose 不得为空字符串")
        return {"purpose": scope}
    if isinstance(scope, dict):
        purpose = scope.get("purpose")
        if not purpose or not isinstance(purpose, str):
            raise ConsentScopeError(
                "scope dict 必须含非空字符串键 'purpose'"
            )
        return dict(scope)
    raise ConsentScopeError(
        f"scope 必须是 str 或 dict，收到 {type(scope).__name__}"
    )


def _purpose_of(scope: Union[str, dict[str, Any]]) -> str:
    """从 scope 提取 purpose 字符串."""
    return _normalize_scope(scope)["purpose"]


# ────────────────────────────────────────────────────────────────────
# DB 操作（与 alembic/versions/0015_parental_consent.py 表结构对齐）
# ────────────────────────────────────────────────────────────────────

# 查最新版本号（用于 version 单调递增）
_LATEST_VERSION_SQL = text(
    """
    SELECT COALESCE(MAX(version), 0)
      FROM parental_consent
     WHERE student_alias_id = :sid
       AND scope ->> 'purpose' = :purpose
    """
)

# 写入新事件（原子版本：INSERT ... SELECT COALESCE(MAX,0)+1）
# 替代旧的 SELECT MAX + INSERT 两步模式，消除版本号冲突竞态（BUG-PC1）。
# 迁移 0023 已对 (student_alias_id, scope->>'purpose', version) 加唯一索引兜底：
# 若并发 INSERT 仍冲突（极端时序），调用方捕获 IntegrityError 后重试。
_INSERT_EVENT_ATOMIC_SQL = text(
    """
    INSERT INTO parental_consent
        (consent_id, student_alias_id, event_type, scope,
         valid_from, valid_until, version)
    SELECT
        :cid, :sid, :etype, CAST(:scope AS jsonb),
        :vfrom, :vuntil,
        COALESCE((SELECT MAX(version)
                    FROM parental_consent
                   WHERE student_alias_id = :sid
                     AND scope ->> 'purpose' = :purpose), 0) + 1
    """
)

# 查最新事件（按 created_at DESC / version DESC 取首条）
_LATEST_EVENT_SQL = text(
    """
    SELECT consent_id, event_type, scope,
           valid_from, valid_until, version, created_at
      FROM parental_consent
     WHERE student_alias_id = :sid
       AND scope ->> 'purpose' = :purpose
     ORDER BY version DESC
     LIMIT 1
    """
)


async def _insert_event_atomic(
    db: AsyncSession,
    *,
    consent_id: uuid.UUID,
    student_alias_id: uuid.UUID,
    event_type: str,
    scope_json: str,
    purpose: str,
    valid_from: Optional[datetime],
    valid_until: Optional[datetime],
) -> None:
    """原子写入新授权事件：单条 SQL 计算 version 并 INSERT（BUG-PC1 修复）.

    唯一冲突（并发同一学生同一 purpose 写入）时抛 IntegrityError，
    由调用方（或上层事务）重试。
    """
    await db.execute(
        _INSERT_EVENT_ATOMIC_SQL,
        {
            "cid": consent_id,
            "sid": student_alias_id,
            "etype": event_type,
            "scope": scope_json,
            "purpose": purpose,
            "vfrom": valid_from,
            "vuntil": valid_until,
        },
    )


# ────────────────────────────────────────────────────────────────────
# 公开 API
# ────────────────────────────────────────────────────────────────────

async def record_consent(
    db: AsyncSession,
    *,
    student_alias_id: uuid.UUID,
    scope: Union[str, dict[str, Any]],
    valid_until: datetime,
    valid_from: Optional[datetime] = None,
    now: Optional[datetime] = None,
) -> uuid.UUID:
    """写入新版本授权记录（grant 事件），旧版本隐式失效.

    Args:
        db: 异步会话。
        student_alias_id: 匿名学生 id。
        scope: 授权范围（字符串=purpose；dict 须含 purpose 键）。
        valid_until: 授权有效期截止时刻。
        valid_from: 授权生效时刻（默认 now）。
        now: 当前时刻（测试注入；默认 valid_from 或当前 UTC）。

    Returns:
        新授权记录的 consent_id。

    Raises:
        ConsentScopeError: scope 非法。
    """
    norm_scope = _normalize_scope(scope)
    purpose = norm_scope["purpose"]
    ts = now or valid_from or datetime.now(timezone.utc)
    vfrom = valid_from or ts
    if valid_until <= vfrom:
        raise ValueError(
            f"valid_until({valid_until}) 须晚于 valid_from({vfrom})"
        )

    consent_id = uuid.uuid4()
    import json
    # BUG-PC1 修复：原子 INSERT（版本号由子查询在单条 SQL 内计算）
    # 唯一冲突（并发写入）时抛 IntegrityError → 调用方重试
    await _insert_event_atomic(
        db,
        consent_id=consent_id,
        student_alias_id=student_alias_id,
        event_type="grant",
        scope_json=json.dumps(norm_scope, ensure_ascii=False),
        purpose=purpose,
        valid_from=vfrom,
        valid_until=valid_until,
    )
    # commit 由调用方控制
    return consent_id


async def revoke_consent(
    db: AsyncSession,
    *,
    student_alias_id: uuid.UUID,
    scope: Union[str, dict[str, Any]],
    now: Optional[datetime] = None,
) -> uuid.UUID:
    """写入撤回记录（revoke 事件），原授权立即失效.

    语义：append-only 写一条 revoke 事件；check_consent 见最新事件为 revoke
    即返回 False。无有效授权可撤回时抛 NoActiveConsentError。

    Args:
        db: 异步会话。
        student_alias_id: 匿名学生 id。
        scope: 授权范围（与 record_consent 同口径）。
        now: 撤回时刻（测试注入）。

    Returns:
        撤回记录的 consent_id。

    Raises:
        ConsentScopeError: scope 非法。
        NoActiveConsentError: 无有效授权可撤回。
    """
    norm_scope = _normalize_scope(scope)
    purpose = norm_scope["purpose"]
    ts = now or datetime.now(timezone.utc)

    # 校验：当前须有有效授权才能撤回（否则审计噪声）
    status = await check_consent(db, student_alias_id, purpose, now=ts)
    if not status.is_valid:
        raise NoActiveConsentError(
            f"学生 {student_alias_id} 无有效的 {purpose!r} 授权可撤回"
            f"（当前状态：{status.state}）"
        )

    consent_id = uuid.uuid4()
    import json
    # BUG-PC1 修复：原子 INSERT（版本号由子查询在单条 SQL 内计算）
    await _insert_event_atomic(
        db,
        consent_id=consent_id,
        student_alias_id=student_alias_id,
        event_type="revoke",
        scope_json=json.dumps(norm_scope, ensure_ascii=False),
        purpose=purpose,
        valid_from=None,
        valid_until=None,
    )
    return consent_id


async def check_consent(
    db: AsyncSession,
    student_alias_id: uuid.UUID,
    scope: Union[str, dict[str, Any]],
    *,
    now: Optional[datetime] = None,
) -> ConsentStatus:
    """查询当前有效授权状态.

    判定逻辑（按最新事件）：
    - 无记录 → missing
    - 最新为 revoke → revoked
    - 最新为 grant 且 now < valid_until → granted
    - 最新为 grant 且 now >= valid_until → expired

    Args:
        db: 异步会话。
        student_alias_id: 匿名学生 id。
        scope: 授权范围（字符串=purpose；dict 取 purpose 键）。
        now: 判定时刻（测试注入；默认当前 UTC）。
    """
    purpose = _purpose_of(scope)
    ts = now or datetime.now(timezone.utc)
    row = (await db.execute(
        _LATEST_EVENT_SQL, {"sid": student_alias_id, "purpose": purpose}
    )).first()

    if row is None:
        return ConsentStatus(
            student_alias_id=student_alias_id,
            purpose=purpose,
            state="missing",
        )

    if row.event_type == "revoke":
        return ConsentStatus(
            student_alias_id=student_alias_id,
            purpose=purpose,
            state="revoked",
            version=row.version,
        )

    # grant 事件：检查有效期
    assert row.valid_until is not None
    if ts >= row.valid_until:
        return ConsentStatus(
            student_alias_id=student_alias_id,
            purpose=purpose,
            state="expired",
            version=row.version,
            valid_from=row.valid_from,
            valid_until=row.valid_until,
        )

    return ConsentStatus(
        student_alias_id=student_alias_id,
        purpose=purpose,
        state="granted",
        version=row.version,
        valid_from=row.valid_from,
        valid_until=row.valid_until,
    )


async def list_consent_history(
    db: AsyncSession,
    student_alias_id: uuid.UUID,
    scope: Union[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """列出授权变更历史（审计用，按 version 升序）."""
    purpose = _purpose_of(scope)
    rows = (await db.execute(
        text(
            """
            SELECT consent_id, event_type, scope,
                   valid_from, valid_until, version, created_at
              FROM parental_consent
             WHERE student_alias_id = :sid
               AND scope ->> 'purpose' = :purpose
             ORDER BY version ASC
            """
        ),
        {"sid": student_alias_id, "purpose": purpose},
    )).all()
    return [
        {
            "consent_id": str(r.consent_id),
            "event_type": r.event_type,
            "scope": r.scope,
            "valid_from": r.valid_from,
            "valid_until": r.valid_until,
            "version": r.version,
            "created_at": r.created_at,
        }
        for r in rows
    ]


__all__ = [
    "ConsentScopeError",
    "ConsentStatus",
    "NoActiveConsentError",
    "check_consent",
    "list_consent_history",
    "record_consent",
    "revoke_consent",
]
