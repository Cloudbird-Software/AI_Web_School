-- 组卷域查询面（审计卡 #151）：候选池 serving 视图读取 + 曝光账本双轨读写。
-- 表结构与约束由迁移承载：serving 视图（0008，published 过滤的单一事实源）、
-- paper_exposure / student_exposure（0010，append-only 触发器物理兜底）。
-- 事务纪律（S4/D11）：查询语句面向连接池（组卷读取阶段取数）；两条 INSERT
-- 必须运行在调用方已 begin 的显式事务内（core/assembly.PGExposureStore 经
-- WithTx 绑定传入，与 paper/paper_item 写入同进同退），提交/回滚由最外层
-- 调用方统一持有。两账只增不改（D1）：本文件只有 INSERT 与 SELECT——
-- UPDATE/DELETE 无查询面可写，DB 触发器兜底。

-- name: LoadServingCandidates :many
-- 候选池 serving 视图查询（CandidateStore.LoadCandidates 的取数语句）：
-- published 过滤由视图 WHERE 承担（0008：status='published' AND published_at
-- IS NOT NULL AND retired_at IS NULL——未过校验门的产物进不了 serving），
-- 本语句只做蓝图维度过滤：学科包 × 学段（objective->>'gradeband'，与冻结
-- _SERVING_POOL_SQL 的 WHERE 同判据）。列投影取 CandidateFromServingRow 的
-- 最小消费集；无 ORDER BY——结果序交由求解器稳定哈希排序，确定性不依赖
-- DB 返回序（Memory 实现的插入序语义同源）。
SELECT pack_id, item_version_id, item_id, template_version_id,
       objective, interaction_ref, lineage
FROM v_serving_item_version
WHERE pack_id = $1 AND objective->>'gradeband' = @gradeband::text;

-- name: QueueExposedItemVersionIDs :many
-- 静态轨：某 渠道×学科包×周队列 已曝光的题目版本集（跨期不重复；
-- ix_paper_exposure_queue 前缀命中）。
SELECT item_version_id FROM paper_exposure
WHERE channel = $1 AND subject_pack_id = $2 AND week_label = $3;

-- name: QueueExposedTemplateVersionIDs :many
-- 静态轨：同队列已曝光的母题版本集（同母题不同卷；IS NOT NULL 与冻结 SQL
-- 同义——无母题的裸题不计入母题互斥集）。
SELECT template_version_id FROM paper_exposure
WHERE channel = $1 AND subject_pack_id = $2 AND week_label = $3
  AND template_version_id IS NOT NULL;

-- name: StudentExposedItemVersionIDs :many
-- 在线轨：某学生（匿名 id，D7）已见过的题目版本集（跨期不重复；
-- ix_student_exposure_student 命中）。
SELECT item_version_id FROM student_exposure
WHERE student_alias_id = $1;

-- name: StudentExposedTemplateVersionIDs :many
-- 在线轨：某学生已见过的母题版本集（同母题不同卷；NULL 模板不计）。
SELECT template_version_id FROM student_exposure
WHERE student_alias_id = $1 AND template_version_id IS NOT NULL;

-- name: InsertPaperExposure :exec
-- 静态轨曝光预留入账（与 paper/paper_item 写入同一事务，序自由——FK 一致性
-- 由 0010 外键在语句级验证）。exposure_id 由应用侧发号（crypto/rand，与
-- estimator 的 run_id 同惯例）；created_at 取 DB 默认 now()。并发组卷的重复
-- 曝光由 uq_paper_exposure_queue_item UNIQUE 兜底（23505），应用层查询只是
-- 热路径优化。
INSERT INTO paper_exposure (
	exposure_id, channel, subject_pack_id, textbook_version, gradeband,
	week_label, item_version_id, template_version_id, paper_id
) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9);

-- name: InsertStudentExposure :exec
-- 在线轨曝光预留入账：把发给学生（匿名 id）的题登记到学生轨。uq_student_
-- exposure_student_item UNIQUE 兜底；purpose 值域由 ck_student_exposure_
-- purpose_domain CHECK 兜底。
INSERT INTO student_exposure (
	exposure_id, student_alias_id, item_version_id, template_version_id,
	paper_id, session_id, purpose
) VALUES ($1, $2, $3, $4, $5, $6, $7);
