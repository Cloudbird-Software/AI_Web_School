"""T-W4-031 PII 保险库单元测试.

覆盖验收标准：
1. pii_vault schema 独立，含学生直标识表（id/姓名/电话/地址/家长联系/加密密文/创建时间）。
2. 写入/读取自动加解密：明文不落地磁盘（除内存中短暂存在）。
3. 白名单：未授权服务/角色查询 pii_vault 表在 DB 层失败（权限错误）。
4. make accept TASK=T-W4-031 全绿；迁移脚本可升级/降级（migrate-check 验证）。
5. 主库零直标识：扫描主库 schema 确认无姓名/电话/地址等 PII 字段。

测试隔离：复用 conftest.async_session 的事务回滚；pii_vault 写入在 savepoint 内，
测试结束自动回滚。SET ROLE 测试用独立连接 + 真实提交后回滚（角色操作不能在
savepoint 内）。
"""
from __future__ import annotations

import base64
import os
import uuid
from datetime import datetime, timezone

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from src.core.compliance.pii_encryption import (
    PIIKeyError,
    PII_VAULT_KEY_ENV,
    StudentIdentity,
    StudentIdentityNotFoundError,
    decrypt_field,
    encrypt_field,
    generate_master_key,
    load_master_key,
    read_identity,
    write_identity,
)


# ────────────────────────────────────────────────────────────────────
# 测试用主密钥（32 字节 base64；不读 .env，固定值保证测试可复现）
# ────────────────────────────────────────────────────────────────────
_TEST_KEY_B64 = "4+H57udVMoZpDOg0U4lmmJu7JqxEhbrgBPfXNZXieXU="
_TEST_KEY = base64.b64decode(_TEST_KEY_B64)
assert len(_TEST_KEY) == 32, "测试主密钥须 32 字节"


@pytest_asyncio.fixture(autouse=True)
async def _truncate_pii_vault(async_engine: AsyncEngine):
    """每测试前清空 pii_vault 表（access_log 与 student_identity）.

    为什么用独立连接 + 真实提交（而非 async_session 的 savepoint）：
    TRUNCATE 会获取 ACCESS EXCLUSIVE 锁并持有至事务结束。若在 savepoint 内
    执行（async_session 的外层事务），锁会持续整个测试，阻塞 access-control
    测试的跨连接 SELECT（SET ROLE 后从另一连接读 pii_vault）。独立连接 +
    真实 commit 后锁立即释放，每个测试从干净状态开始。

    写入隔离仍由 async_session 的 savepoint 回滚保证（TestWriteReadIdentity
    的写入在外层事务内，测试结束回滚）。
    """
    async with async_engine.connect() as conn:
        await conn.execute(
            text("TRUNCATE pii_vault.student_identity, pii_vault.access_log CASCADE")
        )
        await conn.commit()
    yield


# ────────────────────────────────────────────────────────────────────
# 1. 加解密原语
# ────────────────────────────────────────────────────────────────────

class TestEncryptField:
    """AES-256-GCM 加解密原语."""

    def test_roundtrip_chinese(self):
        """中文姓名/地址加解密往返."""
        plaintext = "张三丰"
        ct, nonce = encrypt_field(plaintext, _TEST_KEY)
        assert ct != plaintext.encode("utf-8"), "密文不应等于明文"
        assert len(nonce) == 12, "GCM nonce 须 96-bit"
        assert decrypt_field(ct, nonce, _TEST_KEY) == plaintext

    def test_roundtrip_phone_and_address(self):
        """电话/地址混合字段加解密."""
        for plaintext in ("13800138000", "北京市海淀区中关村大街1号", "+86 138-0013-8000"):
            ct, nonce = encrypt_field(plaintext, _TEST_KEY)
            assert decrypt_field(ct, nonce, _TEST_KEY) == plaintext

    def test_nonce_unique_per_call(self):
        """每次加密 nonce 不同（GCM 安全性要求）."""
        nonces = set()
        for _ in range(100):
            _, nonce = encrypt_field("same_plaintext", _TEST_KEY)
            nonces.add(nonce)
        assert len(nonces) == 100, "nonce 须每次随机生成、不复用"

    def test_ciphertext_different_for_same_plaintext(self):
        """同明文两次加密产生不同密文（因 nonce 不同）."""
        ct1, _ = encrypt_field("张三", _TEST_KEY)
        ct2, _ = encrypt_field("张三", _TEST_KEY)
        assert ct1 != ct2, "同明文不同 nonce 应产生不同密文"

    def test_decrypt_with_wrong_key_raises(self):
        """错误密钥解密抛 InvalidTag（认证失败）."""
        from cryptography.exceptions import InvalidTag

        ct, nonce = encrypt_field("secret", _TEST_KEY)
        wrong_key = base64.b64decode(generate_master_key())
        with pytest.raises(InvalidTag):
            decrypt_field(ct, nonce, wrong_key)

    def test_decrypt_tampered_ciphertext_raises(self):
        """密文被篡改后解密抛 InvalidTag（完整性保护）."""
        from cryptography.exceptions import InvalidTag

        ct, nonce = encrypt_field("secret", _TEST_KEY)
        tampered = bytearray(ct)
        tampered[0] ^= 0xFF  # 翻转首字节
        with pytest.raises(InvalidTag):
            decrypt_field(bytes(tampered), nonce, _TEST_KEY)

    def test_encrypt_rejects_non_string(self):
        """非字符串明文拒绝（类型安全）."""
        with pytest.raises(TypeError):
            encrypt_field(123, _TEST_KEY)  # type: ignore[arg-type]


# ────────────────────────────────────────────────────────────────────
# 2. 密钥加载
# ────────────────────────────────────────────────────────────────────

class TestLoadMasterKey:
    """环境变量主密钥加载."""

    def test_loads_valid_key(self):
        """合法 base64 32 字节密钥正常加载."""
        key = load_master_key(env={PII_VAULT_KEY_ENV: _TEST_KEY_B64})
        assert key == _TEST_KEY
        assert len(key) == 32

    def test_missing_env_raises(self):
        """环境变量缺失抛 PIIKeyError."""
        with pytest.raises(PIIKeyError, match="未设置"):
            load_master_key(env={})

    def test_invalid_base64_raises(self):
        """非 base64 抛 PIIKeyError."""
        with pytest.raises(PIIKeyError, match="非 base64"):
            load_master_key(env={PII_VAULT_KEY_ENV: "!!!not-base64!!!"})

    def test_wrong_length_raises(self):
        """长度非 32 字节抛 PIIKeyError."""
        short = base64.b64encode(b"only16bytes123456").decode("ascii")
        with pytest.raises(PIIKeyError, match="长度"):
            load_master_key(env={PII_VAULT_KEY_ENV: short})

    def test_generate_master_key_produces_valid(self):
        """generate_master_key 输出可被 load_master_key 接受."""
        new_key_b64 = generate_master_key()
        key = load_master_key(env={PII_VAULT_KEY_ENV: new_key_b64})
        assert len(key) == 32


# ────────────────────────────────────────────────────────────────────
# 3. DB 读写：自动加解密 + 明文不落地
# ────────────────────────────────────────────────────────────────────

class TestWriteReadIdentity:
    """write_identity / read_identity 端到端."""

    @pytest.mark.asyncio
    async def test_write_then_read_roundtrip(self, async_session: AsyncSession):
        """写入后读取，明文一致；密文与明文不同."""
        sid = uuid.uuid4()
        await write_identity(
            async_session,
            student_alias_id=sid,
            name="李四",
            phone="13900001111",
            address="上海市浦东新区世纪大道100号",
            parent_contact="李父 13800002222",
            key=_TEST_KEY,
        )
        await async_session.commit()

        identity = await read_identity(
            async_session,
            student_alias_id=sid,
            accessor="test_service",
            purpose="unit_test_roundtrip",
            key=_TEST_KEY,
        )
        assert identity.name == "李四"
        assert identity.phone == "13900001111"
        assert identity.address == "上海市浦东新区世纪大道100号"
        assert identity.parent_contact == "李父 13800002222"
        assert identity.student_alias_id == sid
        assert identity.created_at is not None

    @pytest.mark.asyncio
    async def test_plaintext_not_on_disk(self, async_session: AsyncSession):
        """明文不落地：DB 中只存密文 + nonce，明文字符串不在表里."""
        sid = uuid.uuid4()
        plaintext_name = "王五_unique_marker_xyz"
        plaintext_phone = "15500009999"
        await write_identity(
            async_session,
            student_alias_id=sid,
            name=plaintext_name,
            phone=plaintext_phone,
            address="广州市天河区",
            parent_contact="王母",
            key=_TEST_KEY,
        )
        await async_session.commit()

        # 直接 SQL 查询所有列，明文字符串不应出现在任何列中
        rows = (
            await async_session.execute(
                text(
                    "SELECT name_ciphertext::text, phone_ciphertext::text, "
                    "address_ciphertext::text, parent_contact_ciphertext::text "
                    "FROM pii_vault.student_identity WHERE student_alias_id = :sid"
                ),
                {"sid": sid},
            )
        ).first()
        assert rows is not None
        for col_value in rows:
            col_str = str(col_value)
            assert plaintext_name not in col_str, "明文姓名不得出现在密文列"
            assert plaintext_phone not in col_str, "明文电话不得出现在密文列"

    @pytest.mark.asyncio
    async def test_read_nonexistent_raises(self, async_session: AsyncSession):
        """读取不存在的 student_alias_id 抛 StudentIdentityNotFoundError."""
        sid = uuid.uuid4()
        with pytest.raises(StudentIdentityNotFoundError):
            await read_identity(
                async_session,
                student_alias_id=sid,
                key=_TEST_KEY,
            )

    @pytest.mark.asyncio
    async def test_write_duplicate_pk_raises(self, async_session: AsyncSession):
        """同一 student_alias_id 重复写入触发 PK 冲突（不可改写）."""
        sid = uuid.uuid4()
        await write_identity(
            async_session,
            student_alias_id=sid,
            name="赵六",
            phone="13700000000",
            address="成都市",
            parent_contact="赵父",
            key=_TEST_KEY,
        )
        await async_session.commit()
        with pytest.raises(Exception):  # IntegrityError
            await write_identity(
                async_session,
                student_alias_id=sid,
                name="赵六改",
                phone="13700000001",
                address="成都市改",
                parent_contact="赵父改",
                key=_TEST_KEY,
            )
            await async_session.commit()

    @pytest.mark.asyncio
    async def test_read_writes_audit_log(self, async_session: AsyncSession):
        """每次读取写一条 access_log 审计记录."""
        sid = uuid.uuid4()
        await write_identity(
            async_session,
            student_alias_id=sid,
            name="孙七",
            phone="13600000000",
            address="杭州市",
            parent_contact="孙母",
            key=_TEST_KEY,
        )
        await async_session.commit()

        await read_identity(
            async_session,
            student_alias_id=sid,
            accessor="support_service",
            purpose="parent_phone_lookup",
            key=_TEST_KEY,
            now=datetime(2026, 7, 28, 10, 0, 0, tzinfo=timezone.utc),
        )
        await async_session.commit()

        log_count = (
            await async_session.execute(
                text(
                    "SELECT count(*) FROM pii_vault.access_log "
                    "WHERE student_alias_id = :sid AND accessor = 'support_service' "
                    "AND purpose = 'parent_phone_lookup'"
                ),
                {"sid": sid},
            )
        ).scalar()
        assert log_count == 1, "每次 read_identity 须写一条审计日志"


# ────────────────────────────────────────────────────────────────────
# 4. 白名单访问控制：未授权角色查询在 DB 层失败
# ────────────────────────────────────────────────────────────────────

class TestWhitelistAccessControl:
    """白名单：未授权角色查询 pii_vault 表在 DB 层失败.

    为什么用独立连接 + 真实提交：SET ROLE / CREATE ROLE 不能在 savepoint
    内可靠执行（savepoint 内的角色状态会随 rollback 丢失）；用独立连接
    确保角色操作真实生效，测试后清理。

    关键：SET ROLE 后必须在**同一连接**显式 RESET ROLE，否则连接归还连接池
    时仍持 SET ROLE 状态，DROP ROLE 会因「role is active」无限等待。
    """

    @pytest.mark.asyncio
    async def test_unauthorized_role_select_fails(
        self, async_engine: AsyncEngine
    ):
        """无 pii_vault 权限的角色 SELECT student_identity 报 permission denied."""
        # 1. 准备：创建无权限角色并授予当前用户 SET ROLE 权限
        async with async_engine.connect() as setup_conn:
            await setup_conn.execute(text("DROP ROLE IF EXISTS test_no_pii_role"))
            await setup_conn.execute(text("CREATE ROLE test_no_pii_role NOLOGIN"))
            # 授予 public schema 基础权限，但**不**授予 pii_vault 任何权限
            await setup_conn.execute(text("GRANT USAGE ON SCHEMA public TO test_no_pii_role"))
            await setup_conn.execute(
                text("GRANT test_no_pii_role TO CURRENT_USER")
            )
            await setup_conn.commit()

        # 2. 验证：SET ROLE 后 SELECT pii_vault 应 permission denied
        try:
            async with async_engine.connect() as test_conn:
                await test_conn.execute(text("SET ROLE test_no_pii_role"))
                try:
                    with pytest.raises(Exception) as exc_info:
                        await test_conn.execute(
                            text("SELECT count(*) FROM pii_vault.student_identity")
                        )
                    err_msg = str(exc_info.value).lower()
                    assert "permission" in err_msg or "denied" in err_msg, (
                        f"期望 permission denied，实际：{exc_info.value}"
                    )
                finally:
                    # 关键 1：错误后事务已 abort，须先 rollback 才能发后续命令
                    await test_conn.rollback()
                    # 关键 2：SET ROLE 是 session 级，rollback 不重置——须显式 RESET ROLE，
                    # 否则连接归还连接池后 DROP ROLE 会因 role 仍 active 卡死
                    await test_conn.execute(text("RESET ROLE"))
                    await test_conn.commit()
        finally:
            # 3. 清理：先撤销权限再 DROP ROLE（PG 要求无依赖对象才能 DROP ROLE）
            async with async_engine.connect() as cleanup_conn:
                # REVOKE 须容错：角色不存在时跳过（避免 UndefinedObjectError）
                exists = (
                    await cleanup_conn.execute(
                        text("SELECT 1 FROM pg_roles WHERE rolname='test_no_pii_role'")
                    )
                ).scalar()
                if exists:
                    await cleanup_conn.execute(
                        text("REVOKE ALL ON SCHEMA public FROM test_no_pii_role")
                    )
                await cleanup_conn.execute(text("DROP ROLE IF EXISTS test_no_pii_role"))
                await cleanup_conn.commit()

    @pytest.mark.asyncio
    async def test_authorized_reader_can_select(
        self, async_engine: AsyncEngine
    ):
        """pii_vault_reader 角色可 SELECT student_identity（白名单内）."""
        async with async_engine.connect() as setup_conn:
            await setup_conn.execute(
                text("GRANT pii_vault_reader TO CURRENT_USER")
            )
            await setup_conn.commit()

        try:
            async with async_engine.connect() as test_conn:
                await test_conn.execute(text("SET ROLE pii_vault_reader"))
                try:
                    # pii_vault_reader 有 SELECT 权限，应成功
                    result = await test_conn.execute(
                        text("SELECT count(*) FROM pii_vault.student_identity")
                    )
                    count = result.scalar()
                    assert count is not None and count >= 0
                finally:
                    # 关键：归还连接前 RESET ROLE
                    await test_conn.execute(text("RESET ROLE"))
                    await test_conn.commit()
        finally:
            # 不 DROP pii_vault_reader（迁移管理的持久角色）；仅撤销当前用户对它的 membership
            async with async_engine.connect() as cleanup_conn:
                # REVOKE membership 是幂等的（无 membership 时 noop）
                await cleanup_conn.execute(
                    text("REVOKE pii_vault_reader FROM CURRENT_USER")
                )
                await cleanup_conn.commit()


# ────────────────────────────────────────────────────────────────────
# 5. 主库零直标识：扫描 public schema 无 PII 字段
# ────────────────────────────────────────────────────────────────────

class TestMainDBNoPII:
    """主库 public schema 不含学生直标识字段（D7）."""

    # PII 直标识字段名模式（student_前缀 / parent_ / phone / address / 家长联系等）
    # 通用 name 字段允许（如 paper.name / material_version.name 是内容资产名，非学生 PII）
    _PII_COLUMN_PATTERNS = (
        "student_name", "student_phone", "student_address",
        "parent_name", "parent_phone", "parent_contact",
        "phone_number", "home_address", "student_real_name",
        "real_name", "mobile", "telephone", "email",
        "id_card", "id_number", "national_id",
    )

    @pytest.mark.asyncio
    async def test_public_schema_has_no_pii_columns(self, async_session: AsyncSession):
        """扫描 information_schema.columns：public schema 无 PII 直标识字段."""
        # 构建 NOT LIKE 模式串
        like_patterns = ", ".join(
            f"'%{p}%'" for p in self._PII_COLUMN_PATTERNS
        )
        rows = (
            await async_session.execute(
                text(
                    f"""
                    SELECT table_name, column_name
                      FROM information_schema.columns
                     WHERE table_schema = 'public'
                       AND lower(column_name) LIKE ANY (ARRAY[{like_patterns}])
                    """
                )
            )
        ).all()

        # 允许 pii_vault schema 有 PII 密文列，但 public schema 不得有直标识
        violations = [(r.table_name, r.column_name) for r in rows]
        assert not violations, (
            f"public schema 发现 PII 直标识字段（违反 D7 主库零直标识）：{violations}"
        )

    @pytest.mark.asyncio
    async def test_pii_vault_schema_isolated_from_public(self, async_session: AsyncSession):
        """pii_vault schema 独立于 public：student_identity 在 pii_vault 而非 public."""
        # public 中不应有 student_identity 表
        public_has = (
            await async_session.execute(
                text(
                    "SELECT 1 FROM information_schema.tables "
                    "WHERE table_schema = 'public' AND table_name = 'student_identity'"
                )
            )
        ).scalar()
        assert public_has is None, "student_identity 不得在 public schema"

        # pii_vault 中应有 student_identity 表
        vault_has = (
            await async_session.execute(
                text(
                    "SELECT 1 FROM information_schema.tables "
                    "WHERE table_schema = 'pii_vault' AND table_name = 'student_identity'"
                )
            )
        ).scalar()
        assert vault_has is not None, "pii_vault.student_identity 必须存在（D7）"


# ────────────────────────────────────────────────────────────────────
# 6. StudentIdentity DTO 不变量
# ────────────────────────────────────────────────────────────────────

class TestStudentIdentityDTO:
    """明文 DTO 仅内存存在、不可变（frozen + slots）."""

    def test_immutable(self):
        """DTO frozen：不可修改字段."""
        sid = uuid.uuid4()
        identity = StudentIdentity(
            student_alias_id=sid,
            name="测试",
            phone="13000000000",
            address="测试地址",
            parent_contact="测试家长",
        )
        with pytest.raises(Exception):  # FrozenInstanceError
            identity.name = "改"  # type: ignore[misc]

    def test_no_dict_namespace_pollution(self):
        """slots=True：无 __dict__，防止意外属性注入."""
        sid = uuid.uuid4()
        identity = StudentIdentity(
            student_alias_id=sid,
            name="测试",
            phone="13000000000",
            address="测试地址",
            parent_contact="测试家长",
        )
        assert not hasattr(identity, "__dict__"), "slots DTO 不应有 __dict__"
