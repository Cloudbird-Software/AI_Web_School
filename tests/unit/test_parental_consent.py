"""T-W4-032 家长授权记录单元测试.

覆盖验收标准：
1. record_consent(student_alias_id, scope, valid_until) 写入新版本授权记录，
   旧版本标记过期时间戳（由新事件 created_at 隐式承载）。
2. check_consent(student_alias_id, scope) 返回当前有效授权状态；无有效授权或
   已撤回返回 False（state != 'granted'）。
3. 撤回操作：写入撤回记录，原授权立即失效，历史记录保留。
4. make accept TASK=T-W4-032 全绿；迁移脚本可升级/降级（migrate-check 验证）。
5. 不 import 任何学科包/学段包（CI 静态扫描强制）。

测试隔离：复用 conftest.async_session 的事务回滚；parental_consent 写入在
savepoint 内，测试结束自动回滚。
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from src.core.compliance.parental_consent import (
    ConsentScopeError,
    ConsentStatus,
    NoActiveConsentError,
    check_consent,
    list_consent_history,
    record_consent,
    revoke_consent,
)


# ────────────────────────────────────────────────────────────────────
# fixture
# ────────────────────────────────────────────────────────────────────

@pytest_asyncio.fixture(autouse=True)
async def _truncate_consent(async_engine: AsyncEngine):
    """每测试前清空 parental_consent（独立连接真实提交，避免锁跨测试）."""
    async with async_engine.connect() as conn:
        await conn.execute(text("TRUNCATE parental_consent CASCADE"))
        await conn.commit()
    yield


_T0 = datetime(2026, 7, 28, 10, 0, 0, tzinfo=timezone.utc)
_DAY = timedelta(days=1)


# ────────────────────────────────────────────────────────────────────
# 1. record_consent：版本化写入
# ────────────────────────────────────────────────────────────────────

class TestRecordConsent:
    """record_consent 写入新版本授权记录."""

    @pytest.mark.asyncio
    async def test_first_grant_version_1(self, async_session: AsyncSession):
        """首次授权版本号为 1."""
        sid = uuid.uuid4()
        cid = await record_consent(
            async_session,
            student_alias_id=sid,
            scope="practice",
            valid_until=_T0 + _DAY,
            now=_T0,
        )
        await async_session.commit()
        assert cid is not None

        status = await check_consent(async_session, sid, "practice", now=_T0)
        assert status.state == "granted"
        assert status.version == 1
        assert status.is_valid is True

    @pytest.mark.asyncio
    async def test_second_grant_version_2(self, async_session: AsyncSession):
        """再次授权版本号递增为 2，旧版本隐式失效."""
        sid = uuid.uuid4()
        await record_consent(
            async_session, student_alias_id=sid, scope="practice",
            valid_until=_T0 + _DAY, now=_T0,
        )
        await record_consent(
            async_session, student_alias_id=sid, scope="practice",
            valid_until=_T0 + 2 * _DAY, now=_T0 + _DAY,
        )
        await async_session.commit()

        status = await check_consent(async_session, sid, "practice", now=_T0 + _DAY)
        assert status.version == 2, "最新版本须为 2"
        assert status.valid_until == _T0 + 2 * _DAY

    @pytest.mark.asyncio
    async def test_scope_dict_with_extensions(self, async_session: AsyncSession):
        """scope dict 可含 subject/time_period 等扩展维度."""
        sid = uuid.uuid4()
        await record_consent(
            async_session,
            student_alias_id=sid,
            scope={"purpose": "diagnosis", "subject": "math", "time_period": "2026-Q3"},
            valid_until=_T0 + _DAY,
            now=_T0,
        )
        await async_session.commit()

        # 按 purpose 查询（扩展维度不影响 purpose 主键语义）
        status = await check_consent(async_session, sid, "diagnosis", now=_T0)
        assert status.state == "granted"
        assert status.purpose == "diagnosis"

    @pytest.mark.asyncio
    async def test_invalid_scope_raises(self, async_session: AsyncSession):
        """scope 缺 purpose 抛 ConsentScopeError."""
        sid = uuid.uuid4()
        with pytest.raises(ConsentScopeError):
            await record_consent(
                async_session, student_alias_id=sid,
                scope={"subject": "math"},  # 缺 purpose
                valid_until=_T0 + _DAY,
            )

    @pytest.mark.asyncio
    async def test_empty_purpose_raises(self, async_session: AsyncSession):
        """空 purpose 抛 ConsentScopeError."""
        sid = uuid.uuid4()
        with pytest.raises(ConsentScopeError):
            await record_consent(
                async_session, student_alias_id=sid, scope="",
                valid_until=_T0 + _DAY,
            )

    @pytest.mark.asyncio
    async def test_invalid_time_window_raises(self, async_session: AsyncSession):
        """valid_until 早于 valid_from 抛 ValueError."""
        sid = uuid.uuid4()
        with pytest.raises(ValueError):
            await record_consent(
                async_session, student_alias_id=sid, scope="practice",
                valid_until=_T0,
                valid_from=_T0 + _DAY,  # 反向
            )


# ────────────────────────────────────────────────────────────────────
# 2. check_consent：状态判定
# ────────────────────────────────────────────────────────────────────

class TestCheckConsent:
    """check_consent 返回当前有效授权状态."""

    @pytest.mark.asyncio
    async def test_missing_no_record(self, async_session: AsyncSession):
        """无授权记录返回 missing."""
        sid = uuid.uuid4()
        status = await check_consent(async_session, sid, "practice", now=_T0)
        assert status.state == "missing"
        assert status.is_valid is False
        assert status.version is None

    @pytest.mark.asyncio
    async def test_granted_within_window(self, async_session: AsyncSession):
        """授权窗口内返回 granted."""
        sid = uuid.uuid4()
        await record_consent(
            async_session, student_alias_id=sid, scope="practice",
            valid_until=_T0 + _DAY, now=_T0,
        )
        await async_session.commit()

        status = await check_consent(async_session, sid, "practice", now=_T0 + timedelta(hours=12))
        assert status.state == "granted"
        assert status.is_valid is True

    @pytest.mark.asyncio
    async def test_expired_after_valid_until(self, async_session: AsyncSession):
        """超过 valid_until 返回 expired."""
        sid = uuid.uuid4()
        await record_consent(
            async_session, student_alias_id=sid, scope="practice",
            valid_until=_T0 + _DAY, now=_T0,
        )
        await async_session.commit()

        status = await check_consent(async_session, sid, "practice", now=_T0 + 2 * _DAY)
        assert status.state == "expired"
        assert status.is_valid is False

    @pytest.mark.asyncio
    async def test_revoked_returns_revoked(self, async_session: AsyncSession):
        """已撤回返回 revoked."""
        sid = uuid.uuid4()
        await record_consent(
            async_session, student_alias_id=sid, scope="practice",
            valid_until=_T0 + _DAY, now=_T0,
        )
        await revoke_consent(async_session, student_alias_id=sid, scope="practice", now=_T0 + timedelta(hours=1))
        await async_session.commit()

        status = await check_consent(async_session, sid, "practice", now=_T0 + timedelta(hours=2))
        assert status.state == "revoked"
        assert status.is_valid is False

    @pytest.mark.asyncio
    async def test_different_purposes_independent(self, async_session: AsyncSession):
        """不同 purpose 的授权互相独立（practice 授权不蕴含 diagnosis）."""
        sid = uuid.uuid4()
        await record_consent(
            async_session, student_alias_id=sid, scope="practice",
            valid_until=_T0 + _DAY, now=_T0,
        )
        await async_session.commit()

        practice = await check_consent(async_session, sid, "practice", now=_T0)
        diagnosis = await check_consent(async_session, sid, "diagnosis", now=_T0)
        assert practice.state == "granted"
        assert diagnosis.state == "missing"


# ────────────────────────────────────────────────────────────────────
# 3. revoke_consent：撤回操作
# ────────────────────────────────────────────────────────────────────

class TestRevokeConsent:
    """撤回操作：写入撤回记录，原授权立即失效，历史保留."""

    @pytest.mark.asyncio
    async def test_revoke_invalidates_immediately(self, async_session: AsyncSession):
        """撤回后原授权立即失效（check_consent 返回 False）."""
        sid = uuid.uuid4()
        await record_consent(
            async_session, student_alias_id=sid, scope="practice",
            valid_until=_T0 + _DAY, now=_T0,
        )
        await async_session.commit()

        # 撤回前有效
        assert (await check_consent(async_session, sid, "practice", now=_T0)).is_valid

        await revoke_consent(async_session, student_alias_id=sid, scope="practice", now=_T0 + timedelta(hours=1))
        await async_session.commit()

        # 撤回后立即失效（即使 valid_until 未到）
        status = await check_consent(async_session, sid, "practice", now=_T0 + timedelta(hours=2))
        assert status.state == "revoked"
        assert not status.is_valid

    @pytest.mark.asyncio
    async def test_revoke_writes_new_record(self, async_session: AsyncSession):
        """撤回写新行（event_type='revoke'），历史保留."""
        sid = uuid.uuid4()
        await record_consent(
            async_session, student_alias_id=sid, scope="practice",
            valid_until=_T0 + _DAY, now=_T0,
        )
        await revoke_consent(async_session, student_alias_id=sid, scope="practice", now=_T0 + timedelta(hours=1))
        await async_session.commit()

        history = await list_consent_history(async_session, sid, "practice")
        assert len(history) == 2, "须有 grant + revoke 两条记录"
        assert history[0]["event_type"] == "grant"
        assert history[1]["event_type"] == "revoke"
        assert history[1]["version"] == 2

    @pytest.mark.asyncio
    async def test_revoke_missing_raises(self, async_session: AsyncSession):
        """无授权可撤回抛 NoActiveConsentError."""
        sid = uuid.uuid4()
        with pytest.raises(NoActiveConsentError):
            await revoke_consent(async_session, student_alias_id=sid, scope="practice")

    @pytest.mark.asyncio
    async def test_revoke_expired_raises(self, async_session: AsyncSession):
        """已过期授权不能撤回."""
        sid = uuid.uuid4()
        await record_consent(
            async_session, student_alias_id=sid, scope="practice",
            valid_until=_T0 + _DAY, now=_T0,
        )
        await async_session.commit()

        with pytest.raises(NoActiveConsentError):
            await revoke_consent(
                async_session, student_alias_id=sid, scope="practice",
                now=_T0 + 2 * _DAY,  # 已过期
            )

    @pytest.mark.asyncio
    async def test_revoke_after_revoke_raises(self, async_session: AsyncSession):
        """重复撤回抛 NoActiveConsentError."""
        sid = uuid.uuid4()
        await record_consent(
            async_session, student_alias_id=sid, scope="practice",
            valid_until=_T0 + _DAY, now=_T0,
        )
        await revoke_consent(async_session, student_alias_id=sid, scope="practice", now=_T0 + timedelta(hours=1))
        await async_session.commit()

        with pytest.raises(NoActiveConsentError):
            await revoke_consent(async_session, student_alias_id=sid, scope="practice")

    @pytest.mark.asyncio
    async def test_grant_after_revoke_resurrects(self, async_session: AsyncSession):
        """撤回后再次授权，新 grant 生效（version 递增）."""
        sid = uuid.uuid4()
        await record_consent(
            async_session, student_alias_id=sid, scope="practice",
            valid_until=_T0 + _DAY, now=_T0,
        )
        await revoke_consent(async_session, student_alias_id=sid, scope="practice", now=_T0 + timedelta(hours=1))
        await record_consent(
            async_session, student_alias_id=sid, scope="practice",
            valid_until=_T0 + 3 * _DAY, now=_T0 + 2 * _DAY,
        )
        await async_session.commit()

        status = await check_consent(async_session, sid, "practice", now=_T0 + 2 * _DAY + timedelta(hours=1))
        assert status.state == "granted"
        assert status.version == 3, "grant(1) + revoke(2) + grant(3)"

        history = await list_consent_history(async_session, sid, "practice")
        assert len(history) == 3


# ────────────────────────────────────────────────────────────────────
# 4. append-only 物理强制
# ────────────────────────────────────────────────────────────────────

class TestAppendOnly:
    """parental_consent 表 append-only 物理强制（DB 触发器）."""

    @pytest.mark.asyncio
    async def test_update_rejected(self, async_session: AsyncSession):
        """UPDATE 触发器拒绝修改."""
        sid = uuid.uuid4()
        await record_consent(
            async_session, student_alias_id=sid, scope="practice",
            valid_until=_T0 + _DAY, now=_T0,
        )
        await async_session.commit()

        with pytest.raises(Exception) as exc_info:
            await async_session.execute(
                text("UPDATE parental_consent SET event_type = 'revoke' WHERE student_alias_id = :sid"),
                {"sid": sid},
            )
            await async_session.commit()
        assert "append-only" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_delete_rejected(self, async_session: AsyncSession):
        """DELETE 触发器拒绝删除."""
        sid = uuid.uuid4()
        await record_consent(
            async_session, student_alias_id=sid, scope="practice",
            valid_until=_T0 + _DAY, now=_T0,
        )
        await async_session.commit()

        with pytest.raises(Exception) as exc_info:
            await async_session.execute(
                text("DELETE FROM parental_consent WHERE student_alias_id = :sid"),
                {"sid": sid},
            )
            await async_session.commit()
        assert "append-only" in str(exc_info.value).lower()


# ────────────────────────────────────────────────────────────────────
# 5. 学科包隔离（X6）
# ────────────────────────────────────────────────────────────────────

class TestNoSubjectPackImport:
    """合规层不 import 任何学科包/学段包（宪法 A5/X6）."""

    def test_parental_consent_module_no_subject_pack(self):
        """parental_consent 模块源码不引用任何学科包."""
        import inspect
        from src.core.compliance import parental_consent
        source = inspect.getsource(parental_consent)
        # 禁止 import 学科包/学段包
        forbidden = ("src.packs", "subject_math", "subject_chinese", "subject_english",
                     "gradeband", "subject-math", "subject-chinese", "subject-english")
        for token in forbidden:
            assert token not in source, (
                f"parental_consent 不得引用学科包/学段包（X6），发现 {token!r}"
            )
