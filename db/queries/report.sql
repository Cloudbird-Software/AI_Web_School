-- 弱项报告只读取证（GO-RW-005 服务化接线 / 审计 #155）。
-- response_event 是 append-only 作答事件账（D1）：只读 SELECT，排序键显式
-- 钉死（created_at, event_id）保证报告聚合的确定性输入序。
-- scene 为 NULL 表示跨场景汇总（D5：报告如实回显取数口径，由调用方显式传参）。

-- name: ListInferenceEventsByStudent :many
SELECT item_version_id, error_inferences
FROM response_event
WHERE student_alias_id = $1
  AND ($2 = '' OR scene::text = $2)
ORDER BY created_at, event_id;

-- name: ListRecommendedItemVersions :many
-- 针对性练习 5 题小卷取数：已发布实例池按 error_bindings 含该错误类型过滤，
-- 剔除产生过该错误证据的题目版本（ContributingItemVersionIDs 排除集语义），
-- item_version_id 升序兜底保证确定性。
SELECT item_version_id
FROM item_version
WHERE status = 'published'
  AND error_bindings @> jsonb_build_array(jsonb_build_object('error_type_id', $1::text))
  AND NOT (item_version_id = ANY($2::text[]))
ORDER BY item_version_id
LIMIT $3;
