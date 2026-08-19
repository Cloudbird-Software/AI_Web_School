-- T-W5-032: 由 alembic 0013（item_param.py）在线捕获生成（语义零变更，tools/sql/gen_migrations_from_alembic.py）；禁止手改。
DROP TRIGGER IF EXISTS trg_item_param_append_only ON item_param;

DROP TABLE item_param;
