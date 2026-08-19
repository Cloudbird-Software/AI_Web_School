-- T-W5-032: 由 alembic 0007（kp_closure_graph_release.py）在线捕获生成（语义零变更，tools/sql/gen_migrations_from_alembic.py）；禁止手改。

DROP INDEX ix_kpc_release_src;

DROP TABLE kp_closure;
ALTER TABLE graph_release DROP CONSTRAINT fk_graph_release_superseded_by;

DROP TABLE graph_release;
DROP TYPE IF EXISTS graph_release_status_enum;
