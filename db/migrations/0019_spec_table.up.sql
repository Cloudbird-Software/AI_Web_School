-- T-W5-032: 由 alembic 0019（spec_table.py）在线捕获生成（语义零变更，tools/sql/gen_migrations_from_alembic.py）；禁止手改。

CREATE TABLE spec_table (
	spec_table_id TEXT NOT NULL, 
	spec_table_version TEXT NOT NULL, 
	gradeband TEXT NOT NULL, 
	graph_release TEXT NOT NULL, 
	cells JSONB NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	created_by TEXT NOT NULL, 
	CONSTRAINT pk_spec_table PRIMARY KEY (spec_table_id, spec_table_version), 
	CONSTRAINT ck_spec_table_gradeband_domain CHECK (gradeband IN ('L', 'M', 'H')), 
	CONSTRAINT ck_spec_table_cells_is_array CHECK (jsonb_typeof(cells) = 'array')
);
CREATE INDEX ix_spec_table_gradeband ON spec_table (gradeband);
CREATE INDEX ix_spec_table_graph_release ON spec_table (graph_release);

CREATE OR REPLACE FUNCTION raise_spec_table_append_only_error() RETURNS TRIGGER AS $$
BEGIN
    -- D1 细目表版本账只增不改：改表 = INSERT 新版本行，禁止 UPDATE/DELETE。
    RAISE EXCEPTION 'spec_table is append-only (D1): UPDATE/DELETE forbidden';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_spec_table_append_only
    BEFORE UPDATE OR DELETE ON spec_table
    FOR EACH STATEMENT
    EXECUTE FUNCTION raise_spec_table_append_only_error();
