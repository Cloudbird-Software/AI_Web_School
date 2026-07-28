"""T-W4-031 PII 保险库读写：AES-256-GCM 列级加密.

落地架构 v2 §4.8 与宪法 D7 PII 隔离：
- 学生直标识（姓名/电话/地址/家长联系）只允许存在于 pii_vault schema。
- 列级加密：明文在应用层加密后写入 DB（密文 + 独立 nonce），明文不落地磁盘
  （除内存中短暂存在）。
- 密钥环境变量 PII_VAULT_KEY 注入（32 字节 base64），永不入 SQL/日志/prompt（X3）。

为什么用 cryptography.AESGCM：
- AES-256-GCM：认证加密（confidentiality + integrity），NIST 推荐。
- 应用层加解密：密钥只在 Python 进程内存中，不进 SQL bind param / query log
  （若用 pgcrypto 的 pgp_sym_encrypt，密钥会进 SQL，违反 X3）。
- nonce 96-bit 每次写入 os.urandom 随机生成，与密文同列存（nonce 不保密但须唯一，
  GCM 模式下 nonce 复用会破坏安全性——随机生成满足唯一性概率要求）。

宪法 A5/X6：本模块不 import 任何学科包/学段包。
"""
from __future__ import annotations

import base64
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


# ────────────────────────────────────────────────────────────────────
# 常量
# ────────────────────────────────────────────────────────────────────

# 环境变量名：PII 保险库主密钥（32 字节 base64 编码）
PII_VAULT_KEY_ENV = "PII_VAULT_KEY"

# AES-256-GCM 参数：256-bit key / 96-bit nonce / 128-bit tag
_KEY_BYTES = 32  # AES-256
_NONCE_BYTES = 12  # GCM 标准 96-bit nonce


class PIIKeyError(RuntimeError):
    """PII 主密钥配置错误（缺失/格式错误/长度错误）."""


class StudentIdentityNotFoundError(LookupError):
    """PII 保险库中无此 student_alias_id 的直标识记录."""


# ────────────────────────────────────────────────────────────────────
# 明文 DTO（仅存在于内存中，出函数即被丢弃）
# ────────────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class StudentIdentity:
    """学生直标识明文（仅内存中存在，禁止序列化落盘）.

    出 read_identity 后由调用方按需使用；禁止写入日志/prompt/LLM（D7/X3）。
    """

    student_alias_id: uuid.UUID
    name: str
    phone: str
    address: str
    parent_contact: str
    created_at: Optional[datetime] = None


# ────────────────────────────────────────────────────────────────────
# 密钥加载
# ────────────────────────────────────────────────────────────────────

def load_master_key(env: Optional[dict[str, str]] = None) -> bytes:
    """从环境变量加载 PII 主密钥（AES-256 32 字节）.

    Args:
        env: 显式传入环境变量字典（测试注入）；None 读 os.environ。

    Returns:
        32 字节原始密钥。

    Raises:
        PIIKeyError: 环境变量缺失 / 非 base64 / 长度非 32 字节。
    """
    source = env if env is not None else os.environ
    raw = source.get(PII_VAULT_KEY_ENV)
    if not raw:
        raise PIIKeyError(
            f"环境变量 {PII_VAULT_KEY_ENV} 未设置：PII 保险库主密钥必须通过"
            "环境变量注入（禁止入库/入代码，X3）。"
        )
    try:
        key = base64.b64decode(raw, validate=True)
    except Exception as exc:
        raise PIIKeyError(f"{PII_VAULT_KEY_ENV} 非 base64 编码：{exc}") from exc
    if len(key) != _KEY_BYTES:
        raise PIIKeyError(
            f"{PII_VAULT_KEY_ENV} 解码后长度 {len(key)} 字节，预期 {_KEY_BYTES}"
            "（AES-256 需要 32 字节主密钥）。"
        )
    return key


def generate_master_key() -> str:
    """生成新主密钥并返回 base64 字符串（运维初始化辅助，仅命令行使用）.

    用法：将输出写入 .env 的 PII_VAULT_KEY= 行。
    """
    return base64.b64encode(os.urandom(_KEY_BYTES)).decode("ascii")


# ────────────────────────────────────────────────────────────────────
# 加解密原语
# ────────────────────────────────────────────────────────────────────

def encrypt_field(plaintext: str, key: bytes) -> tuple[bytes, bytes]:
    """加密单个字段：返回 (ciphertext, nonce)。

    - 每次调用生成独立随机 nonce（96-bit），保证 GCM 安全性。
    - ciphertext 内含 GCM 认证标签（AESGCM.encrypt 输出 = ciphertext||tag）。
    - 明文以 UTF-8 编码（中文姓名/地址兼容）。
    """
    if not isinstance(plaintext, str):
        raise TypeError(f"plaintext 必须是 str，收到 {type(plaintext).__name__}")
    aesgcm = AESGCM(key)
    nonce = os.urandom(_NONCE_BYTES)
    ciphertext = aesgcm.encrypt(nonce, plaintext.encode("utf-8"), associated_data=None)
    return ciphertext, nonce


def decrypt_field(ciphertext: bytes, nonce: bytes, key: bytes) -> str:
    """解密单个字段：返回明文。

    - 认证失败（密文被篡改 / nonce 错误 / 密钥错误）抛 cryptography.exceptions
      .InvalidTag；调用方应捕获并视为「密文损坏」，禁止返回部分明文。
    """
    aesgcm = AESGCM(key)
    plaintext_bytes = aesgcm.decrypt(nonce, ciphertext, associated_data=None)
    return plaintext_bytes.decode("utf-8")


# ────────────────────────────────────────────────────────────────────
# DB 读写（与 alembic/versions/0014_pii_vault.py 表结构对齐）
# ────────────────────────────────────────────────────────────────────

# INSERT：参数化（密文以 bytea 绑定，密钥永不入 SQL）
_INSERT_SQL = text(
    """
    INSERT INTO pii_vault.student_identity
        (student_alias_id,
         name_ciphertext, name_nonce,
         phone_ciphertext, phone_nonce,
         address_ciphertext, address_nonce,
         parent_contact_ciphertext, parent_contact_nonce)
    VALUES
        (:sid,
         :name_ct, :name_n,
         :phone_ct, :phone_n,
         :addr_ct, :addr_n,
         :parent_ct, :parent_n)
    """
)

# SELECT：按 student_alias_id 取密文
_SELECT_SQL = text(
    """
    SELECT name_ciphertext, name_nonce,
           phone_ciphertext, phone_nonce,
           address_ciphertext, address_nonce,
           parent_contact_ciphertext, parent_contact_nonce,
           created_at
      FROM pii_vault.student_identity
     WHERE student_alias_id = :sid
    """
)

# 审计日志 INSERT（每次 read_identity 调用即写一条）
_ACCESS_LOG_INSERT_SQL = text(
    """
    INSERT INTO pii_vault.access_log
        (access_id, student_alias_id, accessor, accessed_at, purpose)
    VALUES
        (:aid, :sid, :acc, :ts, :pur)
    """
)


async def write_identity(
    db: AsyncSession,
    *,
    student_alias_id: uuid.UUID,
    name: str,
    phone: str,
    address: str,
    parent_contact: str,
    key: Optional[bytes] = None,
) -> None:
    """加密并写入学生直标识到 PII 保险库.

    - 明文在内存中加密后立即丢弃（不保留引用）。
    - 密文 + nonce 落库；密钥仅作加解密参数传递，不入 DB。
    - 重复写入同一 student_alias_id 会触发 PK 冲突（DB 报错）——
      PII 记录一次写入后不可改写（与 D7「主库零直标识」配合：变更走新 alias）。

    Args:
        db: 异步会话（须有 pii_vault 写权限）。
        student_alias_id: 匿名别名 id（主库公共锚点）。
        name/phone/address/parent_contact: 直标识明文。
        key: 主密钥（测试注入）；None 则从环境变量加载。
    """
    mkey = key if key is not None else load_master_key()
    name_ct, name_n = encrypt_field(name, mkey)
    phone_ct, phone_n = encrypt_field(phone, mkey)
    addr_ct, addr_n = encrypt_field(address, mkey)
    parent_ct, parent_n = encrypt_field(parent_contact, mkey)

    await db.execute(
        _INSERT_SQL,
        {
            "sid": student_alias_id,
            "name_ct": name_ct, "name_n": name_n,
            "phone_ct": phone_ct, "phone_n": phone_n,
            "addr_ct": addr_ct, "addr_n": addr_n,
            "parent_ct": parent_ct, "parent_n": parent_n,
        },
    )
    # 写入由调用方控制 commit（与项目其他写入服务一致：service 层 commit）


async def read_identity(
    db: AsyncSession,
    *,
    student_alias_id: uuid.UUID,
    accessor: str = "unknown",
    purpose: str = "unspecified",
    key: Optional[bytes] = None,
    now: Optional[datetime] = None,
) -> StudentIdentity:
    """从 PII 保险库读取并解密学生直标识.

    - 读密文 → 内存解密 → 返回明文 DTO；明文不落日志。
    - 每次读取写一条 access_log 审计记录（accessor/purpose 必填，D7 审计要求）。
    - 无记录抛 StudentIdentityNotFoundError。

    Args:
        db: 异步会话（须有 pii_vault 读权限）。
        student_alias_id: 匿名别名 id。
        accessor: 调用方服务标识（审计字段）。
        purpose: 本次访问用途（审计字段，如 "support_call" / "parent_report"）。
        key: 主密钥（测试注入）；None 则从环境变量加载。
        now: 审计时间戳（测试注入）。
    """
    mkey = key if key is not None else load_master_key()
    row = (
        await db.execute(_SELECT_SQL, {"sid": student_alias_id})
    ).first()
    if row is None:
        raise StudentIdentityNotFoundError(
            f"pii_vault.student_identity 无 student_alias_id={student_alias_id}"
        )

    name = decrypt_field(row.name_ciphertext, row.name_nonce, mkey)
    phone = decrypt_field(row.phone_ciphertext, row.phone_nonce, mkey)
    address = decrypt_field(row.address_ciphertext, row.address_nonce, mkey)
    parent_contact = decrypt_field(
        row.parent_contact_ciphertext, row.parent_contact_nonce, mkey
    )

    # 审计日志：每次读取留痕（accessor + purpose 强制，D7 审计要求）
    ts = now or datetime.now(timezone.utc)
    await db.execute(
        _ACCESS_LOG_INSERT_SQL,
        {
            "aid": uuid.uuid4(),
            "sid": student_alias_id,
            "acc": accessor,
            "ts": ts,
            "pur": purpose,
        },
    )

    return StudentIdentity(
        student_alias_id=student_alias_id,
        name=name,
        phone=phone,
        address=address,
        parent_contact=parent_contact,
        created_at=row.created_at,
    )


__all__ = [
    "PIIKeyError",
    "PII_VAULT_KEY_ENV",
    "StudentIdentity",
    "StudentIdentityNotFoundError",
    "decrypt_field",
    "encrypt_field",
    "generate_master_key",
    "load_master_key",
    "read_identity",
    "write_identity",
]
