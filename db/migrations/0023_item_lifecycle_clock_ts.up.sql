-- 镜像 alembic 0023（item_lifecycle_clock_ts.py）：created_at 默认值 now() → clock_timestamp()。
-- 本文件未走 gen_migrations_from_alembic.py 在线捕获（生成机无 Docker/PG），
-- 语句为 alembic 0023 upgrade 的原句（单条 ALTER），语义 parity 由 make migrate-go-check 在 CI 复核。
ALTER TABLE item_lifecycle_transition ALTER COLUMN created_at SET DEFAULT clock_timestamp();
