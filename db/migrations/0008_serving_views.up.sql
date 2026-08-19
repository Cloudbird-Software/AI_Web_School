-- T-W5-032: 由 alembic 0008（serving_views.py）在线捕获生成（语义零变更，tools/sql/gen_migrations_from_alembic.py）；禁止手改。
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'serving_reader') THEN
        CREATE ROLE serving_reader LOGIN PASSWORD 'serving_reader_pwd';
    END IF;
END $$;
CREATE OR REPLACE VIEW v_serving_item_version AS
SELECT
    iv.item_version_id,
    iv.item_id,
    iv.status,
    iv.objective,
    iv.interaction_ref,
    iv.content,
    iv.scoring_ref,
    iv.error_bindings,
    iv.lineage,
    iv.rendered_snapshot,
    iv.gate_certificate_id,
    iv.published_at,
    iv.retired_at,
    iv.created_at,
    i.pack_id,
    i.tier,
    i.template_version_id
FROM item_version iv
JOIN item i ON i.item_id = iv.item_id
WHERE iv.status = 'published'
  AND iv.published_at IS NOT NULL
  AND iv.retired_at IS NULL;
CREATE OR REPLACE VIEW v_serving_material_version AS
SELECT
    mv.material_version_id,
    mv.material_id,
    mv.content_ref,
    mv.license_id,
    mv.status,
    mv.lineage,
    mv.gate_certificate_id,
    mv.published_at,
    mv.retired_at,
    mv.created_at,
    m.kind,
    m.pack_id,
    ml.source AS license_source,
    ml.rights_holder,
    ml.scope AS license_scope,
    ml.expires_at AS license_expires_at,
    ml.decision AS license_decision
FROM material_version mv
JOIN material m ON m.material_id = mv.material_id
LEFT JOIN material_license ml ON ml.license_id = mv.license_id
WHERE mv.status = 'published'
  AND mv.published_at IS NOT NULL
  AND mv.retired_at IS NULL
  AND (ml.expires_at IS NULL OR ml.expires_at > now())
  AND (ml.decision IS NULL OR ml.decision = 'approved');
CREATE OR REPLACE VIEW v_serving_corpus_version AS
SELECT
    cv.version_id,
    cv.asset_id,
    cv.content_ref,
    cv.license_id,
    cv.status,
    cv.lineage,
    cv.gate_certificate_id,
    cv.published_at,
    cv.retired_at,
    cv.created_at,
    ca.kind,
    ca.pack_id,
    ml.source AS license_source,
    ml.expires_at AS license_expires_at,
    ml.decision AS license_decision
FROM corpus_version cv
JOIN corpus_asset ca ON ca.asset_id = cv.asset_id
LEFT JOIN material_license ml ON ml.license_id = cv.license_id
WHERE cv.status = 'published'
  AND cv.published_at IS NOT NULL
  AND cv.retired_at IS NULL
  AND (ml.expires_at IS NULL OR ml.expires_at > now())
  AND (ml.decision IS NULL OR ml.decision = 'approved');
GRANT SELECT ON v_serving_item_version TO serving_reader;
GRANT SELECT ON v_serving_material_version TO serving_reader;
GRANT SELECT ON v_serving_corpus_version TO serving_reader;
