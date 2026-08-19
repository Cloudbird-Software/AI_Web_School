# W5-R 任务卡撰写简报（Go+BAML 重建波 · 宪法兑现 × 语言重写）

> 目标：**同一套信任硬化出口语义，做在 Go+BAML 上一次到位**。W5-R 出口 = 强制实证矩阵首次全绿（对 Go 实现），三类攻击（伪造证书发布 / 匿名冒充学生 / 绕过写入服务直写）全部被 DB 或认证层拒绝；外加 GO-1..5 / BAML-1 / SQL-1/2 进 gate。
> 输入：W0–W4 全部成果（DB schema 与数据语义保留）；specs/constitution.md v2.0（A8/A9/A10、D9/D10/D11、P9、X11–X13）；specs/adr/0003；specs/adr/0004（Go+BAML 立项）；specs/adr/0005（scorer 契约变更申请）；issue #34；《2026-07-30 代码审查报告四份.txt》复验清单。
> 铁律：**W5-R 期间不接受任何新功能任务**（沿用原 W5 铁律）。理由见 tasks/roadmap.md §一——三本账只增不改，脏数据不可回滚，比没数据更贵。
> 重写策略（ADR-0004 §四）：DB 迁移语义保留移植；specs/tasks/治理文件保留；src/ 全部服务代码与测试重写为 Go；LLM 调用重写为 BAML；Python src/ 只读冻结（安全修复除外），Go E2E 全绿后归档。

## W5-R 工作流（S1–S7 语义不变，落点改为 Go）

| # | 工作流 | 内容（Go 落点） | 依据 |
|---|---|---|---|
| S1 | 门与账的物理强制 | 迁移移植为纯 SQL（`db/migrations/`，golang-migrate 约定，up/down 成对）；内容版本账 append-only 触发器；`gate_certificate_id` FK + 失败留痕；发布服务证书验真；内容寻址缺参 fail-loud；会话题序不可变——全部做在 Go 服务与 SQL 迁移上 | D1 D2 D3 A8 |
| S2 | 认证与主体绑定 | Go 认证框架（主体：学生 alias / 教研 / 运维 / 机构）；全端点接入并校验主体与 path 中 alias 一致；服务端凭证不落库不回传；CORS 白名单、限流、异常映射不泄露内部信息；渲染出口沙箱 | D9 A9 X13 |
| S3 | 合规硬约束落地 | 家长授权接入在线入口（未授权 403 且零写入）；授权账版本原子分配；PII 保险库角色与审计独立事务；姓名脱敏（`tools/fuzz/fuzz_redaction.py` 的不变式随 Go 移植为原生 fuzz）；AI 总线 PII fail-closed（无 bypass 开关）与台账全覆盖 | D7 D10 X12 |
| S4 | 事务与并发正确性 | 领域服务不自 commit（`*sql.Tx` 显式传递）；作答提交幂等键 + 行锁 + `go test -race` 并发测试；估计器指针切换并发安全 | D11 A9 |
| S5 | 校验门缺陷修复 | 查重验证器走真实内容 digest；语篇事实核查干净语篇必须 pass；评分器契约校验；等价判定全角表补全——校验器实现语言 Go，**llm_judge 类 validator 的准入留给 W6**（共识基准集就绪前不宣称已强制，A8） | A2 D4 X11 |
| S6 | 测试与 CI 可信度 | 迁移可逆全量验证（migrate down→up）；冻结契约守卫遍历 FROZEN.txt 全量；无排名静态扫描盲区；黄金路径补至 ≥10 种交互类型；契约测试移植 Go；**gate 切 Go 工具链：GO-1 gofmt / GO-2 errcheck / GO-3 无循环依赖 / GO-4 `go test -race` / GO-5 goleak / BAML-1 golden / SQL-1 up-down 成对 / SQL-2 sqlc diff** | P3 P4 A8 |
| S7 | 实证矩阵与出口 | `TRACEABILITY.md` 强制实证矩阵（宪法条款 ↔ 可执行实证，指向 Go 服务）+ CI 校验；认证引入的 API 契约变更申请（顺延为 ADR-0006，见 ADR-0004 编号说明）；w5r.sh 出口脚本 | A8 P5 P9 |

## 技术基线（ADR-0004 §三）

- **应用**：Go 模块化单体，入口 `cmd/`；`core/`（六边形核心域，零学科特判——X6/GO-3 由 lint 强制）；`api/`；`packs/`（学科包=SubjectPack、学段包=GradeBandPack）；`registry/`（作答交互 + 评分器双注册表）。
- **LLM prompt**：`baml_src/` → 生成 `baml_client/`（生成物提交入库，纳入 build/vet）；操作员/评价者/量规评分全部 .baml 函数；BAML 版本锁定。
- **数据**：迁移纯 SQL 只增不改；查询经 sqlc 类型生成；禁重 ORM。
- **验证**：Go 原生 fuzz + 表驱动属性测试；schema contract；import 边界 lint。

## W5-R 端到端出口定义（与原 W5 逐条对齐，实现载体 Go）

- **E2E-1（伪证书）**：以最高权限直接 `INSERT/UPDATE` 一条 `status='published'`、`gate_certificate_id='cert_FAKE'` 的内容版本 → 被 FK 拒绝；换成合法但 artifact 不匹配的证书 → 被 Go 写入服务拒绝。
- **E2E-2（改历史）**：对 `item_version` / `material_version` / `corpus_version` / `item_template_version` 执行 UPDATE 与 DELETE（含 `WHERE FALSE`）→ 全部被 append-only 触发器拒绝。
- **E2E-3（冒充）**：不带凭证 / 带他人凭证访问 `POST /sessions`、`POST /sessions/{id}/responses`、`GET /reports/weakness/{alias}` → 401/403；任何公开端点响应体中不出现服务端凭证。
- **E2E-4（未授权未成年人）**：无有效家长授权的 alias 开练习/诊断/测量会话 → 403，且 `response_event` 零写入。
- **E2E-5（PII）**：构造含姓名/手机号的 prompt 与 TTS 文本，令剥离器抛异常 → 调用被拒绝（fail-closed），台账记录失败原因；全仓不存在 bypass 开关。
- **E2E-6（事务与并发）**：同一 session 并发提交同一题 10 次 → 恰好 1 条 `response_event`、`current_index` 恰好推进 1；评分失败时事件与会话状态同进同退（Go 并发测试 + `-race`）。
- **E2E-7（AI 可回放）**：一次 AI 量规评分 → `scoring_trace` 含 model_version 与 prompt 版本（BAML 函数版本），AI 台账可按 item_revision 归集；替换模型版本后历史报告仍引用当时版本。
- **E2E-8（查重真实生效）**：写入两条内容完全相同的内容版本 → 第二条被查重验证器判 review/fail；测试不得通过伪造 ID 制造命中。
- **E2E-9（CI 可信度）**：迁移全量 down→up 成功（migrate 工具链）；任意冻结契约被修改 → contract-watch 红；黄金路径覆盖 ≥10 种交互类型且全绿；gate 含 GO-1..5 / BAML-1 / SQL-1/2 且全绿。
- **E2E-10（实证矩阵）**：`TRACEABILITY.md` 强制实证矩阵中每条宪法条款都有实证路径（指向 Go 实现），CI 校验矩阵完整性；矩阵缺项即红。
- **E2E-11（不退化）**：W0–W4 出口语义在 Go 实现下等价全绿；测试收集数不低于 W4 基线（Python 侧测试在归档前保持绿，作为语义对照）。

## 新增基建任务卡（W5-R 专属，先于原卡执行）

| 卡 | 内容 | 依赖 |
|---|---|---|
| T-W5-030 | 技术验证 spike：baml-go runtime + sqlc + 最小 .baml 走通"生成→golden test→gate" | — |
| T-W5-031 | Go 模块化单体骨架：cmd/core/api/packs/registry 分层 + healthz + 最小 fuzz | T-W5-030 |
| T-W5-032 | 迁移移植：alembic 22 个迁移 → `db/migrations/*.up/down.sql` + migrate 运行时 + migrate-check | — |
| T-W5-033 | gate 切 Go 工具链：gofmt/errcheck/race/goleak + baml golden + sqlc diff 进 CI gate | T-W5-030/031/032 |

原卡 T-W5-001..029/T01..T03 的重锚定对照见 `tasks/w5/REANCHORING.md`（语义不变，owner_module 与验收脚本指向 Go 落点）。

## 非目标（W5-R 明确排除）

新交互类型、新学科包内容、新组卷策略、学生端 UI、支付与商业化、多租户、自适应算法、性能优化（除非是安全/正确性修复的副产品）、**llm_judge 类 validator 的注册启用与共识基准集构建（W6）**。**任何"顺手加个功能"的 PR 一律拒绝。**
