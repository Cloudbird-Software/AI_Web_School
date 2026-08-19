-- T-W5-032: 由 alembic 0004（gate_tables.py）在线捕获生成（语义零变更，tools/sql/gen_migrations_from_alembic.py）；禁止手改。
DROP TRIGGER IF EXISTS trg_gate_certificate_append_only ON gate_certificate;
DROP TRIGGER IF EXISTS trg_gate_run_append_only ON gate_run;
DROP TRIGGER IF EXISTS trg_gate_verdict_append_only ON gate_verdict;

DROP TABLE gate_verdict;

DROP TABLE gate_run;

DROP TABLE gate_certificate;
DROP TYPE IF EXISTS gate_run_verdict_enum;

CREATE TABLE gate_certificate (
	id BIGINT GENERATED ALWAYS AS IDENTITY, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id)
);
