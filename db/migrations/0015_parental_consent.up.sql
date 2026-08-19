-- T-W5-032: 由 alembic 0015（parental_consent.py）在线捕获生成（语义零变更，tools/sql/gen_migrations_from_alembic.py）；禁止手改。

CREATE OR REPLACE FUNCTION raise_parental_consent_append_only_error()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'parental_consent is append-only (T-W4-032): UPDATE/DELETE forbidden';
END;
$$ LANGUAGE plpgsql;

CREATE TABLE parental_consent (
	consent_id UUID NOT NULL, 
	student_alias_id UUID NOT NULL, 
	event_type TEXT NOT NULL, 
	scope JSONB NOT NULL, 
	valid_from TIMESTAMP WITH TIME ZONE, 
	valid_until TIMESTAMP WITH TIME ZONE, 
	version INTEGER NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (consent_id), 
	CONSTRAINT ck_parental_consent_event_type_domain CHECK (event_type IN ('grant', 'revoke')), 
	CONSTRAINT ck_parental_consent_version_positive CHECK (version >= 1), 
	CONSTRAINT ck_parental_consent_event_type_time_consistency CHECK ((event_type = 'grant' AND valid_from IS NOT NULL) OR (event_type = 'revoke' AND valid_from IS NULL     AND valid_until IS NULL)), 
	CONSTRAINT ck_parental_consent_scope_has_purpose CHECK (jsonb_typeof(scope) = 'object' AND scope ? 'purpose')
);
CREATE INDEX ix_parental_consent_student ON parental_consent (student_alias_id, version);
CREATE INDEX ix_parental_consent_student_purpose_version ON parental_consent (student_alias_id, (scope ->> 'purpose'), version);

CREATE TRIGGER trg_parental_consent_append_only
    BEFORE UPDATE OR DELETE ON parental_consent
    FOR EACH STATEMENT
    EXECUTE FUNCTION raise_parental_consent_append_only_error();
