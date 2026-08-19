-- T-W5-032: 由 alembic 0013（item_param.py）在线捕获生成（语义零变更，tools/sql/gen_migrations_from_alembic.py）；禁止手改。

CREATE TABLE item_param (
	param_id TEXT NOT NULL, 
	item_version_id TEXT NOT NULL, 
	purpose_scope TEXT NOT NULL, 
	source TEXT NOT NULL, 
	params JSONB NOT NULL, 
	sample_size INTEGER NOT NULL, 
	method_version TEXT NOT NULL, 
	as_of TIMESTAMP WITH TIME ZONE NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (param_id), 
	CONSTRAINT uq_item_param_identity UNIQUE (item_version_id, purpose_scope, source, method_version, as_of), 
	CONSTRAINT ck_item_param_purpose_scope_domain CHECK (purpose_scope IN ('practice', 'diagnosis', 'measurement')), 
	CONSTRAINT ck_item_param_source_domain CHECK (source ~ '^(prior_rule|prior_expert|measured_.+)$'), 
	CONSTRAINT ck_item_param_sample_size_nonneg CHECK (sample_size >= 0), 
	CONSTRAINT fk_item_param_item_version FOREIGN KEY(item_version_id) REFERENCES item_version (item_version_id) ON DELETE RESTRICT
);
CREATE INDEX ix_item_param_item_version_id ON item_param (item_version_id);
CREATE INDEX ix_item_param_purpose_scope ON item_param (purpose_scope);

CREATE TRIGGER trg_item_param_append_only
    BEFORE UPDATE OR DELETE ON item_param
    FOR EACH STATEMENT
    EXECUTE FUNCTION raise_append_only_error();
