-- T-W5-001 成对回滚：精确 DROP 四个内容版本账 append-only 触发器（IF EXISTS
-- 与 golang-migrate 全量 down→up 演练兼容）。只撤触发器，不触碰
-- raise_append_only_error 函数体与其他账表触发器（0012 review_policy 等
-- 同函数触发器继续生效）。镜像 alembic 0024 downgrade。

DROP TRIGGER IF EXISTS trg_item_template_version_append_only ON item_template_version;
DROP TRIGGER IF EXISTS trg_material_version_append_only ON material_version;
DROP TRIGGER IF EXISTS trg_corpus_version_append_only ON corpus_version;
DROP TRIGGER IF EXISTS trg_passage_append_only ON passage;
