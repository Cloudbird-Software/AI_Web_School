-- T-W5-032: 由 alembic 0001（initial_placeholder_tables.py）在线捕获生成（语义零变更，tools/sql/gen_migrations_from_alembic.py）；禁止手改。

DROP TABLE response_event;

DROP TABLE gate_certificate;

DROP TABLE item;
