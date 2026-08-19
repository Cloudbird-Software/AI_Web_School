-- T-W5-032: 由 alembic 0009（paper_trace.py）在线捕获生成（语义零变更，tools/sql/gen_migrations_from_alembic.py）；禁止手改。

CREATE OR REPLACE FUNCTION raise_paper_append_only_error() RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'paper table is append-only: UPDATE/DELETE forbidden';
END;
$$ LANGUAGE plpgsql;

CREATE TABLE paper (
	paper_id TEXT NOT NULL, 
	paper_code TEXT NOT NULL, 
	paper_spec_id TEXT NOT NULL, 
	paper_title TEXT NOT NULL, 
	gradeband TEXT NOT NULL, 
	subject_pack_id TEXT NOT NULL, 
	weekly_batch_id TEXT, 
	kp_snapshot_ref TEXT NOT NULL, 
	seed BIGINT NOT NULL, 
	rendered_snapshot_path TEXT, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	created_by TEXT NOT NULL, 
	PRIMARY KEY (paper_id), 
	CONSTRAINT uq_paper_code UNIQUE (paper_code), 
	CONSTRAINT uq_paper_spec_id UNIQUE (paper_spec_id), 
	CONSTRAINT ck_paper_gradeband_domain CHECK (gradeband IN ('L', 'M', 'H')), 
	CONSTRAINT ck_paper_subject_pack_domain CHECK (subject_pack_id IN ('subject-math', 'subject-chinese', 'subject-english'))
);

CREATE TABLE paper_item (
	paper_item_id TEXT NOT NULL, 
	paper_id TEXT NOT NULL, 
	item_version_id TEXT NOT NULL, 
	placement_token TEXT NOT NULL, 
	item_number INTEGER NOT NULL, 
	item_short_code TEXT NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (paper_item_id), 
	CONSTRAINT fk_paper_item_paper FOREIGN KEY(paper_id) REFERENCES paper (paper_id) ON DELETE RESTRICT, 
	CONSTRAINT fk_paper_item_item_version FOREIGN KEY(item_version_id) REFERENCES item_version (item_version_id) ON DELETE RESTRICT, 
	CONSTRAINT uq_paper_item_short_code UNIQUE (item_short_code), 
	CONSTRAINT uq_paper_item_paper_placement UNIQUE (paper_id, placement_token), 
	CONSTRAINT ck_paper_item_number_positive CHECK (item_number > 0)
);
CREATE INDEX ix_paper_item_paper_id ON paper_item (paper_id);
CREATE INDEX ix_paper_paper_spec_id ON paper (paper_spec_id);
CREATE INDEX ix_paper_weekly_batch_id ON paper (weekly_batch_id);

CREATE TRIGGER trg_paper_append_only
    BEFORE UPDATE OR DELETE ON paper
    FOR EACH STATEMENT
    EXECUTE FUNCTION raise_paper_append_only_error();

CREATE TRIGGER trg_paper_item_append_only
    BEFORE UPDATE OR DELETE ON paper_item
    FOR EACH STATEMENT
    EXECUTE FUNCTION raise_paper_append_only_error();
