-- T-W5-032: 由 alembic 0009（paper_trace.py）在线捕获生成（语义零变更，tools/sql/gen_migrations_from_alembic.py）；禁止手改。
DROP TRIGGER IF EXISTS trg_paper_item_append_only ON paper_item;
DROP TRIGGER IF EXISTS trg_paper_append_only ON paper;

DROP TABLE paper_item;

DROP TABLE paper;
DROP FUNCTION IF EXISTS raise_paper_append_only_error();
