-- T-W5-018（SQL-2）：会话作答提交（幂等 + 并发安全）的语句面。
-- 提交临界区 = 调用方显式事务内的完整序列（D11 领域服务不自 commit）：
--   LockSessionSubmission（per-session advisory xact lock，串行化同一会话的
--   全部提交者，含「首次提交无登记行可锁」竞态）→ GetSubmissionByIdempotencyKey
--   （幂等判定先行：命中即原样返回首次事件、零副作用）→ GetSessionForSubmit
--   （会话行 FOR UPDATE，验收 #1 的行锁字面语义）→ 状态/时长/题序校验 →
--   事件入账（core/events.Writer 经 events.sql 的 InsertResponseEvent）→
--   InsertResponseSubmission（幂等登记）→ AdvanceSessionAfterSubmit（推进）。
-- 幂等唯一性防线：pk_response_submission (session_id, item_version_id,
-- answer_digest)（0032）——绕过应用锁的重复写入以 SQLSTATE 23505 明确拒绝，
-- core/session 映射为哨兵 ErrSubmissionConflict，异常不泄漏。
-- practice_session 是运行态账（0011）：current_index/answered_count/status 等
-- 运行态字段可更新（题序等身份字段不可变由迁移触发器物理兜底）；本文件的
-- UPDATE 只触运行态列——两表 INSERT/SELECT 面不含任何账本改写语句.

-- name: LockSessionSubmission :exec
-- per-session 事务级 advisory 排他锁（pg_advisory_xact_lock 返回 void，事务
-- 结束自动释放）。键 = practice_session 域前缀 + 会话 id：不同会话互不阻塞.
SELECT pg_advisory_xact_lock(hashtextextended('practice_session:' || $1::text, 0));

-- name: GetSubmissionByIdempotencyKey :one
-- 幂等判定：三元组键恰一回指首次事件（复合主键保证确定性）。无行 =
-- pgx.ErrNoRows（调用方按未命中继续提交流程）.
SELECT event_id, event_created_at FROM response_submission
WHERE session_id = $1 AND item_version_id = $2 AND answer_digest = $3;

-- name: GetSessionForSubmit :one
-- 会话行 FOR UPDATE（验收 #1：提交路径对会话行加锁后再校验题序与推进）；
-- 与 advisory 锁构成双锁分层（序恒定：先 advisory 后行锁，无死锁面）.
SELECT * FROM practice_session WHERE session_id = $1 FOR UPDATE;

-- name: InsertResponseSubmission :exec
-- 幂等登记（与事件同一事务：登记账行永远回指已入账的真实事件——FK 兜底）；
-- created_at 显式传值使登记时刻与事件时刻、内存实现逐字段同构.
INSERT INTO response_submission (
	session_id, item_version_id, answer_digest, event_id, event_created_at, created_at
) VALUES ($1, $2, $3, $4, $5, $6);

-- name: AdvanceSessionAfterSubmit :exec
-- 提交推进：current_index/answered_count 各恰 +1 + 活动时刻（board 验收
-- 「current_index 恰好推进 1」的物理面）。对错记账与内存实现同构（2026-08-31
-- E2E 实证修复：correct_count 只在评分轨迹显式判对时累加，显式判错追加
-- wrong_marks 错题标记；轨迹不含显式判定时两账都不动，不猜对错）。
-- 完结判定与 Python 冻结实现同构：主序列走完且未开启回测 → completed
--（开回测的会话进入回测轮，归会话状态机域，本卡不推进其完结）。
-- CASE 引用的是本行旧值（UPDATE 语义）.
UPDATE practice_session SET
	current_index = current_index + 1,
	answered_count = answered_count + 1,
	correct_count = correct_count + sqlc.arg(correct_delta)::int,
	wrong_marks = CASE
		WHEN sqlc.arg(wrong_mark)::jsonb IS NULL THEN wrong_marks
		ELSE COALESCE(wrong_marks, '[]'::jsonb) || sqlc.arg(wrong_mark)::jsonb
	END,
	last_activity_at = $2,
	status = CASE
		WHEN current_index + 1 >= jsonb_array_length(item_sequence) AND NOT retest_wrong
			THEN 'completed'
		ELSE status
	END,
	completed_at = CASE
		WHEN current_index + 1 >= jsonb_array_length(item_sequence) AND NOT retest_wrong
			THEN $2
		ELSE completed_at
	END
WHERE session_id = $1;

-- name: MarkSessionRestPrompted :exec
-- 时长保护置位（Python _check_time_protection 同语义）：拒绝提交但留下
-- rest_prompted 状态（resume 重置计时后恢复作答）。只动状态列，零事件写入.
UPDATE practice_session SET status = 'rest_prompted' WHERE session_id = $1;

-- name: GetPracticeSessionRuntime :one
-- 会话运行态读取面（GO-RW-002 服务域）：状态投影、归属断言与取题判定的取数
-- 前提。普通读（不锁行）：写路径的互斥由各自语句面承担（提交=advisory+行锁；
-- resume/abandon=下方 UPDATE），读取不参与锁序。
SELECT * FROM practice_session WHERE session_id = $1;

-- name: ResumeSessionAfterRest :one
-- 休息确认（Python resume_session 同语义）：rest_prompted/active → active，
-- 计时锚点（last_resume_at）与活动时刻重置为同一确认时刻。RETURNING * 供
-- 服务域就地装配状态投影，免二次读。completed/abandoned 的拒绝由服务域在
-- 读取态判定（本语句不带状态谓词——拒绝发生在写之前，不在写中半途失败）。
UPDATE practice_session SET
	status = 'active',
	last_resume_at = $2,
	last_activity_at = $2
WHERE session_id = $1
RETURNING *;

-- name: AbandonSessionByID :one
-- 放弃会话（Python abandon_session 同语义）：状态置 abandoned + 活动时刻；
-- 已作答事件保留在 response_event 账（零删除——append-only 无 DELETE 面）。
-- completed 的拒绝同上：服务域在写之前判定。
UPDATE practice_session SET
	status = 'abandoned',
	last_activity_at = $2
WHERE session_id = $1
RETURNING *;
