-- T-W5-014（AI 总线台账全覆盖，W5-R Go 重锚定）：ai_call_ledger 台账表。
-- 本文件未走 gen_migrations_from_alembic.py 在线捕获（生成机无 Docker/PG），
-- 语句为 alembic 镜像 0026（ai_call_ledger.py）upgrade 的原句，语义 parity 由
-- CI make migrate-go-check 复核。双源纪律：语义修改必须同时落 alembic 0026
-- 与本文件（SQL-1 成对进 gate）。
--
-- D10「AI 可回放」：所有生成式调用（出题/改写/评分/TTS）必须经统一 AI 总线并落
-- 台账——模型标识、模型版本、prompt 版本、token 与成本、关联产物 id 缺一不可。
-- 此前双源现状：冻结实现为 JSONL 文件账（src/core/ai/ledger/ledger.py）且
-- record_call 不在 ai_call 内统一调用（存在台账盲区）；Go 重锚定后总线在
-- core/ai 内同步写账，杜绝「先调用后补账」。DB 表由 W6 出口核验点名
-- （tests/holdout/w6.md H-W6-4：ai_call_ledger 或 llm_call_ledger 至少其一）。
--
-- append-only（冻结实现 ledger.py 自述「仅追加，禁止 UPDATE/DELETE」，D7 审计账）：
-- BEFORE UPDATE OR DELETE FOR EACH STATEMENT 触发器物理强制，复用 0005 统一的
-- raise_append_only_error()——零新函数，禁 CREATE OR REPLACE 重定义既有函数体。
--
-- 字段对齐 ADR 附录 A「ai_call_ledger：任务类型(LLM/TTS/嵌入/ASR)/模型/prompt
-- 版本/成本/产物引用」+ 冻结实现 schemas.LedgerEntry 全列：
--   modality       任务类型四值域 LLM/TTS/嵌入/ASR
--   task_level     路由档位 L0–L3；NULL=尚未完成路由即被前置门拒绝
--                  （PG 对 NULL 恒放行 CHECK，天然兼容部分可达语义）
--   task_name      业务任务名（draft_passage / validate / score / rescore …）
--   provider / model / model_version / prompt_hash / prompt_version
--                  D10 五要素：模型标识+版本+prompt 版本（hash 只存 sha256 前
--                  16 hex，原文永不入账——剥离前后文本皆不入库，防 PII 残留）
--   token_in / token_out / cost_cny / duration_ms    用量与人民币成本
--   status         ok / failed / rejected：
--                    rejected = 出站前的合规门拒绝（PII 剥离失败、预算超限、
--                               目标 allowlist 未注册），出站请求零发出；
--                    failed   = 出站已发生但返回失败（供应商错误/超时/取消）；
--                    ok       = 出站成功且产物已交付调用方。
--                  X12 fail-closed：rejected 也是账面事实，拒绝不留暗数。
--   reason         失败短码（固定小写枚举词，如 redaction_failed /
--                  budget_exceeded / caller_error / ledger…）。硬规则：短码
--                  由总线取自哨兵常量，禁止携带底层 error 文本与任何原文片段
--                  ——凭证与 PII 不得进日志/异常/台账（验收 #1/#3）。
--   artifact_ref   关联产物 id（item_revision_id 等，单题全生命周期 AI 成本归集键）
--   caller_name    注册的出站目标名（allowlist 键，事故时可回溯被调通道）
-- cost_cny 用 DOUBLE PRECISION 而非 NUMERIC：与冻结 JSONL 实现的 float 单价
-- 计算口径逐位一致（round 到 1e-6），避免引入第二套十进制舍入语义。

CREATE TABLE ai_call_ledger (
	call_id TEXT NOT NULL,
	modality TEXT NOT NULL,
	task_level TEXT,
	task_name TEXT NOT NULL,
	provider TEXT NOT NULL,
	model TEXT NOT NULL,
	model_version TEXT NOT NULL,
	prompt_hash TEXT NOT NULL,
	prompt_version TEXT NOT NULL,
	token_in INTEGER NOT NULL,
	token_out INTEGER NOT NULL,
	cost_cny DOUBLE PRECISION NOT NULL,
	duration_ms DOUBLE PRECISION NOT NULL,
	status TEXT NOT NULL,
	reason TEXT,
	fallback BOOLEAN NOT NULL,
	artifact_ref TEXT,
	caller_name TEXT,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (call_id),
	CONSTRAINT ck_ai_call_ledger_modality_domain CHECK (modality IN ('llm', 'tts', 'embedding', 'asr')),
	CONSTRAINT ck_ai_call_ledger_task_level_domain CHECK (task_level IN ('L0', 'L1', 'L2', 'L3')),
	CONSTRAINT ck_ai_call_ledger_status_domain CHECK (status IN ('ok', 'failed', 'rejected'))
);
CREATE INDEX ix_ai_call_ledger_artifact ON ai_call_ledger (artifact_ref);
CREATE INDEX ix_ai_call_ledger_task ON ai_call_ledger (modality, task_level, task_name);
CREATE INDEX ix_ai_call_ledger_created_at ON ai_call_ledger (created_at);
CREATE TRIGGER trg_ai_call_ledger_append_only
    BEFORE UPDATE OR DELETE ON ai_call_ledger
    FOR EACH STATEMENT
    EXECUTE FUNCTION raise_append_only_error();
