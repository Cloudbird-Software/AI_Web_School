-- T-W5-032: 由 alembic 0017（score_run.py）在线捕获生成（语义零变更，tools/sql/gen_migrations_from_alembic.py）；禁止手改。
DROP TRIGGER IF EXISTS trg_score_run_append_only ON score_run;

DROP INDEX ix_score_run_rerun_of;

DROP INDEX ix_score_run_scorer_version;

DROP INDEX ix_score_run_purpose_scope;

DROP INDEX ix_score_run_event;

DROP TABLE score_run;
