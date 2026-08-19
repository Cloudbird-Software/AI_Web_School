-- T-W5-032: 由 alembic 0016（estimator_run.py）在线捕获生成（语义零变更，tools/sql/gen_migrations_from_alembic.py）；禁止手改。

DROP INDEX uq_estimator_run_one_active_per_scope;

DROP INDEX ix_estimator_run_purpose_scope;

DROP TABLE estimator_run;
