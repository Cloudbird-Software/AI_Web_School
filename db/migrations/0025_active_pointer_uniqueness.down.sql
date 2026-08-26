-- 镜像 alembic 0025 downgrade：仅回收本迁移引入的 activated_by（对称回滚）；
-- 偏唯一索引 uq_estimator_run_one_active_per_scope 属 0016 所有，此处不触碰。
ALTER TABLE estimator_run ALTER COLUMN activated_by DROP DEFAULT;
ALTER TABLE estimator_run DROP COLUMN IF EXISTS activated_by;
