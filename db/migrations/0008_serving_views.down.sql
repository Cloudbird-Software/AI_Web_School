-- T-W5-032: 由 alembic 0008（serving_views.py）在线捕获生成（语义零变更，tools/sql/gen_migrations_from_alembic.py）；禁止手改。
DROP VIEW IF EXISTS v_serving_corpus_version;
DROP VIEW IF EXISTS v_serving_material_version;
DROP VIEW IF EXISTS v_serving_item_version;
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM serving_reader;
