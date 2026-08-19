-- T-W5-032: 由 alembic 0002（item_model.py）在线捕获生成（语义零变更，tools/sql/gen_migrations_from_alembic.py）；禁止手改。
DROP TRIGGER IF EXISTS trg_item_version_on_publish ON item_version;
DROP FUNCTION IF EXISTS fn_item_version_on_publish();
ALTER TABLE item DROP CONSTRAINT fk_item_current_version;
ALTER TABLE material DROP CONSTRAINT fk_material_current_version;
ALTER TABLE corpus_asset DROP CONSTRAINT fk_corpus_asset_current_version;
ALTER TABLE item_template DROP CONSTRAINT fk_item_template_current_version;

DROP TABLE publication;

DROP TABLE item_kp;

DROP TABLE item_group;

DROP TABLE corpus_version;

DROP TABLE corpus_asset;

DROP TABLE material_version;

DROP TABLE material;

DROP TABLE item_version;

DROP TABLE item;

DROP TABLE item_template_version;

DROP TABLE item_template;

DROP TABLE material_license;
DROP TYPE IF EXISTS material_license_decision_enum;
DROP TYPE IF EXISTS material_kind_enum;
DROP TYPE IF EXISTS item_template_version_status_enum;
DROP TYPE IF EXISTS item_version_status_enum;
DROP TYPE IF EXISTS item_tier_enum;

CREATE TABLE item (
	id BIGINT GENERATED ALWAYS AS IDENTITY, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id)
);
