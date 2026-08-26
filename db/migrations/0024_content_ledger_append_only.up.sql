-- T-W5-001 内容版本账 append-only 物理强制补齐（W5-R Go 重锚定）。
-- 镜像 alembic/versions/0024_content_ledger_append_only.py：本文件未走
-- gen_migrations_from_alembic.py 在线捕获（生成机无 Docker/PG），语句为该
-- upgrade 的原句（四个 CREATE TRIGGER），语义 parity 由 CI make migrate-go-check 复核。
-- 双源纪律：语义修改必须同时落 alembic 0024 与本文件（SQL-1 成对进 gate）。

-- 语义：D1「内容版本账只增不改」（宪法第二部分：Item/Material/Corpus 全版本化）
-- 此前在 DB 层零物理强制，四表的 UPDATE/DELETE 直接成功。本迁移复用迁移 0005
-- 定义的 raise_append_only_error() 挂语句级触发器——BEFORE UPDATE OR DELETE
-- FOR EACH STATEMENT：连零行命中的 UPDATE 也拒绝（migrate_check 探针验收点）。
-- 零新函数；禁 CREATE OR REPLACE 重定义既有函数体（down 后函数体≠目标版本，
-- 破坏全量可逆演练，T-W5-032 #43 裁决）。

-- 覆盖清单与逐表判定理由（代码实证：全仓 grep 无任何针对这些表的 UPDATE/DELETE
-- 路径；全部写路径 INSERT 时一次定型 status/门字段）：
--   item_template_version（0002）：母题模板版本行，一 row = 一版快照；
--     models 明示「D1：永不 UPDATE/DELETE」。
--   material_version（0002）：素材版本行，content_ref 内容寻址，改内容 =
--     新 INSERT 新 id（D3），无合法改行路径。
--   corpus_version（0002）：语料版本行，门字段与 material_version 对齐
--     （0005 补列）；publish_corpus_asset 走 INSERT 定型。
--   passage（0020）：审阅扩盖——writer.py 明示「语篇身份即版本，每次改写 =
--     新行新 passage_id，D1 只增不改」，content_hash 寻址每行即不可变快照。

-- 审阅确认不覆盖的近邻表（防误判逐表留痕，理由详见 alembic 0024 docstring）：
--   item_version：契约 §4 合法受控状态机前移（publication.py），历史由独立
--     append-only 表承载（publication / 0018 item_lifecycle_transition）；
--     整表触发器会掐断合法签发流，按任务卡范围排除。
--   item_template/item/material/corpus_asset：指针身份表，current_version_id
--     前移是契约合法 UPDATE（0002 AFTER INSERT 触发器依赖其可更新）。
--   material_license：decision 生命周期可变。
--   relation_type/kp_node/kp_edge/graph_release（0006/0007）：时间性生效失效与
--     状态前移是设计内变更机制；kp_closure 为派生缓存（release 内 DELETE+重建）。
--   estimator_run/score_run 等其余运行账：非本卡「内容版本账」域。

-- 豁免说明：覆盖四表均无合法受控字段更新，无需豁免分支；受控指针更新
-- （current_version_id）已由结构性设计隔离在身份表，不以整表放行替代。

CREATE TRIGGER trg_item_template_version_append_only
    BEFORE UPDATE OR DELETE ON item_template_version
    FOR EACH STATEMENT
    EXECUTE FUNCTION raise_append_only_error();

CREATE TRIGGER trg_material_version_append_only
    BEFORE UPDATE OR DELETE ON material_version
    FOR EACH STATEMENT
    EXECUTE FUNCTION raise_append_only_error();

CREATE TRIGGER trg_corpus_version_append_only
    BEFORE UPDATE OR DELETE ON corpus_version
    FOR EACH STATEMENT
    EXECUTE FUNCTION raise_append_only_error();

CREATE TRIGGER trg_passage_append_only
    BEFORE UPDATE OR DELETE ON passage
    FOR EACH STATEMENT
    EXECUTE FUNCTION raise_append_only_error();
