-- T-W5-032: 由 alembic 0017（score_run.py）在线捕获生成（语义零变更，tools/sql/gen_migrations_from_alembic.py）；禁止手改。

CREATE TABLE score_run (
	score_run_id TEXT NOT NULL, 
	event_id UUID NOT NULL, 
	event_created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	rerun_of TEXT, 
	purpose_scope TEXT NOT NULL, 
	scorer_id TEXT NOT NULL, 
	scorer_version TEXT NOT NULL, 
	original_scorer_version TEXT NOT NULL, 
	dimension_scores JSONB NOT NULL, 
	scoring_trace JSONB NOT NULL, 
	error_inferences JSONB NOT NULL, 
	correct BOOLEAN NOT NULL, 
	run_label TEXT, 
	input_snapshot_id TEXT NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (score_run_id), 
	CONSTRAINT fk_score_run_response_event FOREIGN KEY(event_id, event_created_at) REFERENCES response_event (event_id, created_at) ON DELETE RESTRICT, 
	CONSTRAINT uq_score_run_identity UNIQUE (event_id, event_created_at, run_label), 
	CONSTRAINT ck_score_run_purpose_scope_domain CHECK (purpose_scope IN ('practice', 'diagnosis', 'measurement'))
);
CREATE INDEX ix_score_run_event ON score_run (event_id, event_created_at);
CREATE INDEX ix_score_run_purpose_scope ON score_run (purpose_scope);
CREATE INDEX ix_score_run_scorer_version ON score_run (scorer_version);
CREATE INDEX ix_score_run_rerun_of ON score_run (rerun_of);
CREATE TRIGGER trg_score_run_append_only
    BEFORE UPDATE OR DELETE ON score_run
    FOR EACH STATEMENT
    EXECUTE FUNCTION raise_append_only_error();
