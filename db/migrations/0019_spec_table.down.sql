-- T-W5-032: 由 alembic 0019（spec_table.py）在线捕获生成（语义零变更，tools/sql/gen_migrations_from_alembic.py）；禁止手改。
DROP TRIGGER IF EXISTS trg_spec_table_append_only ON spec_table;
DROP FUNCTION IF EXISTS raise_spec_table_append_only_error();

DROP TABLE spec_table;
