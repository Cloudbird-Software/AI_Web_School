-- T-W4-031 PII 保险库独立 schema 参考定义（D7）
-- ============================================================================
-- 本文件是 alembic/versions/0014_pii_vault.py 的可读参考副本，**不是**执行
-- 入口。一切 DDL 走 alembic 迁移（宪法 X7：禁止手工改库）。
--
-- 设计要点：
-- 1. 独立 schema `pii_vault`：与主库 public 物理隔离，REVOKE ALL FROM PUBLIC，
--    仅授权角色 pii_vault_reader 可 SELECT（白名单访问控制）。
-- 2. 列级加密：直标识字段以 bytea 密文 + 独立 nonce 落库；明文不落地磁盘
--    （加解密在应用层 src/core/compliance/pii_encryption.py 完成，密钥环境
--    变量 PII_VAULT_KEY 注入，永不入库/入日志/入 prompt，宪法 X3）。
-- 3. 主库零直标识：public schema 仅有 student_alias_id（UUID 不可逆别名），
--    无姓名/电话/地址等直标识字段（由 test_pii_vault::test_main_db_no_pii
--    扫描 information_schema 断言）。
-- 4. 算法：AES-256-GCM（256-bit key / 96-bit nonce / 128-bit tag）；
--    nonce 每次写入随机生成、与密文同列存（nonce 不保密但须唯一）。
-- ============================================================================

-- pgcrypto 不再使用（应用层加解密）；保留注释说明历史决策：
-- 曾考虑用 pgp_sym_encrypt 在 DB 侧加解密，但密钥会出现在 SQL bind param
-- 与 query log，违反 X3；故改为应用层 cryptography.AESGCM（显式依赖：
-- pyproject.toml cryptography>=42，T-W4-031 引入，X8 已登记）。

CREATE SCHEMA IF NOT EXISTS pii_vault;

-- 白名单访问控制：默认拒绝所有角色，仅 pii_vault_reader 可查
REVOKE ALL ON SCHEMA pii_vault FROM PUBLIC;
REVOKE ALL ON ALL TABLES IN SCHEMA pii_vault FROM PUBLIC;

-- pii_vault_reader 角色由 DBA 在生产环境创建；开发环境由迁移幂等创建
-- （CREATE ROLE IF NOT EXISTS 不被 PG 原生支持，迁移用 DO 块捕获 duplicate）
DO $$ BEGIN
    CREATE ROLE pii_vault_reader NOLOGIN;
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;
GRANT USAGE ON SCHEMA pii_vault TO pii_vault_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA pii_vault TO pii_vault_reader;

CREATE TABLE pii_vault.student_identity (
    -- student_alias_id：与主库 public.practice_session.student_alias_id 同源；
    -- 此处是 PII 反查锚点，PII 保险库通过它关联匿名主库（不存外键跨 schema
    -- 强约束，避免主库 schema 变更锁死 PII 保险库演进）。
    student_alias_id     uuid         PRIMARY KEY,
    -- 直标识密文 + nonce（AES-256-GCM；nonce 96-bit 每次写入随机）
    name_ciphertext      bytea        NOT NULL,
    name_nonce           bytea        NOT NULL,
    phone_ciphertext     bytea        NOT NULL,
    phone_nonce          bytea        NOT NULL,
    address_ciphertext   bytea        NOT NULL,
    address_nonce        bytea        NOT NULL,
    parent_contact_ciphertext bytea   NOT NULL,
    parent_contact_nonce      bytea   NOT NULL,
    created_at           timestamptz  NOT NULL DEFAULT now()
);

-- 审计：访问日志（pii_vault_access_log）——每次读取记一条
-- （W4 范围：表结构预留，访问写入由应用层 pii_encryption.read_identity 触发）
CREATE TABLE pii_vault.access_log (
    access_id     uuid        PRIMARY KEY,
    student_alias_id uuid     NOT NULL,
    accessor      text        NOT NULL,  -- 调用方服务标识
    accessed_at   timestamptz NOT NULL DEFAULT now(),
    purpose       text        NOT NULL   -- 用途（审计要求：每次访问须说明用途）
);

-- 默认 REVOKE（白名单兜底）
REVOKE ALL ON pii_vault.student_identity FROM PUBLIC;
REVOKE ALL ON pii_vault.access_log FROM PUBLIC;
GRANT SELECT ON pii_vault.student_identity TO pii_vault_reader;
GRANT INSERT ON pii_vault.access_log TO pii_vault_reader;
