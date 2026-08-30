-- 组卷候选池只读查询面（审计卡 #147：编排层题源）。
-- 纪律：只读 SELECT（宪法铁律 1 的读侧）；语句只住本目录（SQL-2），禁止在
-- Go 代码拼 SQL。池过滤语义与 core/assembly.CandidateStore 端口注释一致：
-- serving 视图已承担 published（status='published' AND published_at 非空 AND
-- retired_at 为空）过滤，本语句只叠加 学科包 × 学段 两个投影维度——学段取
-- objective JSONB 顶层的 gradeband 标量（与 _SERVING_POOL_SQL 的
-- objective->>'gradeband' 同列口径）。

-- name: ListServingItemVersionsByPackGradeband :many
-- 某学科包×学段的 published 候选题（编排层 PaperItemSource 的 DB 实现）。
-- 块列只取组卷+渲染所需最小投影（objective/interaction_ref/lineage/content）；
-- item_version_id 升序保证池加载序确定（R-Z-01：同输入同池序）。
SELECT
	v.item_version_id,
	v.item_id,
	v.template_version_id,
	v.objective,
	v.interaction_ref,
	v.lineage,
	v.content
FROM v_serving_item_version v
WHERE v.pack_id = sqlc.arg(pack_id)
	AND v.objective->>'gradeband' = sqlc.arg(gradeband)::text
ORDER BY v.item_version_id ASC;
