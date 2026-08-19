-- T-W5-032: 由 alembic 0002（item_model.py）在线捕获生成（语义零变更，tools/sql/gen_migrations_from_alembic.py）；禁止手改。
CREATE TYPE item_tier_enum AS ENUM ('A', 'B', 'C', 'D');
CREATE TYPE item_version_status_enum AS ENUM ('draft', 'quarantined', 'published', 'retired');
CREATE TYPE item_template_version_status_enum AS ENUM ('draft', 'published', 'retired');
CREATE TYPE material_kind_enum AS ENUM ('passage', 'image', 'table', 'audio');
CREATE TYPE material_license_decision_enum AS ENUM ('approved', 'rejected', 'expired');

DROP TABLE item;

CREATE TABLE material_license (
	license_id TEXT NOT NULL, 
	source TEXT, 
	rights_holder TEXT, 
	scope TEXT, 
	expires_at TIMESTAMP WITH TIME ZONE, 
	decision material_license_decision_enum NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (license_id)
);

CREATE TABLE item_template (
	template_id TEXT NOT NULL, 
	pack_id TEXT NOT NULL, 
	current_version_id TEXT, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (template_id)
);

CREATE TABLE item_template_version (
	template_version_id TEXT NOT NULL, 
	template_id TEXT NOT NULL, 
	dsl_version TEXT NOT NULL, 
	spec JSONB NOT NULL, 
	status item_template_version_status_enum NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (template_version_id), 
	CONSTRAINT fk_itv_template FOREIGN KEY(template_id) REFERENCES item_template (template_id)
);
ALTER TABLE item_template ADD CONSTRAINT fk_item_template_current_version FOREIGN KEY(current_version_id) REFERENCES item_template_version (template_version_id) DEFERRABLE INITIALLY DEFERRED;

CREATE TABLE item (
	item_id TEXT NOT NULL, 
	pack_id TEXT NOT NULL, 
	tier item_tier_enum NOT NULL, 
	template_version_id TEXT, 
	current_version_id TEXT, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (item_id), 
	CONSTRAINT fk_item_template_version FOREIGN KEY(template_version_id) REFERENCES item_template_version (template_version_id)
);

CREATE TABLE item_version (
	item_version_id TEXT NOT NULL, 
	item_id TEXT NOT NULL, 
	status item_version_status_enum NOT NULL, 
	objective JSONB NOT NULL, 
	interaction_ref JSONB NOT NULL, 
	content JSONB NOT NULL, 
	scoring_ref JSONB NOT NULL, 
	error_bindings JSONB NOT NULL, 
	lineage JSONB NOT NULL, 
	rendered_snapshot JSONB, 
	gate_certificate_id TEXT, 
	published_at TIMESTAMP WITH TIME ZONE, 
	retired_at TIMESTAMP WITH TIME ZONE, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (item_version_id), 
	CONSTRAINT fk_iv_item FOREIGN KEY(item_id) REFERENCES item (item_id), 
	CONSTRAINT ck_iv_published_requires_gate_cert CHECK (published_at IS NULL OR gate_certificate_id IS NOT NULL), 
	CONSTRAINT ck_iv_quarantine_requires_rendered CHECK (status = 'draft' OR rendered_snapshot IS NOT NULL)
);
ALTER TABLE item ADD CONSTRAINT fk_item_current_version FOREIGN KEY(current_version_id) REFERENCES item_version (item_version_id) DEFERRABLE INITIALLY DEFERRED;

CREATE TABLE material (
	material_id TEXT NOT NULL, 
	kind material_kind_enum NOT NULL, 
	pack_id TEXT, 
	current_version_id TEXT, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (material_id)
);

CREATE TABLE material_version (
	material_version_id TEXT NOT NULL, 
	material_id TEXT NOT NULL, 
	content_ref TEXT NOT NULL, 
	license_id TEXT NOT NULL, 
	status item_version_status_enum NOT NULL, 
	lineage JSONB NOT NULL, 
	gate_certificate_id TEXT, 
	published_at TIMESTAMP WITH TIME ZONE, 
	retired_at TIMESTAMP WITH TIME ZONE, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (material_version_id), 
	CONSTRAINT fk_mv_material FOREIGN KEY(material_id) REFERENCES material (material_id), 
	CONSTRAINT fk_mv_license FOREIGN KEY(license_id) REFERENCES material_license (license_id), 
	CONSTRAINT ck_mv_published_requires_gate_cert CHECK (published_at IS NULL OR gate_certificate_id IS NOT NULL)
);
ALTER TABLE material ADD CONSTRAINT fk_material_current_version FOREIGN KEY(current_version_id) REFERENCES material_version (material_version_id) DEFERRABLE INITIALLY DEFERRED;

CREATE TABLE corpus_asset (
	asset_id TEXT NOT NULL, 
	kind TEXT NOT NULL, 
	pack_id TEXT, 
	current_version_id TEXT, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (asset_id)
);

CREATE TABLE corpus_version (
	version_id TEXT NOT NULL, 
	asset_id TEXT NOT NULL, 
	content_ref TEXT NOT NULL, 
	license_id TEXT NOT NULL, 
	lineage JSONB NOT NULL, 
	status item_version_status_enum NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (version_id), 
	CONSTRAINT fk_cv_asset FOREIGN KEY(asset_id) REFERENCES corpus_asset (asset_id), 
	CONSTRAINT fk_cv_license FOREIGN KEY(license_id) REFERENCES material_license (license_id)
);
ALTER TABLE corpus_asset ADD CONSTRAINT fk_corpus_asset_current_version FOREIGN KEY(current_version_id) REFERENCES corpus_version (version_id) DEFERRABLE INITIALLY DEFERRED;

CREATE TABLE item_group (
	item_group_id TEXT NOT NULL, 
	material_version_id TEXT, 
	item_version_ids TEXT[] NOT NULL, 
	ordered BOOLEAN DEFAULT false NOT NULL, 
	testlet BOOLEAN DEFAULT false NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (item_group_id), 
	CONSTRAINT fk_ig_material_version FOREIGN KEY(material_version_id) REFERENCES material_version (material_version_id)
);
ALTER TABLE item_group ADD CONSTRAINT ck_ig_max_six_items CHECK (array_length(item_version_ids, 1) <= 6);

CREATE TABLE item_kp (
	item_kp_id BIGINT GENERATED ALWAYS AS IDENTITY, 
	item_id TEXT NOT NULL, 
	item_version_id TEXT NOT NULL, 
	dimension TEXT NOT NULL, 
	kp_code TEXT NOT NULL, 
	gradeband TEXT, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (item_kp_id), 
	CONSTRAINT fk_ikp_item FOREIGN KEY(item_id) REFERENCES item (item_id), 
	CONSTRAINT fk_ikp_item_version FOREIGN KEY(item_version_id) REFERENCES item_version (item_version_id)
);

CREATE TABLE publication (
	publication_id TEXT NOT NULL, 
	item_id TEXT NOT NULL, 
	item_version_id TEXT NOT NULL, 
	gate_certificate_id TEXT, 
	published_by TEXT NOT NULL, 
	published_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (publication_id), 
	CONSTRAINT fk_pub_item FOREIGN KEY(item_id) REFERENCES item (item_id), 
	CONSTRAINT fk_pub_item_version FOREIGN KEY(item_version_id) REFERENCES item_version (item_version_id)
);

CREATE OR REPLACE FUNCTION fn_item_version_on_publish() RETURNS TRIGGER AS $$
BEGIN
    -- §6.3：item_version INSERT 且 status='published' 时前移 item.current_version_id
    -- 为什么只在 INSERT：item_version 只增不改（D1），不会 UPDATE；触发器仅挂在 INSERT。
    IF NEW.status = 'published' THEN
        UPDATE item SET current_version_id = NEW.item_version_id
        WHERE item_id = NEW.item_id;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_item_version_on_publish
    AFTER INSERT ON item_version
    FOR EACH ROW
    EXECUTE FUNCTION fn_item_version_on_publish();
