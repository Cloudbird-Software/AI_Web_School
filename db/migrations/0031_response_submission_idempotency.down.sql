-- T-W5-018: 由 alembic 0031（response_submission_idempotency.py）downgrade 镜像；
-- 禁止手改语义。逆序还原 0031.up：幂等登记账整表移除（全加性迁移的可逆面）。
DROP TABLE response_submission;
