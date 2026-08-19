-- T-W5-032: 由 alembic 0005（append_only_unify.py）在线捕获生成（语义零变更，tools/sql/gen_migrations_from_alembic.py）；禁止手改。
ALTER TABLE corpus_version DROP CONSTRAINT ck_cv_published_requires_gate_cert;
ALTER TABLE corpus_version DROP COLUMN retired_at;
ALTER TABLE corpus_version DROP COLUMN published_at;
ALTER TABLE corpus_version DROP COLUMN gate_certificate_id;
ALTER TABLE corpus_version ALTER COLUMN status DROP DEFAULT;

CREATE OR REPLACE FUNCTION raise_append_only_error() RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'gate table is append-only (D1): UPDATE/DELETE forbidden';
END;
$$ LANGUAGE plpgsql;
