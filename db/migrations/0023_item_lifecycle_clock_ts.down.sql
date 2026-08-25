-- 镜像 alembic 0023 downgrade：恢复 now() 默认值（历史行两方向均不被触碰）。
ALTER TABLE item_lifecycle_transition ALTER COLUMN created_at SET DEFAULT now();
