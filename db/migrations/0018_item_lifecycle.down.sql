-- T-W5-032: 由 alembic 0018（item_lifecycle.py）在线捕获生成（语义零变更，tools/sql/gen_migrations_from_alembic.py）；禁止手改。
DROP TRIGGER IF EXISTS trg_item_lifecycle_append_only ON item_lifecycle_transition;
DROP FUNCTION IF EXISTS raise_lifecycle_append_only_error();

DROP TABLE item_lifecycle_transition;
DROP TYPE IF EXISTS item_lifecycle_state_enum;
