-- 镜像 alembic 0027 downgrade：对称回收本迁移引入的防线与留痕维度。
-- 唯一索引退回 0015 原名原形的非唯一索引（查询语义不变、约束语义放宽）；
-- recorded_by 列整列删除（append-only 触发器只禁 UPDATE/DELETE，不禁 DDL，
-- DROP COLUMN 无触发器违例面，与 upgrade 的 fast default 论证同源）。

DROP INDEX IF EXISTS uq_parental_consent_version_per_purpose;

CREATE INDEX IF NOT EXISTS ix_parental_consent_student_purpose_version ON parental_consent (student_alias_id, ((scope ->> 'purpose')), version);

ALTER TABLE parental_consent DROP COLUMN IF EXISTS recorded_by;
