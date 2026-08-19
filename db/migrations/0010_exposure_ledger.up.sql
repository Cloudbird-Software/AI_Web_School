-- T-W5-032: 由 alembic 0010（exposure_ledger.py）在线捕获生成（语义零变更，tools/sql/gen_migrations_from_alembic.py）；禁止手改。

CREATE TABLE paper_exposure (
	exposure_id TEXT NOT NULL, 
	channel TEXT NOT NULL, 
	subject_pack_id TEXT NOT NULL, 
	textbook_version TEXT, 
	gradeband TEXT NOT NULL, 
	week_label TEXT NOT NULL, 
	item_version_id TEXT NOT NULL, 
	template_version_id TEXT, 
	paper_id TEXT, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (exposure_id), 
	CONSTRAINT fk_paper_exposure_item_version FOREIGN KEY(item_version_id) REFERENCES item_version (item_version_id) ON DELETE RESTRICT, 
	CONSTRAINT fk_paper_exposure_paper FOREIGN KEY(paper_id) REFERENCES paper (paper_id) ON DELETE RESTRICT, 
	CONSTRAINT uq_paper_exposure_queue_item UNIQUE (channel, subject_pack_id, week_label, item_version_id), 
	CONSTRAINT ck_paper_exposure_gradeband_domain CHECK (gradeband IN ('L', 'M', 'H'))
);

CREATE TABLE student_exposure (
	exposure_id TEXT NOT NULL, 
	student_alias_id TEXT NOT NULL, 
	item_version_id TEXT NOT NULL, 
	template_version_id TEXT, 
	paper_id TEXT, 
	session_id TEXT, 
	purpose TEXT NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (exposure_id), 
	CONSTRAINT fk_student_exposure_item_version FOREIGN KEY(item_version_id) REFERENCES item_version (item_version_id) ON DELETE RESTRICT, 
	CONSTRAINT fk_student_exposure_paper FOREIGN KEY(paper_id) REFERENCES paper (paper_id) ON DELETE RESTRICT, 
	CONSTRAINT uq_student_exposure_student_item UNIQUE (student_alias_id, item_version_id), 
	CONSTRAINT ck_student_exposure_purpose_domain CHECK (purpose IN ('practice', 'diagnosis', 'measurement'))
);
CREATE INDEX ix_paper_exposure_queue ON paper_exposure (channel, subject_pack_id, week_label);
CREATE INDEX ix_paper_exposure_template ON paper_exposure (template_version_id);
CREATE INDEX ix_student_exposure_student ON student_exposure (student_alias_id);
CREATE INDEX ix_student_exposure_template ON student_exposure (template_version_id);

CREATE TRIGGER trg_paper_exposure_append_only
    BEFORE UPDATE OR DELETE ON paper_exposure
    FOR EACH STATEMENT
    EXECUTE FUNCTION raise_append_only_error();

CREATE TRIGGER trg_student_exposure_append_only
    BEFORE UPDATE OR DELETE ON student_exposure
    FOR EACH STATEMENT
    EXECUTE FUNCTION raise_append_only_error();
