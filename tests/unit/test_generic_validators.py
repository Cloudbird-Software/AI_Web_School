"""T-W2-009 通用验证器 v1 单元测试.

对照任务卡验收标准逐条覆盖：
1. SchemaValidator 调用 JSON Schema 校验，返回 pass/fail。
2. LicenseValidator 检查 license_id 存在且 decision=approved、未过期。
3. DuplicatePlaceholderValidator 计算 canonical hash，与已 published 版本比对，重复则 review。
4. 单元测试覆盖通过、结构失败、许可失败、重复提示四种情况。

宪法 A5/X6：核心域零学科特判；D2：门强制物理阻断由 DB 触发器兜底。
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.gate.validator import (
    GateContext,
    get_validator,
    list_validators,
    register_validator,
)
from src.core.gate.validators.generic import (
    DuplicatePlaceholderValidator,
    LicenseValidator,
    SchemaValidator,
    _canonical_hash,
)


# ────────────────────────────────────────────────────────────────────
# DB 隔离 fixture：每测试前清理相关表
# ────────────────────────────────────────────────────────────────────
@pytest_asyncio.fixture(autouse=True)
async def _truncate_tables(async_session: AsyncSession):
    """清理 material_license / item_version / item / material_version / corpus_version.

    为什么 TRUNCATE CASCADE：item_version→item 有 FK；material_version→material 同理。
    """
    await async_session.execute(
        text(
            "TRUNCATE TABLE item_version, item, material_version, material,"
            " corpus_version, corpus_asset, material_license"
            " RESTART IDENTITY CASCADE"
        )
    )
    await async_session.commit()
    yield


# ────────────────────────────────────────────────────────────────────
# 注册表隔离 fixture：每测试前重注册通用验证器
# ────────────────────────────────────────────────────────────────────
# 为什么需要这个 fixture：test_gate_validator_base.py 的 _isolated_registry
# 在 teardown 后只重新注册 SchemaValidator(example)，把 generic.py 的三个真实
# 验证器（schema/license/duplicate_placeholder）覆盖回只剩 schema 一个。
# 此 fixture 在每测试前重新注册 generic.py 的三个真实验证器，保证测试看到
# 完整的 platform 注册表。
@pytest.fixture(autouse=True)
def _re_register_generic_validators():
    """每测试前重注册通用验证器（覆盖回真实实现）."""
    register_validator("platform", SchemaValidator)
    register_validator("platform", LicenseValidator)
    register_validator("platform", DuplicatePlaceholderValidator)
    yield


# ────────────────────────────────────────────────────────────────────
# §1 SchemaValidator
# ────────────────────────────────────────────────────────────────────

async def test_schema_validator_pass_with_json_schema():
    """验收 #1：payload 符合 JSON Schema → pass."""
    schema = {
        "type": "object",
        "required": ["objective", "content"],
        "properties": {
            "objective": {"type": "object"},
            "content": {"type": "object"},
            "cognitive_level": {"type": "string"},
        },
    }
    ctx = GateContext(
        artifact_type="item",
        pack_id="platform",
        artifact_payload={"objective": {}, "content": {}, "cognitive_level": "apply"},
        json_schema=schema,
    )
    r = await SchemaValidator().validate("sha256:item-v1", ctx)
    assert r.verdict == "pass"
    assert r.validator_id == "schema"
    assert r.version == "1.0.0+generic"
    assert r.evidence["mode"] == "json_schema"


async def test_schema_validator_fail_missing_required():
    """验收 #4：缺必填键 → fail."""
    schema = {
        "type": "object",
        "required": ["objective", "content"],
        "properties": {"objective": {"type": "object"}, "content": {"type": "object"}},
    }
    ctx = GateContext(
        artifact_type="item",
        pack_id="platform",
        artifact_payload={"objective": {}},  # 缺 content
        json_schema=schema,
    )
    r = await SchemaValidator().validate("sha256:item-v2", ctx)
    assert r.verdict == "fail"
    assert any("content" in e for e in r.evidence["errors"])


async def test_schema_validator_fail_wrong_type():
    """验收 #4：类型不符 → fail."""
    schema = {
        "type": "object",
        "required": ["objective"],
        "properties": {"objective": {"type": "object"}},
    }
    ctx = GateContext(
        artifact_type="item",
        pack_id="platform",
        artifact_payload={"objective": "not-an-object"},
        json_schema=schema,
    )
    r = await SchemaValidator().validate("sha256:item-v3", ctx)
    assert r.verdict == "fail"
    assert any("type=object" in e for e in r.evidence["errors"])


async def test_schema_validator_fail_extra_property():
    """additionalProperties=false：多余键 → fail."""
    schema = {
        "type": "object",
        "required": ["objective"],
        "properties": {"objective": {"type": "object"}},
        "additionalProperties": False,
    }
    ctx = GateContext(
        artifact_type="item",
        pack_id="platform",
        artifact_payload={"objective": {}, "bogus": 1},
        json_schema=schema,
    )
    r = await SchemaValidator().validate("sha256:item-v4", ctx)
    assert r.verdict == "fail"
    assert any("bogus" in e for e in r.evidence["errors"])


async def test_schema_validator_simple_required_keys_mode():
    """无 schema 时退化为 required_keys 简易模式."""
    ctx = GateContext(
        artifact_type="item",
        pack_id="platform",
        artifact_payload={"a": 1, "b": 2},
        required_keys=["a", "b"],
    )
    r = await SchemaValidator().validate("ref", ctx)
    assert r.verdict == "pass"
    assert r.evidence["mode"] == "required_keys"


async def test_schema_validator_review_when_no_schema_and_no_keys():
    """无 schema 且无 required_keys → review（无法校验）."""
    ctx = GateContext(
        artifact_type="item",
        pack_id="platform",
        artifact_payload={"a": 1},
    )
    r = await SchemaValidator().validate("ref", ctx)
    assert r.verdict == "review"


async def test_schema_validator_review_when_payload_none():
    """payload 为 None → review."""
    ctx = GateContext(
        artifact_type="item",
        pack_id="platform",
        artifact_payload=None,
        required_keys=["a"],
    )
    r = await SchemaValidator().validate("ref", ctx)
    assert r.verdict == "review"


async def test_schema_validator_nested_array_items():
    """array items 校验：嵌套 schema."""
    schema = {
        "type": "object",
        "required": ["kp_set"],
        "properties": {
            "kp_set": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["code"],
                    "properties": {"code": {"type": "string"}},
                },
            }
        },
    }
    # 缺 code → fail
    ctx_bad = GateContext(
        artifact_type="item", pack_id="platform",
        artifact_payload={"kp_set": [{"dimension": "kp"}]},  # 缺 code
        json_schema=schema,
    )
    r_bad = await SchemaValidator().validate("ref", ctx_bad)
    assert r_bad.verdict == "fail"

    # 齐全 → pass
    ctx_ok = GateContext(
        artifact_type="item", pack_id="platform",
        artifact_payload={"kp_set": [{"dimension": "kp", "code": "x"}]},
        json_schema=schema,
    )
    r_ok = await SchemaValidator().validate("ref", ctx_ok)
    assert r_ok.verdict == "pass"


# ────────────────────────────────────────────────────────────────────
# §2 LicenseValidator
# ────────────────────────────────────────────────────────────────────

async def _insert_license(
    async_session: AsyncSession,
    license_id: str,
    decision: str = "approved",
    expires_at: datetime | None = None,
) -> None:
    await async_session.execute(
        text(
            "INSERT INTO material_license (license_id, source, rights_holder, scope,"
            " expires_at, decision) VALUES (:lid, :src, :rh, :scope, :exp, :dec)"
        ),
        {
            "lid": license_id, "src": "test", "rh": "test-holder",
            "scope": "test", "exp": expires_at, "dec": decision,
        },
    )
    await async_session.commit()


async def test_license_validator_pass(async_session: AsyncSession):
    """验收 #2/4：license 存在 + approved + 未过期 → pass."""
    await _insert_license(async_session, "lic-ok", "approved", None)
    ctx = GateContext(
        artifact_type="material", pack_id="platform",
        artifact_payload={"license_id": "lic-ok"}, db=async_session,
    )
    r = await LicenseValidator().validate("sha256:mat-v1", ctx)
    assert r.verdict == "pass"
    assert r.evidence["license_id"] == "lic-ok"
    assert r.evidence["decision"] == "approved"


async def test_license_validator_pass_with_expiry_future(async_session: AsyncSession):
    """未过期（未来时间）→ pass."""
    future = datetime.now(timezone.utc) + timedelta(days=30)
    await _insert_license(async_session, "lic-future", "approved", future)
    ctx = GateContext(
        artifact_type="material", pack_id="platform",
        license_id="lic-future", db=async_session,
    )
    r = await LicenseValidator().validate("ref", ctx)
    assert r.verdict == "pass"


async def test_license_validator_fail_not_found(async_session: AsyncSession):
    """验收 #4：license_id 不存在 → fail."""
    ctx = GateContext(
        artifact_type="material", pack_id="platform",
        license_id="lic-missing", db=async_session,
    )
    r = await LicenseValidator().validate("ref", ctx)
    assert r.verdict == "fail"
    assert "未找到" in r.evidence["reason"]


async def test_license_validator_fail_rejected(async_session: AsyncSession):
    """验收 #4：decision=rejected → fail."""
    await _insert_license(async_session, "lic-rej", "rejected", None)
    ctx = GateContext(
        artifact_type="material", pack_id="platform",
        license_id="lic-rej", db=async_session,
    )
    r = await LicenseValidator().validate("ref", ctx)
    assert r.verdict == "fail"
    assert "rejected" in r.evidence["reason"]


async def test_license_validator_fail_expired(async_session: AsyncSession):
    """验收 #4：expires_at 已过期 → fail."""
    past = datetime.now(timezone.utc) - timedelta(days=1)
    await _insert_license(async_session, "lic-exp", "approved", past)
    ctx = GateContext(
        artifact_type="material", pack_id="platform",
        license_id="lic-exp", db=async_session,
    )
    r = await LicenseValidator().validate("ref", ctx)
    assert r.verdict == "fail"
    assert "过期" in r.evidence["reason"]


async def test_license_validator_fail_no_license_id(async_session: AsyncSession):
    """未提供 license_id → fail."""
    ctx = GateContext(
        artifact_type="material", pack_id="platform",
        artifact_payload={}, db=async_session,
    )
    r = await LicenseValidator().validate("ref", ctx)
    assert r.verdict == "fail"
    assert "license_id" in r.evidence["reason"]


async def test_license_validator_review_no_db():
    """未提供 db → review（无法查证）."""
    ctx = GateContext(
        artifact_type="material", pack_id="platform",
        license_id="lic-x",
    )
    r = await LicenseValidator().validate("ref", ctx)
    assert r.verdict == "review"


# ────────────────────────────────────────────────────────────────────
# §3 DuplicatePlaceholderValidator
# ────────────────────────────────────────────────────────────────────

async def _insert_published_item(
    async_session: AsyncSession, item_version_id: str
) -> None:
    """插入一条 published item_version（published_at=NULL 避免 gate_certificate CHECK）.

    为什么 published_at=NULL：CHECK ck_iv_published_requires_gate_cert 要求
    published_at 非空必伴随 gate_certificate_id；此处仅用于查重测试，
    published_at=NULL 让 CHECK 通过，status='published' 满足查重查询条件。
    """
    await async_session.execute(
        text(
            "INSERT INTO item (item_id, pack_id, tier) VALUES (:iid, 'platform', 'C')"
        ),
        {"iid": f"item-for-{item_version_id[:8]}"},
    )
    await async_session.execute(
        text(
            "INSERT INTO item_version (item_version_id, item_id, status, objective,"
            " interaction_ref, content, scoring_ref, error_bindings, lineage,"
            " rendered_snapshot) VALUES (:vid, :iid, 'published', '{}'::jsonb,"
            " '{}'::jsonb, '{}'::jsonb, '{}'::jsonb, '{}'::jsonb,"
            " '{}'::jsonb, '{}'::jsonb)"
        ),
        {"vid": item_version_id, "iid": f"item-for-{item_version_id[:8]}"},
    )
    await async_session.commit()


async def test_duplicate_validator_pass_no_duplicate(async_session: AsyncSession):
    """验收 #3/4：无重复 → pass."""
    payload = {"objective": {"kp": "unique-1"}, "content": {"q": "新题"}}
    ctx = GateContext(
        artifact_type="item", pack_id="platform",
        artifact_payload=payload, db=async_session,
    )
    r = await DuplicatePlaceholderValidator().validate("sha256:new", ctx)
    assert r.verdict == "pass"
    assert r.evidence["canonical_hash"].startswith("sha256:")
    assert r.evidence["checked_published"] is True


async def test_duplicate_validator_review_on_duplicate(async_session: AsyncSession):
    """验收 #3/4：发现重复（已 published 版本含同哈希）→ review."""
    payload = {"objective": {"kp": "dup"}, "content": {"q": "重复题"}}
    digest = _canonical_hash(payload)
    # 插入一条 published item_version，其 id == 规范化哈希
    await _insert_published_item(async_session, digest)

    ctx = GateContext(
        artifact_type="item", pack_id="platform",
        artifact_payload=payload, db=async_session,
    )
    r = await DuplicatePlaceholderValidator().validate("sha256:dup", ctx)
    assert r.verdict == "review"
    assert r.evidence["canonical_hash"] == digest
    assert "重复" in r.evidence["reason"]


async def test_duplicate_validator_review_when_no_db():
    """未提供 db → review."""
    ctx = GateContext(
        artifact_type="item", pack_id="platform",
        artifact_payload={"a": 1},
    )
    r = await DuplicatePlaceholderValidator().validate("ref", ctx)
    assert r.verdict == "review"


async def test_duplicate_validator_review_when_payload_none():
    """payload 为 None → review."""
    ctx = GateContext(
        artifact_type="item", pack_id="platform",
        artifact_payload=None, db=None,
    )
    r = await DuplicatePlaceholderValidator().validate("ref", ctx)
    assert r.verdict == "review"


async def test_duplicate_validator_review_for_unsupported_type(async_session: AsyncSession):
    """artifact_type 无查重表（group/blueprint/audio）→ review."""
    ctx = GateContext(
        artifact_type="group", pack_id="platform",
        artifact_payload={"a": 1}, db=async_session,
    )
    r = await DuplicatePlaceholderValidator().validate("ref", ctx)
    assert r.verdict == "review"
    assert "group" in r.evidence["artifact_type"]


async def test_duplicate_validator_non_blocking():
    """DuplicatePlaceholderValidator 是非阻断项（blocking=False）."""
    assert DuplicatePlaceholderValidator.blocking is False


# ────────────────────────────────────────────────────────────────────
# §4 覆盖注册表（取代 T-W2-008 桩）
# ────────────────────────────────────────────────────────────────────

async def test_generic_validators_registered_for_platform():
    """三个通用验证器已注册到 platform pack（覆盖桩）."""
    registered = set(list_validators("platform"))
    assert {"schema", "license", "duplicate_placeholder"} <= registered
    # get_validator 返回真实实现（非桩）
    v = get_validator("platform", "schema")
    assert isinstance(v, SchemaValidator)
    assert v.version == "1.0.0+generic"
    assert isinstance(get_validator("platform", "license"), LicenseValidator)
    assert isinstance(
        get_validator("platform", "duplicate_placeholder"), DuplicatePlaceholderValidator
    )


# ────────────────────────────────────────────────────────────────────
# §5 核心域不 import 学科包/学段包
# ────────────────────────────────────────────────────────────────────

def test_no_subject_pack_imports_in_validators():
    """宪法 A5/X6：src/core/gate/validators/ 不 import 任何学科包/学段包."""
    validators_dir = os.path.join("src", "core", "gate", "validators")
    pattern = re.compile(
        r"^\s*(?:from\s+(?:packs|subject_|gradeband)|import\s+(?:packs|subject_|gradeband))",
        re.MULTILINE,
    )
    violations: list[tuple[str, list[str]]] = []
    for fname in os.listdir(validators_dir):
        if not fname.endswith(".py"):
            continue
        fpath = os.path.join(validators_dir, fname)
        with open(fpath, encoding="utf-8") as f:
            content = f.read()
        matches = pattern.findall(content)
        if matches:
            violations.append((fname, matches))
    assert not violations, f"src/core/gate/validators/ 存在学科包 import：{violations}"
