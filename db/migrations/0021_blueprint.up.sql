-- T-W5-032: 由 alembic 0021（blueprint.py）在线捕获生成（语义零变更，tools/sql/gen_migrations_from_alembic.py）；禁止手改。

CREATE TABLE rubric_template (
	rubric_id TEXT NOT NULL, 
	name TEXT NOT NULL, 
	grade_band TEXT NOT NULL, 
	version TEXT NOT NULL, 
	payload JSONB NOT NULL, 
	total_max_score NUMERIC(6, 2) NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (rubric_id), 
	CONSTRAINT ck_rubric_template_grade_band_domain CHECK (grade_band IN ('L', 'M', 'H'))
);

CREATE TABLE blueprint (
	blueprint_id TEXT NOT NULL, 
	writing_type TEXT NOT NULL, 
	pack_id TEXT NOT NULL, 
	template_version_id TEXT NOT NULL, 
	rubric_template_id TEXT NOT NULL, 
	payload JSONB NOT NULL, 
	version TEXT NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (blueprint_id), 
	CONSTRAINT ck_blueprint_writing_type_domain CHECK (writing_type IN ('composition', 'picture_writing')), 
	CONSTRAINT fk_blueprint_rubric_template FOREIGN KEY(rubric_template_id) REFERENCES rubric_template (rubric_id) ON DELETE RESTRICT
);
CREATE INDEX ix_rubric_template_grade_band ON rubric_template (grade_band);
CREATE INDEX ix_blueprint_writing_type ON blueprint (writing_type);
CREATE INDEX ix_blueprint_pack_id ON blueprint (pack_id);
CREATE INDEX ix_blueprint_rubric_template_id ON blueprint (rubric_template_id);
CREATE TRIGGER trg_rubric_template_append_only
    BEFORE UPDATE OR DELETE ON rubric_template
    FOR EACH STATEMENT
    EXECUTE FUNCTION raise_append_only_error();

CREATE TRIGGER trg_blueprint_append_only
    BEFORE UPDATE OR DELETE ON blueprint
    FOR EACH STATEMENT
    EXECUTE FUNCTION raise_append_only_error();
