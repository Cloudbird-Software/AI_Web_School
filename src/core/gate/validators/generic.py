"""T-W2-009 通用验证器 v1（结构/许可/查重占位）.

三个平台通用验证器（pack_id='platform'），按统一契约返回 ValidatorResult：
- SchemaValidator：结构/schema 校验（JSON Schema 子集：type/required/properties/enum）。
- LicenseValidator：素材/语料许可校验（license_id 存在 + decision=approved + 未过期）。
- DuplicatePlaceholderValidator：规范化哈希查重占位（与已 published 版本比对，重复则 review）。

覆盖 T-W2-008 loader.py 中的桩声明——本模块 import 时 register_validator 同 key
覆盖桩，真实实现生效。

宪法 A5/X6：不 import 任何学科包/学段包；宪法 D2：门强制物理阻断由 DB 触发器兜底，
本模块只做内容校验，不绕过写入服务。

为什么 DuplicatePlaceholderValidator 是「占位」：W2 仅做规范化哈希层查重（公式二），
语义查重（MinHash/向量）与外部真题指纹为后续波次（任务卡 non_goals 明示）。
"""
from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.gate.validator import (
    GateContext,
    Validator,
    ValidatorResult,
    register_validator,
)


# ────────────────────────────────────────────────────────────────────
# 辅助：规范化哈希（与 content_addressing.py 同语义，本地实现避免 import 私有符号）
# ────────────────────────────────────────────────────────────────────

def _canonical_json(obj: Any) -> str:
    """规范化 JSON：键序升序、无多余空白、UTF-8 直出（D3 可复现基础）."""
    return json.dumps(
        obj, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )


def _canonical_hash(obj: Any) -> str:
    """计算规范化 SHA-256 摘要，加 sha256: 前缀."""
    return "sha256:" + hashlib.sha256(_canonical_json(obj).encode("utf-8")).hexdigest()


# ────────────────────────────────────────────────────────────────────
# SchemaValidator：结构 / JSON Schema 校验
# ────────────────────────────────────────────────────────────────────
# 实现 JSON Schema 2020-12 子集：type / required / properties / enum /
# additionalProperties。足以校验契约 §5.1 objective / §5.2 lineage schema。
# 为什么不引 jsonschema 依赖：W2 校验需求是结构校验，最小子集够用且无新依赖
# （规则：新依赖必须说明理由并更新锁定文件）；DSL Linter 为后续波次。


_TYPE_MAP: dict[str, type | tuple[type, ...]] = {
    "object": dict,
    "array": list,
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
    "null": type(None),
}


def _validate_schema(value: Any, schema: dict, path: str = "$") -> list[str]:
    """递归校验 value 是否符合 schema（JSON Schema 子集）.

    Returns:
        错误信息列表（空表示通过）。
    """
    errors: list[str] = []

    # type
    t = schema.get("type")
    if t is not None:
        expected = _TYPE_MAP.get(t)
        if expected is not None and not isinstance(value, expected):
            errors.append(f"{path}: 期望 type={t}，实际 {type(value).__name__}")
            return errors  # 类型不符则后续无意义

    # enum
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: 值 {value!r} 不在 enum {schema['enum']}")
        return errors

    # object: required / properties / additionalProperties
    if isinstance(value, dict):
        required = schema.get("required", [])
        for k in required:
            if k not in value:
                errors.append(f"{path}: 缺必填键 {k!r}")
        props = schema.get("properties", {})
        for k, v in value.items():
            if k in props:
                errors.extend(_validate_schema(v, props[k], f"{path}.{k}"))
            elif schema.get("additionalProperties") is False:
                errors.append(f"{path}: 多余键 {k!r}（additionalProperties=false）")

    # array: items
    if isinstance(value, list) and "items" in schema:
        item_schema = schema["items"]
        for i, v in enumerate(value):
            errors.extend(_validate_schema(v, item_schema, f"{path}[{i}]"))

    return errors


class SchemaValidator(Validator):
    """结构验证器：校验 artifact_payload 符合 ctx.json_schema（JSON Schema 子集）.

    ctx 字段（extra='allow'）：
    - json_schema: dict——JSON Schema（type/required/properties/enum/additionalProperties）。
    - required_keys: list[str]——简易模式（无 json_schema 时仅校验必填键存在）。

    verdict 规则：
    - review：无 json_schema 且无 required_keys（无法机器校验）。
    - review：artifact_payload 为 None。
    - fail：json_schema 校验失败或缺必填键。
    - pass：校验通过。

    为什么字段叫 json_schema 而非 schema：Pydantic v2 BaseModel 仍保留 .schema()
    类方法（v1 兼容别名）；用 schema 作 extra 键会被类方法遮蔽，getattr(ctx,'schema')
    返回的是 bound method 而非字典。改用 json_schema 规避名字冲突。
    """

    validator_id = "schema"
    version = "1.0.0+generic"
    blocking = True
    cost_tier = "cheap"

    async def validate(self, artifact_ref: str, ctx: GateContext) -> ValidatorResult:
        start = time.monotonic()
        schema: dict | None = ctx.model_dump().get("json_schema")
        required_keys: list[str] = list(ctx.model_dump().get("required_keys", []) or [])
        payload = ctx.artifact_payload
        elapsed_ms = lambda: int((time.monotonic() - start) * 1000)

        if payload is None:
            return self._timed_result(
                verdict="review",
                evidence={"reason": "artifact_payload 为 None，无法机器校验结构"},
                confidence=Decimal("0.000"),
                elapsed_ms=elapsed_ms(),
            )

        # 简易模式：仅校验必填键
        if schema is None:
            if not required_keys:
                return self._timed_result(
                    verdict="review",
                    evidence={"reason": "未提供 json_schema 或 required_keys，无法校验"},
                    confidence=Decimal("0.000"),
                    elapsed_ms=elapsed_ms(),
                )
            missing = [k for k in required_keys if k not in payload]
            if missing:
                return self._timed_result(
                    verdict="fail",
                    evidence={"missing_keys": missing, "required_keys": required_keys},
                    confidence=Decimal("1.000"),
                    elapsed_ms=elapsed_ms(),
                )
            return self._timed_result(
                verdict="pass",
                evidence={"checked_keys": required_keys, "mode": "required_keys"},
                confidence=Decimal("1.000"),
                elapsed_ms=elapsed_ms(),
            )

        # JSON Schema 模式
        errors = _validate_schema(payload, schema)
        if errors:
            return self._timed_result(
                verdict="fail",
                evidence={"errors": errors, "mode": "json_schema"},
                confidence=Decimal("1.000"),
                elapsed_ms=elapsed_ms(),
            )
        return self._timed_result(
            verdict="pass",
            evidence={"mode": "json_schema", "checked": True},
            confidence=Decimal("1.000"),
            elapsed_ms=elapsed_ms(),
        )


# ────────────────────────────────────────────────────────────────────
# LicenseValidator：许可校验
# ────────────────────────────────────────────────────────────────────
# 契约 §2.4：material_version.license_id FK→material_license；
# serving 规则：过期许可素材不得用于新组卷。
# 校验：license_id 存在 + decision=approved + （expires_at 为空或未过期）。
# 为什么用裸 SQL 而非 ORM：避免与 ORM 模型耦合，且查询简单。


class LicenseValidator(Validator):
    """许可验证器：校验 license_id 合法且未过期.

    ctx 字段：
    - db: AsyncSession（必填，查 material_license 表）。
    - license_id: str（优先取 ctx，其次取 artifact_payload['license_id']）。

    verdict 规则：
    - fail：license_id 缺失 / 未找到 / decision≠approved / 已过期。
    - pass：合法且未过期。
    - review：未提供 db（无法查证）。
    """

    validator_id = "license"
    version = "1.0.0+generic"
    blocking = True
    cost_tier = "cheap"

    async def validate(self, artifact_ref: str, ctx: GateContext) -> ValidatorResult:
        start = time.monotonic()
        db: AsyncSession | None = getattr(ctx, "db", None)
        license_id: str | None = getattr(ctx, "license_id", None)
        if not license_id and ctx.artifact_payload:
            license_id = ctx.artifact_payload.get("license_id")
        elapsed_ms = lambda: int((time.monotonic() - start) * 1000)

        if not license_id:
            return self._timed_result(
                verdict="fail",
                evidence={"reason": "未提供 license_id"},
                confidence=Decimal("1.000"),
                elapsed_ms=elapsed_ms(),
            )

        if db is None:
            return self._timed_result(
                verdict="review",
                evidence={"reason": "未提供 db，无法查证 license", "license_id": license_id},
                confidence=Decimal("0.000"),
                elapsed_ms=elapsed_ms(),
            )

        row = (
            await db.execute(
                text(
                    "SELECT decision, expires_at FROM material_license"
                    " WHERE license_id = :lid"
                ),
                {"lid": license_id},
            )
        ).one_or_none()

        if row is None:
            return self._timed_result(
                verdict="fail",
                evidence={"reason": "license_id 未找到", "license_id": license_id},
                confidence=Decimal("1.000"),
                elapsed_ms=elapsed_ms(),
            )

        decision, expires_at = row
        if decision != "approved":
            return self._timed_result(
                verdict="fail",
                evidence={
                    "reason": f"license decision={decision}，非 approved",
                    "license_id": license_id,
                },
                confidence=Decimal("1.000"),
                elapsed_ms=elapsed_ms(),
            )

        # 过期校验：expires_at 为空视为永久；非空则比较 now
        if expires_at is not None:
            now = datetime.now(timezone.utc)
            if expires_at <= now:
                return self._timed_result(
                    verdict="fail",
                    evidence={
                        "reason": "license 已过期",
                        "license_id": license_id,
                        "expires_at": expires_at.isoformat(),
                    },
                    confidence=Decimal("1.000"),
                    elapsed_ms=elapsed_ms(),
                )

        return self._timed_result(
            verdict="pass",
            evidence={
                "license_id": license_id,
                "decision": decision,
                "expires_at": expires_at.isoformat() if expires_at else None,
            },
            confidence=Decimal("1.000"),
            elapsed_ms=elapsed_ms(),
        )


# ────────────────────────────────────────────────────────────────────
# DuplicatePlaceholderValidator：规范化哈希查重占位
# ────────────────────────────────────────────────────────────────────
# 契约 §3 公式二：同一内容必得同一 id；重复命题/粘贴产生同 id，
# 入库时作去重提示而非拒绝（D3 精神）。
# W2 占位：规范化哈希层查重；语义查重（MinHash/向量）为后续波次（non_goals）。
#
# 为什么 blocking=False：重复是「提示」而非「阻断」——同一内容可能是有意复用
# （如不同学段同一题），由人工裁决；编排器不因重复短路。


_PUBLISHED_HASH_QUERY: dict[str, str] = {
    # artifact_type → 查询已 published 版本是否已存在该内容哈希
    "item": "SELECT 1 FROM item_version WHERE item_version_id = :h AND status = 'published' LIMIT 1",
    "material": "SELECT 1 FROM material_version WHERE material_version_id = :h AND status = 'published' LIMIT 1",
    "corpus": "SELECT 1 FROM corpus_version WHERE version_id = :h AND status = 'published' LIMIT 1",
}


class DuplicatePlaceholderValidator(Validator):
    """查重占位验证器：规范化哈希与已 published 版本比对.

    ctx 字段：
    - db: AsyncSession（必填，查 published 版本表）。
    - artifact_payload: dict——被检产物内容（计算规范化哈希）。

    verdict 规则：
    - review：发现重复（已 published 版本含同哈希）——提示人工复核。
    - pass：无重复。
    - review：artifact_type 无对应查重表（group/blueprint/audio 暂不查）。
    - review：未提供 db 或 payload（无法查证）。
    """

    validator_id = "duplicate_placeholder"
    version = "1.0.0+generic"
    blocking = False  # 查重提示不阻断
    cost_tier = "cheap"

    async def validate(self, artifact_ref: str, ctx: GateContext) -> ValidatorResult:
        start = time.monotonic()
        db: AsyncSession | None = getattr(ctx, "db", None)
        payload = ctx.artifact_payload
        artifact_type = ctx.artifact_type
        elapsed_ms = lambda: int((time.monotonic() - start) * 1000)

        if payload is None:
            return self._timed_result(
                verdict="review",
                evidence={"reason": "artifact_payload 为 None，无法计算哈希"},
                confidence=Decimal("0.000"),
                elapsed_ms=elapsed_ms(),
            )

        query_sql = _PUBLISHED_HASH_QUERY.get(artifact_type)
        if query_sql is None:
            return self._timed_result(
                verdict="review",
                evidence={
                    "reason": f"artifact_type={artifact_type!r} 暂无查重表",
                    "artifact_type": artifact_type,
                },
                confidence=Decimal("0.000"),
                elapsed_ms=elapsed_ms(),
            )

        if db is None:
            return self._timed_result(
                verdict="review",
                evidence={"reason": "未提供 db，无法查证重复"},
                confidence=Decimal("0.000"),
                elapsed_ms=elapsed_ms(),
            )

        digest = _canonical_hash(payload)
        hit = (
            await db.execute(text(query_sql), {"h": digest})
        ).first()

        if hit is not None:
            return self._timed_result(
                verdict="review",
                evidence={
                    "reason": "发现重复：已 published 版本含同规范化哈希",
                    "canonical_hash": digest,
                    "artifact_type": artifact_type,
                    "action": "请人工确认是否为有意复用",
                },
                confidence=Decimal("0.900"),
                elapsed_ms=elapsed_ms(),
            )

        return self._timed_result(
            verdict="pass",
            evidence={
                "canonical_hash": digest,
                "artifact_type": artifact_type,
                "checked_published": True,
            },
            confidence=Decimal("1.000"),
            elapsed_ms=elapsed_ms(),
        )


# ────────────────────────────────────────────────────────────────────
# 注册：覆盖 T-W2-008 loader.py 中的桩
# ────────────────────────────────────────────────────────────────────
# 为什么无条件 register：同 (pack_id, validator_id) 后注册覆盖先注册（register_validator
# 直接赋值），T-W2-008 桩的 NotImplementedError 被本模块真实实现取代。
register_validator("platform", SchemaValidator)
register_validator("platform", LicenseValidator)
register_validator("platform", DuplicatePlaceholderValidator)
