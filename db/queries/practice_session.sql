-- T-W5-004（SQL-2）：会话题序写入/读取语句面（题序一经固化不可变）。
-- 题序承载 = practice_session.item_sequence（0011 行内 JSONB 数组，条目
-- {item_version_id, placement_token, item_number}）。本文件只有 INSERT/SELECT：
-- 题序改写（UPDATE）与整行删除（DELETE）无查询面可写，0030 的锚列触发器物理
-- 兜底；幂等判读所需的存量读取同为 SELECT。
-- status/wrong_marks/current_index 等列按冻结 start_session（src/core/session/
-- service.py）的创建形态在 INSERT 时一次定型；运行态推进（current_index/
-- status/... 的 UPDATE）属作答提交域（T-W5-018），不在本文件——本文件不为
-- 会话提供任何改写语句（结构上不可能改写题序，非应用层 if）。

-- name: InsertPracticeSession :exec
-- 会话创建即题序固化（0030 触发器拒绝锚列改写与整行删除；session_id 重复以
-- PK 23505 到达应用层，由 core/session 翻译为幂等成功或题序冲突哨兵）。
INSERT INTO practice_session (
	session_id, student_alias_id, scene, gradeband, status, paper_id,
	item_sequence, current_index, retest_wrong, wrong_marks, time_limit_sec,
	answered_count, correct_count, started_at, last_resume_at,
	last_activity_at, completed_at
) VALUES (
	$1, $2, $3, $4, $5, $6,
	$7, $8, $9, $10, $11,
	$12, $13, $14, $15, $16, $17
);

-- name: GetPracticeSessionItemSequence :one
-- 题序读取面：存量读出（幂等判读 + 按 item_number 升序还原的原始账面）；
-- 会话存在性判定由此吸收（ErrNoRows → 会话不存在）。
SELECT item_sequence FROM practice_session WHERE session_id = $1;
