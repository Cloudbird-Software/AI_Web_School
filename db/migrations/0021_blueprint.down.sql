-- T-W5-032: 由 alembic 0021（blueprint.py）在线捕获生成（语义零变更，tools/sql/gen_migrations_from_alembic.py）；禁止手改。
DROP TRIGGER IF EXISTS trg_blueprint_append_only ON blueprint;
DROP TRIGGER IF EXISTS trg_rubric_template_append_only ON rubric_template;

DROP INDEX ix_blueprint_rubric_template_id;

DROP INDEX ix_blueprint_pack_id;

DROP INDEX ix_blueprint_writing_type;

DROP INDEX ix_rubric_template_grade_band;

DROP TABLE blueprint;

DROP TABLE rubric_template;
