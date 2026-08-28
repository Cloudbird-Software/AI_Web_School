-- T-W5-004: 会话题序不可变与结构性 DB 约束（W5-R Go 重锚定）。
-- 本文件未走 gen_migrations_from_alembic.py 在线捕获（生成机无 Docker/PG），
-- 语句为 alembic 镜像 0030（session_topic_order_immutable.py）upgrade 的原句，
-- 语义 parity 由 CI make migrate-go-check 复核。双源纪律：语义修改必须同时落
-- alembic 0030 与本文件（SQL-1 成对进 gate）。
--
-- 缺陷事实（任务卡目标说明 + 0011/0019 现状实读）：
--   ①practice_session.item_sequence（题序 + placement_token）是 A4 追溯链路的
--     锚点，0011 仅以 NOT NULL + 「应用层只写一次」纪律保证创建后不变——任意
--     UPDATE 仍可改写题序，改写后历史作答（response_event）无法与题目对应；
--     DELETE 整行更可直接销毁锚点（response_event 无 FK 指回会话，0003 现状）。
--   ②spec_table.cells 仅校验「是 JSON 数组」（ck_spec_table_cells_is_array），
--     单元格结构（必需键 / 计数为非负整数 / 单元格数与声明一致）零 DB 级约束。
--
-- 结构（全加性，零既有语句改写；题序拒绝面复用 0005 raise_append_only_error()）：
--   practice_session（题序承载结构 = 行内 JSONB 数组，条目
--   {item_version_id, placement_token, item_number}，item_number 即题序行
--   (session_id, seq) 的 seq 维度，session_id 维度由行主键承载）：
--   A1. 题序/身份锚列（session_id/student_alias_id/scene/item_sequence）创建后
--       拒绝 UPDATE——条件式行级触发器，运行态列（current_index/status/
--       answered_count/correct_count/wrong_marks/last_*/completed_at 等）明确放
--       行：0011 docstring「会话进度是运行态操作数据，必须随作答推进原地更新」，
--       任务卡验收 #1 的字面语义；与 0024 内容四表整表触发器的关键差异即在此
--       （本表不在 D1 三本账内，E2E-6 的会话状态同事务推进依赖运行态列可更新）。
--   A2. 整行 DELETE 拒绝（BEFORE DELETE 行级触发器，复用 0005 函数原句）——
--       无任何合法删会话路径（冻结实现 abandon 是 status 更新非删行），删行 =
--       销毁 A4 题序锚点。
--   B1. 题序数组容器 CHECK（jsonb_typeof = 'array'，对齐 spec_table.cells 的
--       ck_spec_table_cells_is_array 惯例）。
--   B2. 题序行结构 + (session_id, seq) 唯一（BEFORE INSERT 行级触发器完整校验：
--       条目必为对象、必需键 item_version_id 非空文本 / item_number 为 ≥1 整数
--       / placement_token 文本或 null、item_number 数组内唯一——重复即以
--       ERRCODE 23505 + 约束名 uq_session_topic_order_seq 显式暴露，应用层
--       core/session 按唯一冲突映射哨兵错误）。UPDATE 面结构校验结构性不可达：
--       item_sequence 任何改写已被 A1 无条件拒绝。
--   spec_table：
--   C1. cells 容器 CHECK（非空数组）。
--   C2. cells 结构触发器（BEFORE INSERT 完整校验，本表 append-only 故 INSERT
--       即唯一写面）：条目必为对象、必需键 content_code/cognitive_level/
--       target_count/difficulty_min/difficulty_max、target_count 为非负整数、
--       难度区间 0≤min≤max≤1（p_correct 口径）、cognitive_level 六值域、
--       (content_code, cognitive_level) 身份唯一（单元格数与声明一致——
--       src/core/assembly/spec_table.py SpecTable._check_cell_uniqueness 的
--       结构性表达）、Σtarget_count > 0（_check_total_count_positive 同源）。
--       全部为冻结 SpecTable/SpecCell 构造期不变式的 DB 级固化，非新语义。
--
-- 与 migrate-go-check 探针的关系（如实声明）：practice_session/spec_table 均
-- 不做整表 append-only（运行态更新/版本账 INSERT 是合法写面），不落入
-- migrate_check 第 3 段「BEFORE UPDATE OR DELETE ON <表>」自动探针清单；本迁移
-- 的行为验收（题序 UPDATE 拒 / 运行态 UPDATE 放 / DELETE 拒 / 非法结构拒）由
-- CI 全量 cycle（第 2 段 down→up 可逆性）+ Go 侧 core/session 语义测试承担。
--
-- 可逆性（make migrate-go-check）：upgrade→downgrade→upgrade 全绿；down 精确
-- DROP 本迁移新建的触发器/函数/约束（IF EXISTS 成对语义）；禁 CREATE OR REPLACE
-- 重定义既有函数体（T-W5-032 #43 裁决：down 后函数体≠目标版本会破坏全量可逆
-- 演练）。

-- ── A1. 题序/身份锚列 UPDATE 拒绝（运行态列放行）────────────────────────
CREATE FUNCTION enforce_practice_session_anchor_immutable() RETURNS TRIGGER AS $$
BEGIN
    IF NEW.session_id IS DISTINCT FROM OLD.session_id
       OR NEW.student_alias_id IS DISTINCT FROM OLD.student_alias_id
       OR NEW.scene IS DISTINCT FROM OLD.scene
       OR NEW.item_sequence IS DISTINCT FROM OLD.item_sequence THEN
        RAISE EXCEPTION 'practice_session topic order anchor columns (session_id/student_alias_id/scene/item_sequence) reject UPDATE';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_practice_session_topic_order_no_rewrite
    BEFORE UPDATE ON practice_session
    FOR EACH ROW
    EXECUTE FUNCTION enforce_practice_session_anchor_immutable();

-- ── A2. 整行 DELETE 拒绝（复用 0005 raise_append_only_error() 原句）──────
CREATE TRIGGER trg_practice_session_topic_order_no_delete
    BEFORE DELETE ON practice_session
    FOR EACH ROW
    EXECUTE FUNCTION raise_append_only_error();

-- ── B1. 题序数组容器 CHECK ────────────────────────────────────────────────
ALTER TABLE practice_session
    ADD CONSTRAINT ck_practice_session_item_sequence_is_array
    CHECK (jsonb_typeof(item_sequence) = 'array');

-- ── B2. 题序行结构 + (session_id, seq) 唯一（INSERT 面）──────────────────
CREATE FUNCTION enforce_practice_session_topic_order_structure() RETURNS TRIGGER AS $$
DECLARE
    entries jsonb := NEW.item_sequence;
    n integer := jsonb_array_length(NEW.item_sequence);
    seqs integer[] := '{}';
    entry jsonb;
    seq integer;
    i integer;
BEGIN
    FOR i IN 0 .. n - 1 LOOP
        entry := entries -> i;
        IF jsonb_typeof(entry) <> 'object' THEN
            RAISE EXCEPTION 'practice_session item_sequence[%] must be a JSON object', i;
        END IF;
        IF entry -> 'item_version_id' IS NULL
           OR jsonb_typeof(entry -> 'item_version_id') <> 'string'
           OR entry ->> 'item_version_id' = '' THEN
            RAISE EXCEPTION 'practice_session item_sequence[%].item_version_id must be a non-empty string', i;
        END IF;
        IF entry -> 'item_number' IS NULL
           OR jsonb_typeof(entry -> 'item_number') <> 'number'
           OR (entry ->> 'item_number')::numeric <> floor((entry ->> 'item_number')::numeric)
           OR (entry ->> 'item_number')::numeric < 1 THEN
            RAISE EXCEPTION 'practice_session item_sequence[%].item_number must be an integer >= 1', i;
        END IF;
        seq := (entry ->> 'item_number')::integer;
        IF entry -> 'placement_token' IS NOT NULL
           AND jsonb_typeof(entry -> 'placement_token') NOT IN ('string', 'null') THEN
            RAISE EXCEPTION 'practice_session item_sequence[%].placement_token must be a string or null', i;
        END IF;
        IF seq = ANY(seqs) THEN
            RAISE EXCEPTION 'practice_session item_sequence duplicate item_number %: (session_id, item_number) must be unique', seq
                USING ERRCODE = '23505', CONSTRAINT = 'uq_session_topic_order_seq';
        END IF;
        seqs := array_append(seqs, seq);
    END LOOP;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_practice_session_topic_order_structure
    BEFORE INSERT ON practice_session
    FOR EACH ROW
    EXECUTE FUNCTION enforce_practice_session_topic_order_structure();

-- ── C1. spec_table cells 容器 CHECK（0019 仅校验「是数组」，缺非空约束）──
ALTER TABLE spec_table
    ADD CONSTRAINT ck_spec_table_cells_not_empty
    CHECK (jsonb_array_length(cells) > 0);

-- ── C2. spec_table cells 结构触发器（必需键/计数/身份数一致）─────────────
CREATE FUNCTION validate_spec_table_cells() RETURNS TRIGGER AS $$
DECLARE
    cells jsonb := NEW.cells;
    n integer := jsonb_array_length(NEW.cells);
    identities text[] := '{}';
    total numeric := 0;
    cell jsonb;
    key text;
    required_keys text[] := ARRAY['content_code', 'cognitive_level', 'target_count', 'difficulty_min', 'difficulty_max'];
    i integer;
BEGIN
    IF n = 0 THEN
        RAISE EXCEPTION 'spec_table cells must not be empty';
    END IF;
    FOR i IN 0 .. n - 1 LOOP
        cell := cells -> i;
        IF jsonb_typeof(cell) <> 'object' THEN
            RAISE EXCEPTION 'spec_table cells[%] must be a JSON object', i;
        END IF;
        FOREACH key IN ARRAY required_keys LOOP
            IF cell -> key IS NULL OR jsonb_typeof(cell -> key) = 'null' THEN
                RAISE EXCEPTION 'spec_table cells[%] missing required key "%"', i, key;
            END IF;
        END LOOP;
        IF jsonb_typeof(cell -> 'content_code') <> 'string'
           OR cell ->> 'content_code' = '' THEN
            RAISE EXCEPTION 'spec_table cells[%].content_code must be a non-empty string', i;
        END IF;
        IF cell ->> 'cognitive_level' NOT IN
           ('remember', 'understand', 'apply', 'analyze', 'evaluate', 'create') THEN
            RAISE EXCEPTION 'spec_table cells[%].cognitive_level must be one of the six Bloom levels', i;
        END IF;
        IF jsonb_typeof(cell -> 'target_count') <> 'number'
           OR (cell ->> 'target_count')::numeric <> floor((cell ->> 'target_count')::numeric)
           OR (cell ->> 'target_count')::numeric < 0 THEN
            RAISE EXCEPTION 'spec_table cells[%].target_count must be a non-negative integer', i;
        END IF;
        IF jsonb_typeof(cell -> 'difficulty_min') <> 'number'
           OR jsonb_typeof(cell -> 'difficulty_max') <> 'number'
           OR (cell ->> 'difficulty_min')::numeric < 0
           OR (cell ->> 'difficulty_min')::numeric > 1
           OR (cell ->> 'difficulty_max')::numeric < 0
           OR (cell ->> 'difficulty_max')::numeric > 1
           OR (cell ->> 'difficulty_min')::numeric > (cell ->> 'difficulty_max')::numeric THEN
            RAISE EXCEPTION 'spec_table cells[%] difficulty window must satisfy 0 <= min <= max <= 1', i;
        END IF;
        total := total + (cell ->> 'target_count')::numeric;
        -- 显式括号：->> 与 || 同优先级左结合，链式裸写曾被 PG 解析为 text ->> unknown
        key := (cell ->> 'content_code') || '/' || (cell ->> 'cognitive_level');
        IF key = ANY(identities) THEN
            RAISE EXCEPTION 'spec_table cells duplicate identity %: cell count must match declared identities', key
                USING ERRCODE = '23505', CONSTRAINT = 'uq_spec_table_cells_identity';
        END IF;
        identities := array_append(identities, key);
    END LOOP;
    IF total <= 0 THEN
        RAISE EXCEPTION 'spec_table cells total target_count must be > 0';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_spec_table_cells_structure
    BEFORE INSERT ON spec_table
    FOR EACH ROW
    EXECUTE FUNCTION validate_spec_table_cells();
