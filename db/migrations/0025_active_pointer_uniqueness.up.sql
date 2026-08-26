-- T-W5-019（估计器指针切换并发安全）：active 指针唯一性防线声明 + 切换留痕主体字段。
-- 语句为 alembic 镜像 0025（active_pointer_uniqueness.py）upgrade 的原句，
-- 语义 parity 由 make migrate-go-check 在 CI 复核（本机无 Docker/PG，未在线捕获）。
--
-- 唯一性防线事实核验（双源已读）：偏唯一索引
--   uq_estimator_run_one_active_per_scope ON estimator_run (purpose_scope) WHERE retired_at IS NULL
-- 已由 alembic 0016 / db/migrations 0016 建立（T-W4-002），同一谓词不重建第二份索引——
-- 重复索引只会增加写放大而不增强不变量；验收 #1 的“每 scope 活跃指针唯一由 DB 部分
-- 唯一索引保证”即以此索引为准。
-- 本迁移补上并发切换留痕缺失的“谁”：estimator_run 作为估计器版本操作元数据账
-- （0016 注释自述，非 D1 三本账、不套 append-only 触发器），新增 activated_by 记录
-- 登记主体，配合既有 activated_at 与退役链可完整还原「谁在何时把 scope 从版本 A
-- 切到版本 B」（任务卡 T-W5-019 验收 #3）。

ALTER TABLE estimator_run ADD COLUMN IF NOT EXISTS activated_by TEXT;

-- 存量行登记主体不可考，统一回填 'system'（应用侧切换自此卡起必须显式传 who）。
UPDATE estimator_run SET activated_by = 'system' WHERE activated_by IS NULL;

ALTER TABLE estimator_run ALTER COLUMN activated_by SET DEFAULT 'system';
ALTER TABLE estimator_run ALTER COLUMN activated_by SET NOT NULL;
