-- T-W5-018（作答提交幂等与并发安全）：作答提交幂等键防线（response_submission 登记账）。
-- 语句为 alembic 镜像 0031（response_submission_idempotency.py）upgrade 的原句，
-- 语义 parity 由 make migrate-go-check 在 CI 复核（本机无 Docker/PG，未在线捕获）。
--
-- 幂等语义（board 验收）：同一 (session, item, 作答指纹) 重复提交 → 幂等成功
-- 返回原事件 id、不重复落账；并发提交 → 恰一条 response_event 入账、
-- current_index 恰推进 1。并发临界区由 core/session 的 per-session advisory
-- xact lock + 会话行 FOR UPDATE 承担（首次提交无登记行可锁，advisory 锁在
-- 空账场景同样串行化「查重→入账→登记→推进」——core/estimator / core/compliance
-- 同一惯例）；本表复合主键 pk_response_submission (session_id, item_version_id,
-- answer_digest) 是最后一道防线：绕过应用锁的重复写入在 23505 处被明确拒绝。
--
-- 为什么是独立登记账而不是给 response_event 补部分唯一索引：response_event 是
-- 按 created_at RANGE 分区的事件账（0003，D1 append-only），PostgreSQL 对分区
-- 表的唯一索引强制包含全部分区键——created_at 入键即每事件各占一行，幂等判定
-- 失效；「不含分区键的部分唯一索引」在物理上不可建。幂等键因此落在非分区的
-- 登记账上：answer_digest 为作答提交指纹（core/gate/validators.ContentDigest
-- 规范化摘要口径，键序/空白不敏感，形如 sha256:<64hex>，CHECK 物理锚定）；
-- event_id + event_created_at 复合回指 response_event 主键（0003 实现注记：
-- 分区表事件以 (event_id, created_at) 复合键引用）——幂等重放从登记账取回
-- 原事件 id，「重复提交返回首次结果」的取数面。
--
-- 全加性：不改 response_event 一列一索引（冻结契约 §1 十三列零漂移）；
-- down 侧逆序完全还原（可逆性：make migrate-check down→up 全绿）。

CREATE TABLE response_submission (
	session_id UUID NOT NULL, 
	item_version_id TEXT NOT NULL, 
	answer_digest TEXT NOT NULL, 
	event_id UUID NOT NULL, 
	event_created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	CONSTRAINT pk_response_submission PRIMARY KEY (session_id, item_version_id, answer_digest), 
	CONSTRAINT fk_response_submission_session FOREIGN KEY(session_id) REFERENCES practice_session (session_id) ON DELETE RESTRICT, 
	CONSTRAINT fk_response_submission_event FOREIGN KEY(event_id, event_created_at) REFERENCES response_event (event_id, created_at) ON DELETE RESTRICT, 
	CONSTRAINT ck_response_submission_digest_shape CHECK (answer_digest ~ '^sha256:[0-9a-f]{64}$')
);
