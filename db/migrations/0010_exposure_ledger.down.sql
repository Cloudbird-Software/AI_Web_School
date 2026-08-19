-- T-W5-032: 由 alembic 0010（exposure_ledger.py）在线捕获生成（语义零变更，tools/sql/gen_migrations_from_alembic.py）；禁止手改。
DROP TRIGGER IF EXISTS trg_student_exposure_append_only ON student_exposure;
DROP TRIGGER IF EXISTS trg_paper_exposure_append_only ON paper_exposure;

DROP TABLE student_exposure;

DROP TABLE paper_exposure;
