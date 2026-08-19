-- T-W5-032: 由 alembic 0018（item_lifecycle.py）在线捕获生成（语义零变更，tools/sql/gen_migrations_from_alembic.py）；禁止手改。
CREATE TYPE item_lifecycle_state_enum AS ENUM ('ACTIVE', 'WATCH', 'QUARANTINED', 'RETIRED');

CREATE TABLE item_lifecycle_transition (
	transition_id TEXT NOT NULL, 
	item_id TEXT NOT NULL, 
	from_state item_lifecycle_state_enum, 
	to_state item_lifecycle_state_enum NOT NULL, 
	gate_certificate_id TEXT, 
	reason TEXT, 
	health_score NUMERIC(4, 3), 
	anomaly_tags JSONB, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (transition_id), 
	CONSTRAINT fk_ilt_item FOREIGN KEY(item_id) REFERENCES item (item_id) ON DELETE RESTRICT, 
	CONSTRAINT ck_ilt_health_score_domain CHECK (health_score IS NULL OR (health_score >= 0 AND health_score <= 1))
);
CREATE INDEX ix_item_lifecycle_item_created ON item_lifecycle_transition (item_id, created_at);
CREATE INDEX ix_item_lifecycle_to_state ON item_lifecycle_transition (to_state);

CREATE OR REPLACE FUNCTION raise_lifecycle_append_only_error() RETURNS TRIGGER AS $$
BEGIN
    -- D1 生命周期账只增不改：状态变更走 INSERT 新行，禁止 UPDATE/DELETE。
    RAISE EXCEPTION 'item_lifecycle_transition is append-only (D1): UPDATE/DELETE forbidden';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_item_lifecycle_append_only
    BEFORE UPDATE OR DELETE ON item_lifecycle_transition
    FOR EACH STATEMENT
    EXECUTE FUNCTION raise_lifecycle_append_only_error();
