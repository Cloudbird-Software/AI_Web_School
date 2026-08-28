-- T-W5-004: 会话题序不可变与结构性 DB 约束（W5-R Go 重锚定）——成对回滚。
-- 本文件未走 gen_migrations_from_alembic.py 在线捕获（生成机无 Docker/PG），
-- 语句为 alembic 镜像 0030（session_topic_order_immutable.py）downgrade 的原句，
-- 语义 parity 由 CI make migrate-go-check 复核。双源纪律：语义修改必须同时落
-- alembic 0030 与本文件（SQL-1 成对进 gate）。
--
-- 成对语义：精确移除 up 新建的 3 个 practice_session 触发器 + 2 函数 + 1 约束、
-- 1 个 spec_table 触发器 + 1 函数 + 2 约束，还原 0011/0019/0029 形态；不触碰
-- 0005 raise_append_only_error / 0019 raise_spec_table_append_only_error 等
-- 既有函数体，不丢任何既有对象（全量可逆演练 down→up 全绿的前提）。

DROP TRIGGER IF EXISTS trg_practice_session_topic_order_structure ON practice_session;
DROP TRIGGER IF EXISTS trg_practice_session_topic_order_no_delete ON practice_session;
DROP TRIGGER IF EXISTS trg_practice_session_topic_order_no_rewrite ON practice_session;
DROP FUNCTION IF EXISTS enforce_practice_session_topic_order_structure();
DROP FUNCTION IF EXISTS enforce_practice_session_anchor_immutable();
ALTER TABLE practice_session
    DROP CONSTRAINT IF EXISTS ck_practice_session_item_sequence_is_array;

DROP TRIGGER IF EXISTS trg_spec_table_cells_structure ON spec_table;
DROP FUNCTION IF EXISTS validate_spec_table_cells();
ALTER TABLE spec_table
    DROP CONSTRAINT IF EXISTS ck_spec_table_cells_not_empty;
