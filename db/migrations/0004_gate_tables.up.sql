-- T-W5-032: 由 alembic 0004（gate_tables.py）在线捕获生成（语义零变更，tools/sql/gen_migrations_from_alembic.py）；禁止手改。
CREATE TYPE gate_run_verdict_enum AS ENUM ('pass', 'fail', 'review');

DROP TABLE gate_certificate;

CREATE TABLE gate_certificate (
	cert_id TEXT NOT NULL, 
	artifact_ref TEXT NOT NULL, 
	cert_type TEXT NOT NULL, 
	policy_version TEXT NOT NULL, 
	issued_by TEXT NOT NULL, 
	issued_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (cert_id), 
	CONSTRAINT ck_gc_cert_type_domain CHECK (cert_type IN ('publish', 'retire'))
);

CREATE TABLE gate_run (
	run_id TEXT NOT NULL, 
	certificate_id TEXT NOT NULL, 
	policy_version TEXT NOT NULL, 
	validator_id TEXT NOT NULL, 
	validator_version TEXT NOT NULL, 
	verdict gate_run_verdict_enum NOT NULL, 
	evidence JSONB NOT NULL, 
	confidence NUMERIC(4, 3) NOT NULL, 
	cost_ms INTEGER NOT NULL, 
	cost_tokens INTEGER NOT NULL, 
	run_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (run_id), 
	CONSTRAINT fk_gr_certificate FOREIGN KEY(certificate_id) REFERENCES gate_certificate (cert_id), 
	CONSTRAINT ck_gr_confidence_range CHECK (confidence >= 0 AND confidence <= 1), 
	CONSTRAINT ck_gr_cost_nonneg CHECK (cost_ms >= 0 AND cost_tokens >= 0)
);

CREATE TABLE gate_verdict (
	verdict_id BIGINT GENERATED ALWAYS AS IDENTITY, 
	run_id TEXT NOT NULL, 
	detail JSONB NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (verdict_id), 
	CONSTRAINT fk_gv_run FOREIGN KEY(run_id) REFERENCES gate_run (run_id)
);

CREATE OR REPLACE FUNCTION raise_append_only_error() RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'gate table is append-only (D1): UPDATE/DELETE forbidden';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_gate_certificate_append_only
    BEFORE UPDATE OR DELETE ON gate_certificate
    FOR EACH STATEMENT
    EXECUTE FUNCTION raise_append_only_error();

CREATE TRIGGER trg_gate_run_append_only
    BEFORE UPDATE OR DELETE ON gate_run
    FOR EACH STATEMENT
    EXECUTE FUNCTION raise_append_only_error();

CREATE TRIGGER trg_gate_verdict_append_only
    BEFORE UPDATE OR DELETE ON gate_verdict
    FOR EACH STATEMENT
    EXECUTE FUNCTION raise_append_only_error();
