-- 复习到期只读取证（GO-RW-006 服务化接线 / 审计 #155）。
-- review_queue_entry 的到期判据与 core/review.DueReviews 纯函数同口径：
-- status='pending' 且 due_at <= now（now 恰等视为已到期——「今天到期今天取」）。
-- 排序 due_at 升序（最逾期优先）、entry_id 兜底确定性；命中索引
-- ix_review_queue_entry_due (student_alias_id, status, due_at)。

-- name: ListDueReviewEntries :many
SELECT entry_id, student_alias_id, item_version_id, policy_id, policy_version,
       stage, status, source_error_type_id, enqueued_at, due_at
FROM review_queue_entry
WHERE student_alias_id = $1
  AND status = 'pending'
  AND due_at <= $2
ORDER BY due_at, entry_id
LIMIT $3;

-- ============ 复习队列入队写路径（P0-4，2026-08-31 补齐） ============
-- 冻结实现 src/core/review/service.py::sync_review_queue 的 Go 侧取证面：
-- 读 response_event（只读 SELECT——作答事件账永不被本模块写），经
-- core/review.RebuildQueue 纯函数全量重放，幂等 upsert 进 review_queue_entry
-- （派生队列，非三本账，允许 UPDATE）。全量重放而非增量：天然满足
-- 「队列版本可重建」（R-Z-07）——同一事件流 × 同一策略版本，重放结果必一致。

-- name: GetReviewPolicy :one
-- 策略版本的固定间隔表（天）。调用方应先确认迁移 0012 已执行（v1 内置
-- fixed-interval/1.0.0：[1,3,7,21]）；缺失返回 pgx.ErrNoRows 由调用方映射。
SELECT policy_id, policy_version, intervals_days
FROM review_policy
WHERE policy_id = $1 AND policy_version = $2;

-- name: ListStudentReviewEvents :many
-- 学生的作答事件排程投影（只读，与冻结 _EVENTS_SQL 同语句面）：
-- ORDER BY created_at, event_id 升序喂入 RebuildQueue（乱序会破坏状态机语义）。
SELECT event_id, item_version_id, created_at, scoring_trace, error_inferences
FROM response_event
WHERE student_alias_id = $1
ORDER BY created_at, event_id;

-- name: UpsertReviewQueueEntry :exec
-- 幂等 upsert：UNIQUE(student_alias_id, item_version_id, policy_id, policy_version)
-- 冲突时仅在状态实际变化时更新（IS DISTINCT FROM 判据），保证「重复同步结果
-- 不变」——updated_at 不因无变化的重放而空转。entry_id / enqueued_at 不在
-- UPDATE 集内：重放保留首次入队的稳定身份与时刻（与冻结实现「重置不改
-- 入队时间」同构；enqueued_at 由 RebuildQueue 状态机供给）。
INSERT INTO review_queue_entry (
    entry_id, student_alias_id, item_version_id, policy_id, policy_version,
    stage, status, source_error_type_id, last_event_id, enqueued_at, due_at,
    updated_at
) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
ON CONFLICT (student_alias_id, item_version_id, policy_id, policy_version)
DO UPDATE SET
    stage = EXCLUDED.stage,
    status = EXCLUDED.status,
    source_error_type_id = EXCLUDED.source_error_type_id,
    last_event_id = EXCLUDED.last_event_id,
    due_at = EXCLUDED.due_at,
    updated_at = EXCLUDED.updated_at
WHERE review_queue_entry.stage IS DISTINCT FROM EXCLUDED.stage
   OR review_queue_entry.status IS DISTINCT FROM EXCLUDED.status
   OR review_queue_entry.source_error_type_id IS DISTINCT FROM EXCLUDED.source_error_type_id
   OR review_queue_entry.last_event_id IS DISTINCT FROM EXCLUDED.last_event_id
   OR review_queue_entry.due_at IS DISTINCT FROM EXCLUDED.due_at;
