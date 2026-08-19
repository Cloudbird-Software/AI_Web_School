-- T-W5-032: 由 alembic 0014（pii_vault.py）在线捕获生成（语义零变更，tools/sql/gen_migrations_from_alembic.py）；禁止手改。

DO $$ BEGIN
    CREATE ROLE pii_vault_reader NOLOGIN;
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;
CREATE SCHEMA IF NOT EXISTS pii_vault;
REVOKE ALL ON SCHEMA pii_vault FROM PUBLIC;
REVOKE ALL ON ALL TABLES IN SCHEMA pii_vault FROM PUBLIC;

CREATE TABLE pii_vault.student_identity (
	student_alias_id UUID NOT NULL, 
	name_ciphertext BYTEA NOT NULL, 
	name_nonce BYTEA NOT NULL, 
	phone_ciphertext BYTEA NOT NULL, 
	phone_nonce BYTEA NOT NULL, 
	address_ciphertext BYTEA NOT NULL, 
	address_nonce BYTEA NOT NULL, 
	parent_contact_ciphertext BYTEA NOT NULL, 
	parent_contact_nonce BYTEA NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (student_alias_id)
);

CREATE TABLE pii_vault.access_log (
	access_id UUID NOT NULL, 
	student_alias_id UUID NOT NULL, 
	accessor TEXT NOT NULL, 
	accessed_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	purpose TEXT NOT NULL, 
	PRIMARY KEY (access_id)
);
GRANT USAGE ON SCHEMA pii_vault TO pii_vault_reader;
GRANT SELECT ON pii_vault.student_identity TO pii_vault_reader;
GRANT INSERT ON pii_vault.access_log TO pii_vault_reader;
REVOKE ALL ON pii_vault.student_identity FROM PUBLIC;
REVOKE ALL ON pii_vault.access_log FROM PUBLIC;
