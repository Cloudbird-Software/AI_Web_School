-- T-W5-032: 由 alembic 0014（pii_vault.py）在线捕获生成（语义零变更，tools/sql/gen_migrations_from_alembic.py）；禁止手改。
DROP SCHEMA IF EXISTS pii_vault CASCADE;
DROP ROLE IF EXISTS pii_vault_reader;
