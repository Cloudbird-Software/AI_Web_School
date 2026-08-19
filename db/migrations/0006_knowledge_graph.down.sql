-- T-W5-032: 由 alembic 0006（knowledge_graph.py）在线捕获生成（语义零变更，tools/sql/gen_migrations_from_alembic.py）；禁止手改。

DROP TABLE kp_edge;
ALTER TABLE kp_node DROP CONSTRAINT fk_kp_node_supersedes;

DROP TABLE kp_node;

DROP TABLE relation_type;
DROP TYPE IF EXISTS kp_node_status_enum;
