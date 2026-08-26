-- 镜像 alembic 0026 downgrade：成对回收 0026 引入的表/索引/触发器。
-- 触发器函数 raise_append_only_error 为 0005 统一所有，此处不触碰
-- （对称回滚只回收本迁移自身的对象）。
DROP TRIGGER IF EXISTS trg_ai_call_ledger_append_only ON ai_call_ledger;
DROP INDEX IF EXISTS ix_ai_call_ledger_created_at;
DROP INDEX IF EXISTS ix_ai_call_ledger_task;
DROP INDEX IF EXISTS ix_ai_call_ledger_artifact;
DROP TABLE IF EXISTS ai_call_ledger;
