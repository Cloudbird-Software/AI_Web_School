# 强制实证矩阵（宪法 A8/P9 执行机制 · T-W5-027）

> **载体差异记录（P9 精神：差异明示）**：宪法 A8 字面锚点为
> `specs/contracts/TRACEABILITY.md`——该文件位于 contract-drift 引擎检测面
> （specs/contracts/** 下被 PR 触碰的非 JSON 文件一律按 JSON 解析、fail-closed 红，
> PR #108 实证），矩阵无法在不炸门的情况下写入。
> 经对抗审查裁决：矩阵载体移至本文件（specs/ 根，引擎检测面外）；
> 宪法正文修改须人类+ADR，本差异本身作为 A8 矩阵的首条「载体迁移」记录。
> 机器校验：`go run ./tools/traceability`（默认读本文件）。

## 强制实证矩阵（A8/P9 执行机制，T-W5-027）

> 机器校验：`go run ./tools/traceability`（解析本表——条款覆盖宪法全集、
> 已强制行必有实证路径、路径文件必须存在；违反即红 exit 1）。
> 状态语义：**已强制**=实证路径承载机器可验的强制；**仅纪律**=无机器实证，
> 靠评审与流程（禁止在汇报中宣称已强制）；**未实现**=明示缺口+承接波次。
> 诚实原则（A8）：没有实证的条款一律视为未实现。

### 北极星愿景（V1–V6）

| 条款 | 层级 | 实证路径 | 状态 |
|---|---|---|---|
| V1 | 流程 | specs/constitution.md#北极星（波次出口口径=闭环完整性，tasks/roadmap.md） | 仅纪律（产品价值主张不可机器断言；闭环出口机制见 V3 行） |
| V2 | DB+服务 | db/migrations/0003_response_event.up.sql; core/events/writer.go::Writer.Record; db/migrations/0031_response_submission_idempotency.up.sql | 已强制（作答回流承载：append-only+幂等入账）；归因/重算闭环 W6 |
| V3 | 流程 | scripts/wave-exit/w4.sh（波次出口=闭环验收的既有执行体；冻结面） | 仅纪律（W5-R 出口脚本 w5r.sh 归 owner 波次收尾；W6 起每波刷新） |
| V4 | 服务 | core/ai/bus.go::Bus.Call（产能层已建）；core/gate/verifier.go（质量层）；core/scoring/runner.go（诊断层地基） | 已强制（层结构承载）；错误模式库/实测参数数据层 W6-W7 |
| V5 | 服务 | core/ai/cost.go::ComputeCostCNY; core/ai/ledger.go（token+成本逐调用归集） | 已强制（AI 面成本可见）；内容生产全成本归集 W6 |
| V6 | 未实现 | —（W7 学生端 + 真实用户验收；模拟器/演示不构成交付） | 未实现（承接 W7） |

### 架构公理（A1–A10）

| 条款 | 层级 | 实证路径 | 状态 |
|---|---|---|---|
| A1 | DB+测试 | db/migrations/0024_content_ledger_append_only.up.sql; core/content/publish.go::PublishService | 已强制（单一内容资产域，发布经同一服务） |
| A2 | DB+服务 | db/migrations/0028_gate_cert_fk_failure_trail.up.sql; core/gate/verifier.go::CertificateVerifier.Verify; core/content/publish.go（发布持证验真 fail-loud） | 已强制 |
| A3 | DB+服务 | db/migrations/0024_content_ledger_append_only.up.sql; db/migrations/0029_pii_vault_roles.up.sql; db/migrations/0030_session_topic_order_immutable.up.sql（账表 append-only 触发器族）; core/estimator/pointer.go::ActivePointerStore（版本可切换历史可引用） | 已强制 |
| A4 | 契约+服务 | specs/contracts/api/openapi-v1.1.yaml::NextItemResponse（T-W5-028 交付物）（placement_token/source_ref 显式化，ADR-0006 待批）; core/gate/validators/digest.go::ContentDigest | 已强制（服务面）；契约显式化随 ADR-0006 签署生效 |
| A5 | CI | tools/go-lint/import-boundary（core 禁 import packs，X6 同源）; Makefile::check-go | 已强制 |
| A6 | 测试 | packs/packs_test.go::TestGradeBandPack（学段包类型化承载框架） | 已强制（框架面）；学段差异内容 W6-W7 |
| A7 | 服务 | core/content/publish.go::PublishService（统一入库服务/同一校验门） | 已强制 |
| A8 | CI | tools/traceability/main.go（本矩阵机器校验） | 已强制（自我承载） |
| A9 | 测试 | core/session/topicorder_test.go（会话域事务/幂等/并发/授权 -race 全绿）; core/session/submit_test.go; core/session/consent_test.go | 已强制 |
| A10 | 未实现 | —（漏斗事件/单位成本内建报表；不排名不破 D8/D7 的约束由 D8/D7 实证承载） | 未实现（承接 W7-W8） |

### 数据与内容铁律（D1–D11）

| 条款 | 层级 | 实证路径 | 状态 |
|---|---|---|---|
| D1 | DB | db/migrations/0024_content_ledger_append_only.up.sql（内容版本账四表）; db/migrations/0003_response_event.up.sql（作答事件账）; db/migrations/0028_gate_cert_fk_failure_trail.up.sql（gate_failure 留痕）; tools/sql/migrate_check.py::append-only 探针 | 已强制（三本账物理 append-only） |
| D2 | DB+服务 | db/migrations/0028_gate_cert_fk_failure_trail.up.sql（证书 FK 六引用面+NOT VALID）; core/content/publish.go::PublishService（发布持证 fail-loud）; core/gate/trail.go::FailureTrail | 已强制 |
| D3 | 服务+测试 | core/gate/validators/digest.go::ContentDigest（唯一摘要口径）；core/content/publish_test.go（冻结向量 parity） | 已强制 |
| D4 | 服务+测试 | registry/registry.go::Registry[Validator]; core/gate/validators/registry.go::PlatformRegistry; registry/scorer.go::ScorerTable（无契约面条目注册即拒） | 已强制 |
| D5 | DB | db/migrations/0013_item_param.up.sql（source/purpose_scope 分列） | 已强制（DB 面）；估计面 W6（estimator 域） |
| D6 | 服务+测试 | core/estimator/pointer.go::ActivePointerStore（advisory lock+偏唯一索引+留痕 SwitchTrail） | 已强制 |
| D7 | DB+服务+测试 | db/migrations/0029_pii_vault_roles.up.sql; core/compliance/vault.go::VaultService（AES-256-GCM+审计独立事务）；core/ai/redact.go（剥离 fail-closed） | 已强制 |
| D8 | CI | tools/scan/norank（跨用户排名静态扫描+白名单纪律） | 已强制 |
| D9 | 服务+测试 | core/auth/token.go（令牌校验）；api/middleware/middleware.go::RequireAuth；core/auth/credential.go（凭证零回传）；api/authz_test.go（13 端点匿名探测 401） | 已强制 |
| D10 | 服务+测试 | core/ai/bus.go::Bus.Call（台账统一落账+fail-closed 三路径）；core/scoring/runner.go::buildTrace（model_version 入 scoring_trace） | 已强制 |
| D11 | 服务+测试 | core/events/writer.go（ErrNoTransaction fail-closed + go/parser 静态守卫）；core/session/submit.go::SubmitAnswer（幂等+行锁）；core/compliance/vault.go（审计独立双 Executor） | 已强制 |

### 开发纪律（P1–P9）

| 条款 | 层级 | 实证路径 | 状态 |
|---|---|---|---|
| P1 | CI | .github/workflows/ci.yml::repo-gate（PR 必须引用任务卡 T-W[0-9]-N） | 已强制 |
| P2 | CI | org:.github/workflows/adversary-gate.yml（独立对抗审查面，org 级） | 已强制（org 门） |
| P3 | CI | .github/workflows/ci.yml::gate（唯一 required check 严格聚合 ADR-0032） | 已强制 |
| P4 | 测试 | tests/golden-path/（冻结面 Python 链路；30 题型承载） | 已强制（冻结面绿）；Go 侧 E2E W6 |
| P5 | CI+流程 | tools/ci/check_sources.py; specs/contracts/FROZEN.txt（只增不改）; specs/adr/0006-api-v1.1-auth.md（变更申请实例） | 已强制 |
| P6 | 流程 | tasks/board.md（升级队列机制） | 仅纪律（升级动作不可机器断言） |
| P7 | 未实现 | —（PR 记录执行模型/token 成本；.agent/telemetry/ JSONL 机器校验缺） | 未实现（承接 W6 遥测面） |
| P8 | 流程 | tasks/board.md（派工粒度规则） | 仅纪律 |
| P9 | CI | tools/traceability/main.go（矩阵常绿机器校验，波次出口刷新义务） | 已强制（自我承载） |

### 绝对禁令（X1–X13）

| 条款 | 层级 | 实证路径 | 状态 |
|---|---|---|---|
| X1 | CI | .github/workflows/ci.yml::repo-gate 反测试削弱 + specs/test-freeze/MANIFEST.sha256（哈希冻结清单） | 已强制 |
| X2 | CI | org:.github/workflows/adversary-gate.yml（对抗审查，与 P2/P3 信号面联动） | 已强制（org 门；伪造成功的最终防线=人审） |
| X3 | CI | .github/workflows/ci.yml::repo-gate 泄密扫描 + core/auth/credential.go::Registry.Mask（三出口屏蔽） | 已强制 |
| X4 | 流程 | tasks/board.md（owner_module 互斥，tools/opc board 校验） | 仅纪律 |
| X5 | 流程 | specs/constitution.md#X5（架构面：api 无生成式端点，core/ai Caller 注入不出网） | 仅纪律（在线消费已发布池的端到端断言 W7） |
| X6 | CI | tools/go-lint/import-boundary（core 禁 import packs）+ api/middleware/import_boundary_test.go（go/parser 补盲区） | 已强制 |
| X7 | CI | tools/sql/migratechain（版本连续+alembic 链守卫）+ tools/sql/check_pairs.py + tools/sql/migrate_check.py（Docker 全量演练） | 已强制 |
| X8 | CI | org:CI-Workflows/.github/workflows/dep-review.yml（license 白名单+age≥90 天+lockfile 一致性 fail-closed） | 已强制（org 门） |
| X9 | 流程 | AGENTS.md（上下文索引纪律） | 仅纪律 |
| X10 | 流程 | docs/backup-runbook.md（备份演练 runbook；W4 运维面） | 仅纪律（执行演练记录 W8） |
| X11 | 流程 | —（互证形态不可静态枚举；发现机制=P2 对抗审查判例 #79） | 仅纪律 |
| X12 | 服务+测试 | core/ai/bus_test.go（PII 剥离失败/台账失败/预算超限三路径 fail-closed）；core/compliance/consent_test.go（store 不可用拒绝）；core/compliance/vault_test.go | 已强制 |
| X13 | 测试 | api/authz_test.go（openapi 运行期扫描逐端点匿名探测 401——无主体端点结构性不可合入） | 已强制 |

> 波次义务（P9）：W5-R 出口时本矩阵全绿；W6 起每波刷新（新增条款同波补实证；
> 「已强制」行实证路径变更须同步本表并过 tools/traceability 校验）。

