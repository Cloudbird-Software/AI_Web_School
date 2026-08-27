-- T-W5-002（门证书外键补建与门失败留痕，W5-R Go 重锚定）：D2 门的数据库级强制补全。
-- 本文件未走 gen_migrations_from_alembic.py 在线捕获（生成机无 Docker/PG），
-- 语句为 alembic 镜像 0028（gate_cert_fk_failure_trail.py）upgrade 的原句，语义
-- parity 由 CI make migrate-go-check 复核。双源纪律：语义修改必须同时落
-- alembic 0028 与本文件（SQL-1 成对进 gate）。
--
-- 缺陷事实（任务卡目标说明 + 冻结实现 src/core/gate 实读）：
--   ①所有内容表的 gate_certificate_id 只有「published 非空须带证」的 CHECK
--     （0002/0005/0020），没有任何指向 gate_certificate(cert_id) 的外键——
--     契约 specs/contracts/db/item-model.md §2.2 明文「FK→gate_certificate，
--     可空」，物理层却允许 cert_FAKE 这类任意字符串通过 CHECK 直写发布态。
--     D2 自迁移落地起真正成立：绕过写入服务直写发布态在 DB 层失败。
--   ②冻结编排器 run_gate 在 fail/review 时以占位 cert_id='cert:none' 写
--     gate_run.certificate_id（orchestrator._insert_run_and_verdict 内注释自述
--     「W3 计划迁移落地该占位行」），该占位行从未在任何迁移中创建——生产首次
--     门失败即撞 fk_gr_certificate 外键错、留痕事务整体回滚。X11 反模式：
--     失败留痕依赖测试 fixture 预插。本迁移选择任务卡验收 #2 方案①——
--     certificate_id 放宽为可空（语义正确：没签发证书就没有证书引用；
--     NOT NULL 撤销是放宽不是收紧，存量行零回填），不做系统占位证书行。
--
-- 三段内容：A 外键补建 / B 占位证书解绑 / C gate_failure 失败留痕账。

-- ── A. 内容表 → gate_certificate 外键补建 ────────────────────────────────
-- DEFERRABLE INITIALLY DEFERRED（验收 #1）：与既有指针外键（0002
-- fk_item_template_current_version 等）同策略——发布事务里内容行与证书行的
-- 写入先后序自由，一致性检查统一推迟到 COMMIT 边界。
-- NOT VALID 的两段式取舍（任务卡「NOT VALID + VALIDATE 两步或说明」取说明）：
-- 只对增量生效即足以让 D2 成立（新 INSERT/更新到发布态当场验 FK），而存量行
-- 校验留待独立数据审计卡执行 VALIDATE CONSTRAINT——历史行可能有占位/伪造 id
-- （这正是本卡要终结的反模式产物），在无先清点后验数据之前贸然 VALIDATE 会让
-- 迁移在生产首次执行即硬失败；NOT VALID 下假证无法再新增，风险不再增长。
-- 全部六个 gate_certificate_id 引用面逐一判定（grep db/migrations 全量实证）：
--   item_version（0002）：题目版本发布态（ck_iv_published_requires_gate_cert）。
--   material_version（0002）：素材版本发布态（ck_mv_published_requires_gate_cert）。
--   corpus_version（0005 补列）：语料版本发布态（ck_cv_published_requires_gate_cert）。
--   passage（0020）：语篇发布态（ck_passage_published_requires_gate）。
--   publication（0002）：发布事件账本体——publication.gate_certificate_id 记录
--     该次发布所持证书，同属带发布态的引用面。
--   item_lifecycle_transition（0018）：生命周期转换账 QUARANTINE/RETIRE 转换
--     所持 retire 类证书；与发布同一扇门的另一入口，一并收口。

ALTER TABLE item_version ADD CONSTRAINT fk_iv_gate_certificate FOREIGN KEY (gate_certificate_id) REFERENCES gate_certificate (cert_id) DEFERRABLE INITIALLY DEFERRED NOT VALID;
ALTER TABLE material_version ADD CONSTRAINT fk_mv_gate_certificate FOREIGN KEY (gate_certificate_id) REFERENCES gate_certificate (cert_id) DEFERRABLE INITIALLY DEFERRED NOT VALID;
ALTER TABLE corpus_version ADD CONSTRAINT fk_cv_gate_certificate FOREIGN KEY (gate_certificate_id) REFERENCES gate_certificate (cert_id) DEFERRABLE INITIALLY DEFERRED NOT VALID;
ALTER TABLE passage ADD CONSTRAINT fk_passage_certificate FOREIGN KEY (gate_certificate_id) REFERENCES gate_certificate (cert_id) DEFERRABLE INITIALLY DEFERRED NOT VALID;
ALTER TABLE publication ADD CONSTRAINT fk_pub_certificate FOREIGN KEY (gate_certificate_id) REFERENCES gate_certificate (cert_id) DEFERRABLE INITIALLY DEFERRED NOT VALID;
ALTER TABLE item_lifecycle_transition ADD CONSTRAINT fk_ilt_certificate FOREIGN KEY (gate_certificate_id) REFERENCES gate_certificate (cert_id) DEFERRABLE INITIALLY DEFERRED NOT VALID;

-- ── B. gate_run 占位证书解绑（X11 终结）─────────────────────────────────
ALTER TABLE gate_run ALTER COLUMN certificate_id DROP NOT NULL;

-- ── C. gate_failure 门失败留痕账（append-only，失败也是账面事实）──────────
-- 与 gate_run/gate_verdict 的分工：后两者按验证器逐条留「跑过什么、判了什么」
-- （含 pass），本表一行记一次被拒事实的最小可审四元组——什么规则、什么输入、
-- 何时、为何拒。Go core/gate.FailureTrail 为其唯一写入面（Executor 显式事务
-- 模式，与 core/events.Writer 同纪律：不自 commit）；失败不回滚业务判断链路。
CREATE TABLE gate_failure (
	failure_id TEXT NOT NULL,
	artifact_type TEXT NOT NULL,
	artifact_ref TEXT NOT NULL,
	validator_id TEXT NOT NULL,
	validator_version TEXT NOT NULL,
	policy_version TEXT NOT NULL,
	reason TEXT NOT NULL,
	evidence JSONB DEFAULT '{}'::jsonb NOT NULL,
	failed_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (failure_id),
	CONSTRAINT ck_gf_artifact_type_domain CHECK (artifact_type IN ('item', 'material', 'corpus', 'group', 'blueprint', 'audio'))
);

CREATE INDEX ix_gate_failure_artifact ON gate_failure (artifact_ref);
CREATE INDEX ix_gate_failure_failed_at ON gate_failure (failed_at);

CREATE TRIGGER trg_gate_failure_append_only
    BEFORE UPDATE OR DELETE ON gate_failure
    FOR EACH STATEMENT
    EXECUTE FUNCTION raise_append_only_error();
