-- T-W5-032: 由 alembic 0012（review_tables.py）在线捕获生成（语义零变更，tools/sql/gen_migrations_from_alembic.py）；禁止手改。

CREATE OR REPLACE FUNCTION raise_review_policy_append_only_error() RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'review_policy table is append-only: UPDATE/DELETE forbidden';
END;
$$ LANGUAGE plpgsql;

CREATE TABLE review_policy (
	policy_id TEXT NOT NULL, 
	policy_version TEXT NOT NULL, 
	intervals_days JSONB NOT NULL, 
	description TEXT, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	CONSTRAINT pk_review_policy PRIMARY KEY (policy_id, policy_version), 
	CONSTRAINT ck_review_policy_intervals_nonempty_array CHECK (jsonb_typeof(intervals_days) = 'array' AND jsonb_array_length(intervals_days) > 0)
);

CREATE TABLE review_queue_entry (
	entry_id UUID NOT NULL, 
	student_alias_id UUID NOT NULL, 
	item_version_id TEXT NOT NULL, 
	policy_id TEXT NOT NULL, 
	policy_version TEXT NOT NULL, 
	stage INTEGER NOT NULL, 
	status TEXT NOT NULL, 
	source_error_type_id TEXT, 
	last_event_id UUID, 
	enqueued_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	due_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (entry_id), 
	CONSTRAINT fk_review_queue_entry_policy FOREIGN KEY(policy_id, policy_version) REFERENCES review_policy (policy_id, policy_version) ON DELETE RESTRICT, 
	CONSTRAINT uq_review_queue_entry_student_item_policy UNIQUE (student_alias_id, item_version_id, policy_id, policy_version), 
	CONSTRAINT ck_review_queue_entry_status_domain CHECK (status IN ('pending', 'done')), 
	CONSTRAINT ck_review_queue_entry_stage_nonnegative CHECK (stage >= 0)
);
CREATE INDEX ix_review_queue_entry_due ON review_queue_entry (student_alias_id, status, due_at);

CREATE TRIGGER trg_review_policy_append_only
    BEFORE UPDATE OR DELETE ON review_policy
    FOR EACH STATEMENT
    EXECUTE FUNCTION raise_review_policy_append_only_error();
INSERT INTO review_policy (policy_id, policy_version, intervals_days, description) VALUES ('fixed-interval'::VARCHAR, '1.0.0'::VARCHAR, CAST('[1, 3, 7, 21]'::VARCHAR AS jsonb), 'W3 S6 复习排程 v1：固定间隔表 1/3/7/21 天（架构 §4.4）'::VARCHAR);
