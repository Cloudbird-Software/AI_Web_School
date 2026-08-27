-- T-W5-011（家长授权账版本原子性与并发安全）：授权版本唯一性防线 + 留痕主体字段。
-- 语句为 alembic 镜像 0027（consent_active_uniqueness.py）upgrade 的原句，
-- 语义 parity 由 make migrate-go-check 在 CI 复核（本机无 Docker/PG，未在线捕获）。
--
-- 唯一性事实核验（双源已读：alembic/versions/0015_parental_consent.py 与
-- db/migrations/0015_parental_consent.up.sql）：0015 只建立了非唯一索引
--   ix_parental_consent_student_purpose_version ON parental_consent
--     (student_alias_id, ((scope ->> 'purpose')), version)
-- 查询加速有之，约束语义全无——MAX(version)+1 读后写分配下，并发 grant/revoke
-- 可产生同版本号双行，check_consent 取到哪条不确定（任务卡目标陈述的合规账
-- 不确定性）。本迁移把该索引升级为同一列集的唯一索引：
--   uq_parental_consent_version_per_purpose ON parental_consent
--     (student_alias_id, ((scope ->> 'purpose')), version)
-- 同一列集只保留一份索引（先 DROP 非唯一再建唯一），不重复建第二份——重复索引
-- 只增加写放大而不增强不变量（同 T-W5-019 的事实修正纪律）。版本号自此在
-- (student_alias_id, purpose) 链内全局无重，最新版本恰一行 → check_consent
-- 「永远取最新版本」从约定升级为 DB 强制；应用层并发分配由 core/compliance 的
-- per-chain advisory xact lock 承担，本索引是最后一道防线（23505 → 明确错误）。
--
-- 留痕维度：parental_consent 新增 recorded_by TEXT NOT NULL DEFAULT 'system'，
-- 记录授权事件登记主体（家长操作/客服代录/系统），配合既有 created_at 与
-- 单调 version 可完整还原「谁在何时把 (student, purpose) 授权链从版本 A
-- 推进到版本 B」（append-only 时间线，与 T-W5-019 对 estimator_run 补
-- activated_by 同构）。
--
-- 为什么用「带 DEFAULT 的单条 ADD COLUMN NOT NULL」而不做 UPDATE 回填：
-- 本表被 trg_parental_consent_append_only 触发器物理禁止一切 UPDATE/DELETE，
-- estimator_run 式「加可空列→UPDATE 回填→SET NOT NULL」三步在这里第一步 UPDATE
-- 就会被触发器拒绝。ADD COLUMN ... NOT NULL DEFAULT 'system' 由 PG 以 fast default
-- 直接物化存量行默认值，不发任何 UPDATE 语句，触发器无从违反；新写入由应用层
-- 必须显式传 who（core/compliance 输入结构），空缺回落列默认 'system'。

ALTER TABLE parental_consent ADD COLUMN recorded_by TEXT DEFAULT 'system' NOT NULL;

DROP INDEX ix_parental_consent_student_purpose_version;

CREATE UNIQUE INDEX uq_parental_consent_version_per_purpose ON parental_consent (student_alias_id, ((scope ->> 'purpose')), version);
