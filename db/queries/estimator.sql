-- T-W5-019（SQL-2）：估计器活跃指针的读改写语句面。
-- 切换临界区 = 调用方显式事务内的完整序列（D11 领域服务不自 commit）：
--   LockEstimatorScope（per-scope advisory xact lock，串行化含「首次登记无行可锁」
--   在内的全部并发切换者）→ GetActive*（幂等判定）→ RetireActive*（先退役腾出
--   偏唯一索引谓词槽位 retired_at IS NULL）→ InsertEstimatorRun（后插入不撞
--   uq_estimator_run_one_active_per_scope；activated_by 即 0025 留痕「谁」列）。
-- 唯一索引是最后一道防线：其拒绝以 SQLSTATE 23505 到达应用层并被翻译为明确错误。
-- 行锁备选（SELECT ... FOR UPDATE 对活跃行加锁）因覆盖不了空表首插竞态而未采用，
-- 评审对照见 core/estimator/pg.go 注释。

-- name: LockEstimatorScope :exec
-- per-scope 事务级 advisory 排他锁（pg_advisory_xact_lock 返回 void，事务结束自动释放）。
SELECT pg_advisory_xact_lock(hashtextextended('estimator_run:' || $1::text, 0));

-- name: GetActiveEstimatorRun :one
-- 当前活跃指针（retired_at IS NULL 即唯一活口——偏唯一索引的谓词面）。
SELECT * FROM estimator_run
WHERE purpose_scope = $1 AND retired_at IS NULL
ORDER BY activated_at DESC LIMIT 1;

-- name: GetActiveEstimatorRunAsOf :one
-- D6 时间回溯：asOf 当时正活跃的版本（已登记且未退役或退役晚于 asOf）。
SELECT * FROM estimator_run
WHERE purpose_scope = $1 AND activated_at <= $2
  AND (retired_at IS NULL OR retired_at > $2)
ORDER BY activated_at DESC LIMIT 1;

-- name: RetireActiveEstimatorRun :exec
-- 先退役旧活跃行：先 UPDATE 后 INSERT 保证偏唯一索引不冲突（与冻结实现同序）。
UPDATE estimator_run SET retired_at = $2
WHERE purpose_scope = $1 AND retired_at IS NULL;

-- name: InsertEstimatorRun :exec
-- 后登记新活跃行（retired_at 缺省 NULL）；activated_by 为留痕的操作者维度。
INSERT INTO estimator_run (
	run_id, purpose_scope, model_version, code_digest,
	input_snapshot_id, graph_release_id, activated_by, activated_at
) VALUES ($1, $2, $3, $4, $5, $6, $7, $8);

-- name: ListEstimatorRunLineage :many
-- 全量行账（升序）：前驱行 model_version 即 from，本行 who/when/to 取
-- activated_by / activated_at / model_version——append-only 时间线还原面。
SELECT * FROM estimator_run
WHERE purpose_scope = $1
ORDER BY activated_at ASC;
