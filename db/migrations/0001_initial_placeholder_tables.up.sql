-- T-W5-032: 由 alembic 0001（initial_placeholder_tables.py）在线捕获生成（语义零变更，tools/sql/gen_migrations_from_alembic.py）；禁止手改。

CREATE TABLE item (
	id BIGINT GENERATED ALWAYS AS IDENTITY, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id)
);

CREATE TABLE gate_certificate (
	id BIGINT GENERATED ALWAYS AS IDENTITY, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id)
);

CREATE TABLE response_event (
	id BIGINT GENERATED ALWAYS AS IDENTITY, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id)
);
