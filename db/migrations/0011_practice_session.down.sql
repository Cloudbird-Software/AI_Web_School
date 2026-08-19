-- T-W5-032: 由 alembic 0011（practice_session.py）在线捕获生成（语义零变更，tools/sql/gen_migrations_from_alembic.py）；禁止手改。

DROP INDEX ix_practice_session_student;

DROP TABLE practice_session;
