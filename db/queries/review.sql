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
