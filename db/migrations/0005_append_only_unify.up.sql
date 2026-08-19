-- T-W5-032: 由 alembic 0005（append_only_unify.py）在线捕获生成（语义零变更，tools/sql/gen_migrations_from_alembic.py）；禁止手改。

CREATE OR REPLACE FUNCTION raise_append_only_error() RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'append-only table rejects UPDATE/DELETE';
END;
$$ LANGUAGE plpgsql;
ALTER TABLE corpus_version ALTER COLUMN status SET DEFAULT 'draft';
ALTER TABLE corpus_version ADD COLUMN gate_certificate_id TEXT;
ALTER TABLE corpus_version ADD COLUMN published_at TIMESTAMP WITH TIME ZONE;
ALTER TABLE corpus_version ADD COLUMN retired_at TIMESTAMP WITH TIME ZONE;
ALTER TABLE corpus_version ADD CONSTRAINT ck_cv_published_requires_gate_cert CHECK (published_at IS NULL OR gate_certificate_id IS NOT NULL);
