-- T-W5-032: 由 alembic 0022（shadow_score.py）在线捕获生成（语义零变更，tools/sql/gen_migrations_from_alembic.py）；禁止手改。
DROP TRIGGER IF EXISTS trg_shadow_score_append_only ON shadow_score;

DROP INDEX ix_shadow_score_rubric_id;

DROP INDEX ix_shadow_score_consistency_status;

DROP INDEX ix_shadow_score_grade_band;

DROP INDEX ix_shadow_score_dataset_id;

DROP TABLE shadow_score;
