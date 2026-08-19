-- T-W5-032: 由 alembic 0022（shadow_score.py）在线捕获生成（语义零变更，tools/sql/gen_migrations_from_alembic.py）；禁止手改。

CREATE TABLE shadow_score (
	shadow_id TEXT NOT NULL, 
	dataset_id TEXT NOT NULL, 
	case_id TEXT NOT NULL, 
	rubric_id TEXT NOT NULL, 
	grade_band TEXT NOT NULL, 
	writing_type TEXT NOT NULL, 
	response_text TEXT, 
	response_text_digest TEXT NOT NULL, 
	ai_score_payload JSONB NOT NULL, 
	overall_confidence NUMERIC(4, 3) NOT NULL, 
	needs_human_review BOOLEAN NOT NULL, 
	human_score_payload JSONB, 
	consistency_status TEXT DEFAULT 'pending' NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (shadow_id), 
	CONSTRAINT ck_shadow_score_grade_band_domain CHECK (grade_band IN ('L', 'M', 'H')), 
	CONSTRAINT ck_shadow_score_writing_type_domain CHECK (writing_type IN ('composition', 'picture_writing')), 
	CONSTRAINT ck_shadow_score_consistency_status_domain CHECK (consistency_status IN ('pending', 'consistent', 'inconsistent')), 
	CONSTRAINT ck_shadow_score_overall_confidence_range CHECK (overall_confidence >= 0 AND overall_confidence <= 1)
);
CREATE INDEX ix_shadow_score_dataset_id ON shadow_score (dataset_id);
CREATE INDEX ix_shadow_score_grade_band ON shadow_score (grade_band);
CREATE INDEX ix_shadow_score_consistency_status ON shadow_score (consistency_status);
CREATE INDEX ix_shadow_score_rubric_id ON shadow_score (rubric_id);
CREATE TRIGGER trg_shadow_score_append_only
    BEFORE UPDATE OR DELETE ON shadow_score
    FOR EACH STATEMENT
    EXECUTE FUNCTION raise_append_only_error();
