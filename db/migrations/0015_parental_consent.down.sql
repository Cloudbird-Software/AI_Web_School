-- T-W5-032: 由 alembic 0015（parental_consent.py）在线捕获生成（语义零变更，tools/sql/gen_migrations_from_alembic.py）；禁止手改。
DROP TRIGGER IF EXISTS trg_parental_consent_append_only ON parental_consent;

DROP TABLE parental_consent;
DROP FUNCTION IF EXISTS raise_parental_consent_append_only_error();
