-- 镜像 alembic 0028（gate_cert_fk_failure_trail.py）downgrade：成对回收本迁移
-- 引入的约束/表/触发器，并恢复 gate_run.certificate_id 的 NOT NULL。
-- 触发器函数 raise_append_only_error 为 0005 统一所有，此处不触碰
-- （对称回滚只回收本迁移自身的对象）。
--
-- 回滚前提说明：SET NOT NULL 仅在不存在 certificate_id IS NULL 的 gate_run 行时
-- 可执行。干净库演练（make migrate-go-check 的 up→down→up）天然满足；带数据的
-- 真实库回滚前须先处置失败留痕行——0028 起 fail/review 不再挂占位证书，
-- NULL 即「未签发证书」的合法态（任务卡验收 #2 方案①）。本文件不加 DELETE
-- 一类的清理语句：留痕账只增不改（D1/A3），数据处置永远走显式人工流程。

DROP TRIGGER IF EXISTS trg_gate_failure_append_only ON gate_failure;
DROP INDEX IF EXISTS ix_gate_failure_failed_at;
DROP INDEX IF EXISTS ix_gate_failure_artifact;
DROP TABLE IF EXISTS gate_failure;

ALTER TABLE item_lifecycle_transition DROP CONSTRAINT IF EXISTS fk_ilt_certificate;
ALTER TABLE publication DROP CONSTRAINT IF EXISTS fk_pub_certificate;
ALTER TABLE passage DROP CONSTRAINT IF EXISTS fk_passage_certificate;
ALTER TABLE corpus_version DROP CONSTRAINT IF EXISTS fk_cv_gate_certificate;
ALTER TABLE material_version DROP CONSTRAINT IF EXISTS fk_mv_gate_certificate;
ALTER TABLE item_version DROP CONSTRAINT IF EXISTS fk_iv_gate_certificate;

ALTER TABLE gate_run ALTER COLUMN certificate_id SET NOT NULL;
