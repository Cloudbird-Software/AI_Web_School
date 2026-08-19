-- T-W5-032: 由 alembic 0003（response_event.py）在线捕获生成（语义零变更，tools/sql/gen_migrations_from_alembic.py）；禁止手改。
CREATE TYPE response_event_scene_enum AS ENUM ('practice', 'diagnosis', 'measurement');

DROP TABLE response_event;

CREATE TABLE response_event (
	event_id UUID NOT NULL, 
	student_alias_id UUID NOT NULL, 
	item_version_id TEXT NOT NULL, 
	scene response_event_scene_enum NOT NULL, 
	raw_payload JSONB NOT NULL, 
	duration_ms INTEGER, 
	scoring_trace JSONB NOT NULL, 
	error_inferences JSONB NOT NULL, 
	testlet_id TEXT, 
	session_id UUID, 
	audio_play_events JSONB, 
	source_ref JSONB, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	CONSTRAINT pk_response_event PRIMARY KEY (event_id, created_at)
)
 PARTITION BY RANGE (created_at);

CREATE OR REPLACE FUNCTION raise_append_only_error() RETURNS TRIGGER AS $$
BEGIN
    -- D1 三本账只增不改：作答事件账禁止 UPDATE/DELETE。
    -- 为什么用 EXCEPTION 而非 silently skip：违反即失败（宪法开篇铁律），
    -- 应用层应感知错误并走升级流程；silently skip 会让 bug 静默累积。
    RAISE EXCEPTION 'response_event is append-only (D1): UPDATE/DELETE forbidden';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_response_event_append_only
    BEFORE UPDATE OR DELETE ON response_event
    FOR EACH STATEMENT
    EXECUTE FUNCTION raise_append_only_error();

DO $$
DECLARE
    m date;
    next_m date;
    part_name text;
    i int;
BEGIN
    FOR i IN 0..3 LOOP
        m := date_trunc('month', CURRENT_DATE + (i || ' month')::interval)::date;
        next_m := date_trunc('month', CURRENT_DATE + ((i+1) || ' month')::interval)::date;
        part_name := 'response_event_' || to_char(m, 'YYYYMM');
        EXECUTE format(
            'CREATE TABLE IF NOT EXISTS %%I PARTITION OF response_event FOR VALUES FROM (%%L) TO (%%L)',
            part_name, m, next_m
        );
    END LOOP;
END $$;
