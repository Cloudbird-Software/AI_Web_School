-- 镜像 alembic 0030（pii_vault_roles.py）downgrade：成对回滚到 0014 授权形态。
--
-- pii_vault_writer 角色本身不 DROP（0014 #43 判例：集群级角色可能被同集群其他
-- 库的 ACL 依赖引用，DROP 会使全量 down 在共享集群上失败；角色生命周期归
-- 部署/DBA）——只精确回收本迁移授予的授权。回收后 writer 角色以空授权 NOLOGIN
-- 留存，不构成任何权限残留。
--
-- 回滚后果说明（评审留痕）：down 后 reader 重新持有 INSERT access_log、失去
-- SELECT access_log——即回到 0014 的「读身份角色兼写审计、审计不可自查」缺陷
-- 形态；Go 侧 VaultService 的独立审计执行面在该形态下将因缺 INSERT 授权而
-- 写入失败（X12：审计失败上交而非静默吞）。因此生产回滚本迁移前必须先回滚
-- 依赖 0030 角色前提的应用装配，不可单独降级。

-- ── 回收 B：writer 授权 ─────────────────────────────────────────────────
REVOKE INSERT ON pii_vault.access_log FROM pii_vault_writer;
REVOKE INSERT ON pii_vault.student_identity FROM pii_vault_writer;
REVOKE USAGE ON SCHEMA pii_vault FROM pii_vault_writer;

-- ── 回滚 C：reader 恢复 0014 形态（INSERT 归还、SELECT 收回）─────────────
GRANT INSERT ON pii_vault.access_log TO pii_vault_reader;
REVOKE SELECT ON pii_vault.access_log FROM pii_vault_reader;
