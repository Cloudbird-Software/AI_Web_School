-- T-W5-032: 由 alembic 0016（estimator_run.py）在线捕获生成（语义零变更，tools/sql/gen_migrations_from_alembic.py）；禁止手改。

CREATE TABLE estimator_run (
	run_id TEXT NOT NULL, 
	purpose_scope TEXT NOT NULL, 
	model_version TEXT NOT NULL, 
	code_digest TEXT NOT NULL, 
	input_snapshot_id TEXT NOT NULL, 
	graph_release_id TEXT NOT NULL, 
	activated_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	retired_at TIMESTAMP WITH TIME ZONE, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (run_id), 
	CONSTRAINT ck_estimator_run_purpose_scope_domain CHECK (purpose_scope IN ('practice', 'diagnosis', 'measurement')), 
	CONSTRAINT uq_estimator_run_identity UNIQUE (purpose_scope, model_version, activated_at)
);
CREATE INDEX ix_estimator_run_purpose_scope ON estimator_run (purpose_scope);
CREATE UNIQUE INDEX uq_estimator_run_one_active_per_scope ON estimator_run (purpose_scope) WHERE retired_at IS NULL;
