-- T-W5-032: 由 alembic 0007（kp_closure_graph_release.py）在线捕获生成（语义零变更，tools/sql/gen_migrations_from_alembic.py）；禁止手改。
CREATE TYPE graph_release_status_enum AS ENUM ('draft', 'active', 'frozen', 'superseded');

CREATE TABLE graph_release (
	release_id TEXT NOT NULL, 
	status graph_release_status_enum DEFAULT 'draft' NOT NULL, 
	valid_from TIMESTAMP WITH TIME ZONE, 
	valid_to TIMESTAMP WITH TIME ZONE, 
	superseded_by TEXT, 
	description TEXT, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (release_id)
);
ALTER TABLE graph_release ADD CONSTRAINT fk_graph_release_superseded_by FOREIGN KEY(superseded_by) REFERENCES graph_release (release_id) DEFERRABLE INITIALLY DEFERRED;

CREATE TABLE kp_closure (
	closure_id BIGINT GENERATED ALWAYS AS IDENTITY, 
	graph_release_id TEXT NOT NULL, 
	src_node_id TEXT NOT NULL, 
	dst_node_id TEXT NOT NULL, 
	rel_type TEXT NOT NULL, 
	depth INTEGER NOT NULL, 
	path_count INTEGER DEFAULT 1 NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (closure_id), 
	CONSTRAINT fk_kpc_graph_release FOREIGN KEY(graph_release_id) REFERENCES graph_release (release_id), 
	CONSTRAINT fk_kpc_src FOREIGN KEY(src_node_id) REFERENCES kp_node (node_id), 
	CONSTRAINT fk_kpc_dst FOREIGN KEY(dst_node_id) REFERENCES kp_node (node_id), 
	CONSTRAINT fk_kpc_rel_type FOREIGN KEY(rel_type) REFERENCES relation_type (rel_type), 
	CONSTRAINT uq_kpc_release_src_dst_rel_depth UNIQUE (graph_release_id, src_node_id, dst_node_id, rel_type, depth), 
	CONSTRAINT ck_kpc_depth_positive CHECK (depth >= 1), 
	CONSTRAINT ck_kpc_path_count_positive CHECK (path_count >= 1), 
	CONSTRAINT ck_kpc_no_self_loop CHECK (src_node_id <> dst_node_id)
);
CREATE INDEX ix_kpc_release_src ON kp_closure (graph_release_id, src_node_id, rel_type);
