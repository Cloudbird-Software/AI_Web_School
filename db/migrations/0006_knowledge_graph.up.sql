-- T-W5-032: 由 alembic 0006（knowledge_graph.py）在线捕获生成（语义零变更，tools/sql/gen_migrations_from_alembic.py）；禁止手改。
CREATE TYPE kp_node_status_enum AS ENUM ('draft', 'active', 'deprecated', 'superseded');

CREATE TABLE relation_type (
	rel_type TEXT NOT NULL, 
	pack_id TEXT, 
	directed BOOLEAN DEFAULT true NOT NULL, 
	transitive BOOLEAN DEFAULT false NOT NULL, 
	acyclic BOOLEAN DEFAULT true NOT NULL, 
	"symmetric" BOOLEAN DEFAULT false NOT NULL, 
	description TEXT, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (rel_type)
);

CREATE TABLE kp_node (
	node_id TEXT NOT NULL, 
	pack_id TEXT NOT NULL, 
	dimension TEXT NOT NULL, 
	code TEXT NOT NULL, 
	title TEXT NOT NULL, 
	std_anchor TEXT, 
	gradeband TEXT, 
	status kp_node_status_enum DEFAULT 'draft' NOT NULL, 
	valid_from TIMESTAMP WITH TIME ZONE, 
	valid_to TIMESTAMP WITH TIME ZONE, 
	supersedes_id TEXT, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (node_id), 
	CONSTRAINT uq_kp_node_pack_dim_code UNIQUE (pack_id, dimension, code)
);
ALTER TABLE kp_node ADD CONSTRAINT fk_kp_node_supersedes FOREIGN KEY(supersedes_id) REFERENCES kp_node (node_id) DEFERRABLE INITIALLY DEFERRED;

CREATE TABLE kp_edge (
	edge_id BIGINT GENERATED ALWAYS AS IDENTITY, 
	src_node_id TEXT NOT NULL, 
	dst_node_id TEXT NOT NULL, 
	rel_type TEXT NOT NULL, 
	attrs JSONB DEFAULT '{}'::jsonb NOT NULL, 
	provenance JSONB DEFAULT '{}'::jsonb NOT NULL, 
	valid_from TIMESTAMP WITH TIME ZONE, 
	valid_to TIMESTAMP WITH TIME ZONE, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (edge_id), 
	CONSTRAINT fk_kpe_src FOREIGN KEY(src_node_id) REFERENCES kp_node (node_id), 
	CONSTRAINT fk_kpe_dst FOREIGN KEY(dst_node_id) REFERENCES kp_node (node_id), 
	CONSTRAINT fk_kpe_rel_type FOREIGN KEY(rel_type) REFERENCES relation_type (rel_type), 
	CONSTRAINT uq_kp_edge_src_dst_rel UNIQUE (src_node_id, dst_node_id, rel_type), 
	CONSTRAINT ck_kp_edge_no_self_loop CHECK (src_node_id <> dst_node_id)
);
