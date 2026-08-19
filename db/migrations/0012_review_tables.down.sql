-- T-W5-032: 由 alembic 0012（review_tables.py）在线捕获生成（语义零变更，tools/sql/gen_migrations_from_alembic.py）；禁止手改。
DROP TRIGGER IF EXISTS trg_review_policy_append_only ON review_policy;

DROP TABLE review_queue_entry;

DROP TABLE review_policy;
DROP FUNCTION IF EXISTS raise_review_policy_append_only_error();
