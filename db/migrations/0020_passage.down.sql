-- T-W5-032: 由 alembic 0020（passage.py）在线捕获生成（语义零变更，tools/sql/gen_migrations_from_alembic.py）；禁止手改。

DROP INDEX ix_passage_subject_grade_band;

DROP INDEX ix_passage_content_hash;

DROP TABLE passage;
