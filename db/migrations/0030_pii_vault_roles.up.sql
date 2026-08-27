-- T-W5-012（PII 保险库权限模型与审计独立事务，W5-R Go 重锚定）：vault 角色职责分离。
-- 本文件未走 gen_migrations_from_alembic.py 在线捕获（生成机无 Docker/PG），
-- 语句为 alembic 镜像 0030（pii_vault_roles.py）upgrade 的原句，语义 parity 由
-- CI make migrate-go-check 复核。双源纪律：语义修改必须同时落 alembic 0030
-- 与本文件（SQL-1 成对进 gate）。
--
-- 缺陷事实（任务卡目标说明 + 0014 现状实读）：
--   ①只有单角色 pii_vault_reader，却被同时授 SELECT student_identity（读身份）
--     与 INSERT access_log（写审计）——读身份与写审计混在同一角色；写身份
--     （INSERT student_identity）反而没有任何角色承载，应用只能以表属主连接
--     直写，D9 最小权限落空；
--   ②reader 有 INSERT access_log 却无 SELECT access_log——「每次访问必留痕」
--     的审计账（D7）连读身份自己都无法复核，审计完整性不可验证；
--   ③冻结实现 read_identity 的审计写入与业务同事务，业务回滚时审计静默消失
--     （D11「审计副作用必须走显式独立事务」违例）——Go 侧
--     core/compliance.VaultService 以双 Executor 独立事务终结该缺陷，审计面
--     执行面持 pii_vault_writer 角色（独立连接），本迁移为其提供角色前提。
--
-- 三段内容（全为授权面 GRANT/REVOKE + 幂等 CREATE ROLE，零表结构变更）：
--   A. 新建 pii_vault_writer 写身份角色（幂等 DO 块，0014 同惯例）
--   B. writer 授权：INSERT student_identity（写身份）+ INSERT access_log（写审计）
--   C. reader 重整为「读身份+读审计」：补 SELECT access_log（①审计可复核），
--      收回 0014 误授的 INSERT access_log（写审计职责移交 writer）
-- down 成对回滚到 0014 授权形态；集群级角色不 DROP（0014 #43 判例：
-- 角色生命周期归部署/DBA，迁移只管本库 schema 与授权）。

-- ── A. pii_vault_writer 写身份角色（幂等创建）────────────────────────────
DO $$ BEGIN
    CREATE ROLE pii_vault_writer NOLOGIN;
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- ── B. writer 授权：写身份 + 写审计 ─────────────────────────────────────
GRANT USAGE ON SCHEMA pii_vault TO pii_vault_writer;
GRANT INSERT ON pii_vault.student_identity TO pii_vault_writer;
GRANT INSERT ON pii_vault.access_log TO pii_vault_writer;

-- ── C. reader 重整：读身份 + 读审计（收回写审计）────────────────────────
GRANT SELECT ON pii_vault.access_log TO pii_vault_reader;
REVOKE INSERT ON pii_vault.access_log FROM pii_vault_reader;
