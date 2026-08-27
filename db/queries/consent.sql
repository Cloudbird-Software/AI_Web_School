-- T-W5-011（SQL-2）：家长授权账（parental_consent）的并发安全读写语句面。
-- 版本分配临界区 = 调用方显式事务内的完整序列（D11 领域服务不自 commit）：
--   LockConsentChain（per-chain advisory xact lock，串行化同一
--   (student_alias_id, purpose) 授权链的全部写入者，含「首事件无行可锁」竞态）
--   → GetLatestConsentEvent（读链顶版本与撤回前置校验）→ InsertConsentEvent
--   （版本 = 链顶+1 写入新事件）。
-- 唯一索引 uq_parental_consent_version_per_purpose（0027）是最后防线：其拒绝以
-- SQLSTATE 23505 到达应用层并被翻译为哨兵错误 ErrConsentConflict，异常不泄漏。
-- append-only 纪律：本文件只有 INSERT/SELECT——UPDATE/DELETE 无查询面可写，
-- 0015 的 trg_parental_consent_append_only 触发器物理兜底。

-- name: LockConsentChain :exec
-- per-chain 事务级 advisory 排他锁（pg_advisory_xact_lock 返回 void，
-- 事务结束自动释放）。键含 student_alias_id 与 purpose 二级粒度：
-- 不同学生、不同 purpose 的授权链互不阻塞。
SELECT pg_advisory_xact_lock(hashtextextended('parental_consent:' || $1::text || ':' || $2::text, 0));

-- name: GetLatestConsentEvent :one
-- 链顶事件（版本最大恰一行——0027 唯一索引保证确定性）。
-- $2 显式 ::text 定型：scope ->> 'purpose' 的左值是 text，无定型时生成层会把
-- 形参推成 jsonb 字节面（[]byte），运行时撞 text = bytea 无算符错误。
SELECT * FROM parental_consent
WHERE student_alias_id = $1 AND scope ->> 'purpose' = $2::text
ORDER BY version DESC
LIMIT 1;

-- name: InsertConsentEvent :exec
-- 追加授权事件（grant/revoke 共面）：version 由调用方在锁内算出；
-- created_at 显式传值使留痕时间线与内存实现逐字段同构；
-- recorded_by 为留痕的登记主体维度（0027 列）。
INSERT INTO parental_consent (
	consent_id, student_alias_id, event_type, scope,
	valid_from, valid_until, version, recorded_by, created_at
) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9);

-- name: ListConsentHistory :many
-- 全量事件账（升序）：第 n 行 version 即 from→to 时间线的 to(n)/from(n+1)，
-- who/when 取 recorded_by / created_at——append-only 只读投影还原面。
-- $2 ::text 定型理由同 GetLatestConsentEvent。
SELECT * FROM parental_consent
WHERE student_alias_id = $1 AND scope ->> 'purpose' = $2::text
ORDER BY version ASC;
