-- T-W5-017（SQL-2）：response_event 作答事件账的写入语句面（三本只增不改的账
-- 之一，D1；数据飞轮入水口 A3/A4）。表结构与 append-only 触发器由 0003 迁移
-- 承载，本文件只有 INSERT——UPDATE/DELETE 无查询面可写，DB 触发器物理兜底。
-- 事务纪律（S4/D11）：InsertResponseEvent 必须运行在调用方已 begin 的显式事务
-- 内（core/events.Writer 经 WithTx 绑定传入），提交/回滚由最外层调用方统一持有，
-- 领域服务不自 commit——作答事件与会话状态同进同退。

-- name: InsertResponseEvent :exec
-- 契约 §1 十三列逐字对应：身份（event_id/student_alias_id/item_version_id）
-- + 场景（scene，D5 三值 enum）+ 载荷（raw_payload/duration_ms 可空=NULL未知/
-- scoring_trace/error_inferences）+ 归属（testlet_id/session_id/audio_play_events
-- /source_ref）+ 分区键 created_at（PK=(event_id, created_at)，实现注记）。
INSERT INTO response_event (
	event_id, student_alias_id, item_version_id, scene,
	raw_payload, duration_ms, scoring_trace, error_inferences,
	testlet_id, session_id, audio_play_events, source_ref, created_at
) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13);
