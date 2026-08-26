-- T-W5-014（SQL-2）：AI 调用台账的读写语句面（core/ai 总线消费）。
-- 台账 = D10 审计账（append-only 由 0026 触发器 trg_ai_call_ledger_append_only
-- 物理强制），本文件只允许 INSERT 与只读 SELECT——不存在 UPDATE/DELETE 语句面，
-- 依赖方向与冻结实现一致（总线内统一落账，业务包不直写）。
-- 命名规约对齐 estimator.sql：:exec 写入、:many 按产物归集查询
-- （W6 成本核算按 artifact_ref 汇总单题全生命周期 AI 成本）。

-- name: InsertAICallLedger :exec
-- 台账唯一写路径：ok/failed/rejected 三态共用一表，一次调用恰一行。
INSERT INTO ai_call_ledger (
	call_id, modality, task_level, task_name, provider, model, model_version,
	prompt_hash, prompt_version, token_in, token_out, cost_cny, duration_ms,
	status, reason, fallback, artifact_ref, caller_name, created_at
) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17, $18, $19);

-- name: ListAICallLedgerByArtifact :many
-- 单题全生命周期 AI 成本归集键（T-W4-010 冻结语义的 SQL 面）；升序保时间线。
SELECT * FROM ai_call_ledger
WHERE artifact_ref = $1
ORDER BY created_at ASC;
