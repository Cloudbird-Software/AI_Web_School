-- T-W5-032: 由 alembic 0003（response_event.py）在线捕获生成（语义零变更，tools/sql/gen_migrations_from_alembic.py）；禁止手改。
DROP TRIGGER IF EXISTS trg_response_event_append_only ON response_event;
DROP FUNCTION IF EXISTS raise_append_only_error();

DROP TABLE response_event;
DROP TYPE IF EXISTS response_event_scene_enum;

CREATE TABLE response_event (
	id BIGINT GENERATED ALWAYS AS IDENTITY, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id)
);
