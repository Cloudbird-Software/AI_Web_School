-- T-W5-032: 由 alembic 0011（practice_session.py）在线捕获生成（语义零变更，tools/sql/gen_migrations_from_alembic.py）；禁止手改。

CREATE TABLE practice_session (
	session_id UUID NOT NULL, 
	student_alias_id UUID NOT NULL, 
	scene TEXT NOT NULL, 
	gradeband TEXT NOT NULL, 
	status TEXT DEFAULT 'active' NOT NULL, 
	paper_id TEXT, 
	item_sequence JSONB NOT NULL, 
	current_index INTEGER DEFAULT '0' NOT NULL, 
	retest_wrong BOOLEAN DEFAULT false NOT NULL, 
	wrong_marks JSONB DEFAULT '[]'::jsonb NOT NULL, 
	time_limit_sec INTEGER NOT NULL, 
	answered_count INTEGER DEFAULT '0' NOT NULL, 
	correct_count INTEGER DEFAULT '0' NOT NULL, 
	started_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	last_resume_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	last_activity_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	completed_at TIMESTAMP WITH TIME ZONE, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (session_id), 
	CONSTRAINT fk_practice_session_paper FOREIGN KEY(paper_id) REFERENCES paper (paper_id) ON DELETE RESTRICT, 
	CONSTRAINT ck_practice_session_scene_domain CHECK (scene IN ('practice', 'diagnosis')), 
	CONSTRAINT ck_practice_session_gradeband_domain CHECK (gradeband IN ('L', 'M', 'H')), 
	CONSTRAINT ck_practice_session_status_domain CHECK (status IN ('active', 'rest_prompted', 'completed', 'abandoned')), 
	CONSTRAINT ck_practice_session_current_index_nonneg CHECK (current_index >= 0), 
	CONSTRAINT ck_practice_session_time_limit_positive CHECK (time_limit_sec > 0)
);
CREATE INDEX ix_practice_session_student ON practice_session (student_alias_id);
