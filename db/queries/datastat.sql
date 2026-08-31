-- 参数标定语句面（P0-5，2026-08-31 补齐）：冻结实现 src/core/data/ctt.py
-- ::run_ctt_calibration 的 Go 侧取证/落库面。
--
-- 纪律：item_param 是 append-only 账（迁移 0013 触发器物理兜底 UPDATE/DELETE
-- 禁止）；本文件只有 INSERT。幂等由 uq_item_param_identity 承担——同快照
-- （item_version_id, purpose_scope, source, method_version, as_of）重跑冲突，
-- 调用方翻译为「已标定」计数而非异常穿透（换 method_version 或更大 as_of 的
-- 新数据才产生新行，D6）。

-- name: ListCttResponseRecords :many
-- 单场景作答事件取数（与冻结实现 _FETCH_SQL 同语句面）：correct 取
-- scoring_trace.dimension_scores.correct（JSONB number；缺键行被 WHERE 过滤，
-- 不参与估计也不计入 sample_size——宁缺不猜）。
SELECT item_version_id,
       student_alias_id::text AS student_alias_id,
       (scoring_trace->'dimension_scores'->>'correct')::float AS correct,
       created_at
FROM response_event
WHERE scene = $1
  AND scoring_trace->'dimension_scores'->>'correct' IS NOT NULL;

-- name: InsertItemParam :exec
-- 实测参数行落账（source=measured_ctt 满足 ck_item_param_source_domain 正则）。
INSERT INTO item_param (
	param_id, item_version_id, purpose_scope, source,
	params, sample_size, method_version, as_of
) VALUES ($1, $2, $3, $4, $5, $6, $7, $8);
