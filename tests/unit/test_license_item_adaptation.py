"""W3 遗留 S9-②：LicenseValidator item 业务适配测试.

适配规则（LicenseValidator v1.1.0+generic）：
  - artifact_type='item' 且无 license_id → pass（跳过；许可在 material/corpus 侧强制）
  - artifact_type='item' 且显式携带 license_id → 照常校验（不存在/拒绝/过期仍 fail）
  - material/corpus 等其余产物类型：缺失 license_id 仍 fail（既有行为不变）
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.gate.validator import GateContext, register_validator
from src.core.gate.validators.generic import (
    DuplicatePlaceholderValidator,
    LicenseValidator,
    SchemaValidator,
)


@pytest.fixture(autouse=True)
def _re_register_generic_validators():
    """每测试前重注册通用验证器（防止其他测试 reset_registry 后互染）."""
    register_validator("platform", SchemaValidator)
    register_validator("platform", LicenseValidator)
    register_validator("platform", DuplicatePlaceholderValidator)
    yield


async def _insert_license(
    db: AsyncSession,
    license_id: str,
    decision: str = "approved",
    expires_at: datetime | None = None,
) -> None:
    await db.execute(
        text(
            "INSERT INTO material_license (license_id, source, rights_holder, scope,"
            " expires_at, decision) VALUES (:lid, :src, :rh, :scope, :exp, :dec)"
        ),
        {
            "lid": license_id, "src": "test", "rh": "test-holder",
            "scope": "test", "exp": expires_at, "dec": decision,
        },
    )
    await db.commit()


class TestItemWithoutLicenseId:
    """item 无 license_id 不被阻断（W3 适配核心）."""

    @pytest.mark.asyncio
    async def test_item_no_license_id_passes(self, async_session: AsyncSession):
        """item + 无 license_id（payload 空）→ pass（skipped）."""
        ctx = GateContext(
            artifact_type="item", pack_id="platform",
            artifact_payload={}, db=async_session,
        )
        r = await LicenseValidator().validate("sha256:iv-1", ctx)
        assert r.verdict == "pass"
        assert r.evidence["skipped"] is True

    @pytest.mark.asyncio
    async def test_item_no_license_id_passes_without_db(self):
        """item + 无 license_id 且无 db → 仍 pass（不需要查库）."""
        ctx = GateContext(
            artifact_type="item", pack_id="platform",
            artifact_payload={"objective": {}},
        )
        r = await LicenseValidator().validate("sha256:iv-2", ctx)
        assert r.verdict == "pass"
        assert r.evidence["skipped"] is True

    @pytest.mark.asyncio
    async def test_item_payload_without_license_key_passes(
        self, async_session: AsyncSession
    ):
        """item payload 无 license_id 键（A 线实例化产物形态）→ pass."""
        payload = {
            "objective": {}, "interaction_ref": {}, "content": {},
            "scoring_ref": {}, "lineage": {},
        }
        ctx = GateContext(
            artifact_type="item", pack_id="subject-math",
            artifact_payload=payload, db=async_session,
        )
        r = await LicenseValidator().validate("sha256:iv-3", ctx)
        assert r.verdict == "pass"


class TestItemWithLicenseIdStillChecked:
    """item 显式携带 license_id 时照常校验."""

    @pytest.mark.asyncio
    async def test_item_with_approved_license_passes(
        self, async_session: AsyncSession
    ):
        await _insert_license(async_session, "lic-item-ok", "approved", None)
        ctx = GateContext(
            artifact_type="item", pack_id="platform",
            artifact_payload={"license_id": "lic-item-ok"}, db=async_session,
        )
        r = await LicenseValidator().validate("sha256:iv-4", ctx)
        assert r.verdict == "pass"
        assert r.evidence["license_id"] == "lic-item-ok"

    @pytest.mark.asyncio
    async def test_item_with_unknown_license_fails(
        self, async_session: AsyncSession
    ):
        ctx = GateContext(
            artifact_type="item", pack_id="platform",
            license_id="lic-item-missing", db=async_session,
        )
        r = await LicenseValidator().validate("sha256:iv-5", ctx)
        assert r.verdict == "fail"
        assert "未找到" in r.evidence["reason"]

    @pytest.mark.asyncio
    async def test_item_with_expired_license_fails(
        self, async_session: AsyncSession
    ):
        past = datetime.now(timezone.utc) - timedelta(days=1)
        await _insert_license(async_session, "lic-item-exp", "approved", past)
        ctx = GateContext(
            artifact_type="item", pack_id="platform",
            license_id="lic-item-exp", db=async_session,
        )
        r = await LicenseValidator().validate("sha256:iv-6", ctx)
        assert r.verdict == "fail"
        assert "过期" in r.evidence["reason"]


class TestOtherArtifactTypesUnchanged:
    """material/corpus 等产物类型：缺失 license_id 仍 fail（既有语义不变）."""

    @pytest.mark.asyncio
    async def test_corpus_no_license_id_fails(self, async_session: AsyncSession):
        ctx = GateContext(
            artifact_type="corpus", pack_id="platform",
            artifact_payload={}, db=async_session,
        )
        r = await LicenseValidator().validate("sha256:cv-1", ctx)
        assert r.verdict == "fail"
        assert "license_id" in r.evidence["reason"]

    @pytest.mark.asyncio
    async def test_audio_no_license_id_fails(self, async_session: AsyncSession):
        ctx = GateContext(
            artifact_type="audio", pack_id="platform",
            artifact_payload={}, db=async_session,
        )
        r = await LicenseValidator().validate("sha256:au-1", ctx)
        assert r.verdict == "fail"
