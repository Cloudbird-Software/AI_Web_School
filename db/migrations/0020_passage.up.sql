-- T-W5-032: 由 alembic 0020（passage.py）在线捕获生成（语义零变更，tools/sql/gen_migrations_from_alembic.py）；禁止手改。

CREATE TABLE passage (
	passage_id TEXT NOT NULL, 
	content_hash TEXT NOT NULL, 
	body TEXT NOT NULL, 
	genre TEXT NOT NULL, 
	kp_refs JSONB NOT NULL, 
	difficulty_metrics JSONB NOT NULL, 
	license_id TEXT, 
	grade_band TEXT NOT NULL, 
	subject TEXT NOT NULL, 
	status TEXT DEFAULT 'draft' NOT NULL, 
	gate_certificate_id TEXT, 
	published_at TIMESTAMP WITH TIME ZONE, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (passage_id), 
	CONSTRAINT ck_passage_published_requires_gate CHECK (status <> 'published' OR gate_certificate_id IS NOT NULL), 
	CONSTRAINT ck_passage_genre_domain CHECK (genre IN ('narrative','expository','argumentative','poetry','fable','fairy_tale','dialogue','news_report','letter','diary')), 
	CONSTRAINT ck_passage_grade_band_domain CHECK (grade_band IN ('L','M','H')), 
	CONSTRAINT ck_passage_status_domain CHECK (status IN ('draft','quarantined','published','retired')), 
	CONSTRAINT fk_passage_license FOREIGN KEY(license_id) REFERENCES material_license (license_id)
);
CREATE INDEX ix_passage_content_hash ON passage (content_hash);
CREATE INDEX ix_passage_subject_grade_band ON passage (subject, grade_band);
