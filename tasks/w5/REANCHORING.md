# W5-R 重锚定对照表（ADR-0004 / issue #34）

> 语义不变原则：原 W5 任务卡的**验收语义逐条保留**（门/账/认证/合规/事务的强制行为），仅实现载体与验收脚本落点从 Python src/ 切换为 Go。本表是原卡与 Go 落点的唯一对照事实源；原卡文件不改写（历史留痕），执行时以本表落点为准。

## 一、目录映射（Python → Go）

| Python（冻结） | Go（W5-R 落点） | 说明 |
|---|---|---|
| src/core/ | core/ | 六边形核心域；X6/GO-3：core 不 import packs/*（lint 强制） |
| src/api/ | api/ | 路由/中间件/错误映射；OpenAPI 契约不变 |
| src/packs/subject-*/ gradeband_low/ | packs/subject*/ gradebandlow/ | 学科包/学段包，只复用注册表 |
| src/registry/ | registry/ | 作答交互 + 评分器双注册表（D4） |
| src/workbench/ | （不移植） | 冻结；W7 学生端（TS）取代 |
| alembic/versions/ | db/migrations/ | 纯 SQL up/down；语义审计见 §二 |
| tests/contract/ | tests-go/contract/（或 core/*/contract_test.go） | 契约测试移植；断言语义不变 |
| tests/unit/ | 各包 *_test.go | 单测随包；`-race` 强制 |
| tests/golden*、tests/simulator | tests-go/golden* | 黄金数据集（YAML/JSON）语言无关，直接复用 |
| src/core/ai/（prompt 字符串） | baml_src/ + baml_client/ | prompt 全部出代码进 .baml（languages.yaml llm_prompt 层） |

## 二、迁移 SQL up/down 审计与移植清单（alembic → 纯 SQL）

审计结论（2026-08-19）：**22/22 个迁移 downgrade 均非空 pass，全部有真实回滚逻辑**。移植时 down 语义原样翻译，逐对移植、逐对验证（SQL-1：up+down 成对进 gate）。

| # | alembic 迁移 | 内容 | down 现状 | 移植目标 |
|---|---|---|---|---|
| 0001 | initial_placeholder_tables | 占位表 ×3 | 非空（drop 反向） | db/migrations/0001_*.up/down.sql |
| 0002 | item_model | 内容模型 | 非空（删表+触发器+枚举，重建占位） | 0002 |
| 0003 | response_event | 作答事件账 | 非空 | 0003 |
| 0004 | gate_tables | 校验门三表 | 非空 | 0004 |
| 0005 | append_only_unify | append-only 统一 | 非空（还原函数定义） | 0005 |
| 0006 | knowledge_graph | 知识图谱 | 非空 | 0006 |
| 0007 | kp_closure_graph_release | 闭包发布 | 非空 | 0007 |
| 0008 | serving_views | serving 视图 | 非空（删视图+REVOKE） | 0008 |
| 0009 | paper_trace | 试卷溯源 | 非空 | 0009 |
| 0010 | exposure_ledger | 曝光账 | 非空 | 0010 |
| 0011 | practice_session | 练习会话 | 非空 | 0011 |
| 0012 | review_tables | 复习排程 | 非空 | 0012 |
| 0013 | item_param | 题目参数（三场景分离） | 非空 | 0013 |
| 0014 | pii_vault | PII 保险库 schema | 非空（DROP SCHEMA CASCADE） | 0014 |
| 0015 | parental_consent | 家长授权 | 非空 | 0015 |
| 0016 | estimator_run | 估计器运行账 | 非空 | 0016 |
| 0017 | score_run | 评分运行账 | 非空 | 0017 |
| 0018 | item_lifecycle | 内容生命周期 | 非空 | 0018 |
| 0019 | spec_table | 规格 | 非空 | 0019 |
| 0020 | passage | 语篇 | 非空 | 0020 |
| 0021 | blueprint | 组卷蓝图 | 非空 | 0021 |
| 0022 | shadow_score | 影子评分账 | 非空 | 0022 |

移植纪律：每个迁移移植后跑 `migrate up→down→up`（migrate-check 进 gate，SQL-1）；alembic 目录在 Go 版 E2E 全绿前保留不动（对照），归档时一并处理。

## 三、原任务卡重锚定对照（T-W5-001..029 / T01..T03）

执行顺序调整：**先基建（T-W5-030..033），后原卡**。原卡间批次依赖（A→B→C→D→E→F）保持不变。

| 原卡 | 标题（语义不变） | Go 落点（owner_module） | 状态 |
|---|---|---|---|
| T-W5-001 | 内容版本账 append-only 物理强制补齐 | db/migrations/0005 移植 + core/content（Go 触发器迁移与写入服务） | 重锚定 |
| T-W5-002 | 门证书外键补建与门失败留痕 | db/migrations/0004 移植 + core/gate | 重锚定 |
| T-W5-003 | 发布服务证书验真与内容寻址 fail-loud | core/content + core/gate | 重锚定 |
| T-W5-004 | 会话题序不可变与结构性 DB 约束 | db/migrations/0011 移植 + core/session | 重锚定 |
| T-W5-005 | 认证与主体绑定框架 | core/auth（新）+ api/middleware | 重锚定 |
| T-W5-006 | 全端点接入认证与学生主体绑定 | api/routers | 重锚定 |
| T-W5-007 | 服务端凭证治理与敏感字段屏蔽 | core/auth + api | 重锚定 |
| T-W5-008 | API 边界加固（CORS/限流/错误映射） | api/middleware | 重锚定 |
| T-W5-009 | 渲染出口安全（PDF 沙箱与失败检测） | core/render | 重锚定 |
| T-W5-010 | 家长授权接入在线会话入口 | core/session + core/compliance | 重锚定 |
| T-W5-011 | 家长授权账版本原子性与并发安全 | db/migrations/0015 移植 + core/compliance | 重锚定 |
| T-W5-012 | PII 保险库权限模型与审计独立事务 | db/migrations/0014 移植 + core/compliance | 重锚定 |
| T-W5-013 | 姓名脱敏边界修复与强断言 | core/compliance（含 Go 原生 fuzz，移植 tools/fuzz/fuzz_redaction.py 不变式） | 重锚定 |
| T-W5-014 | AI 总线 fail-closed、台账全覆盖与出站加固 | core/ai（BAML 经 baml_src，台账 Go 实现） | 重锚定 |
| T-W5-015 | TTS 链路 PII 剥离与台账对齐 | core/ai/tts | 重锚定 |
| T-W5-016 | 评分链路可回放与评分器契约校验 | core/scoring + registry | 重锚定 |
| T-W5-017 | 事件写入事务边界归位 | core/events（*sql.Tx 显式传递） | 重锚定 |
| T-W5-018 | 作答提交幂等与并发安全 | core/session（go test -race 并发测试） | 重锚定 |
| T-W5-019 | 估计器指针切换并发安全 | core/estimator（原 src/core/... 对应域） | 重锚定 |
| T-W5-020 | 查重验证器走真实内容摘要路径 | core/gate/validators | 重锚定 |
| T-W5-021 | 语篇事实核查判定与阻断策略修正 | core/gate/validators | 重锚定 |
| T-W5-022 | 迁移可逆全量验证与 PR 阶段拦截 | db/migrations + gate（SQL-1；migrate down→up 全量） | 重锚定（与 T-W5-032 协同） |
| T-W5-023 | CI 守卫盲区修复（冻结契约 / 无排名扫描） | tools/（Go 或脚本均可，语义不变） | 重锚定 |
| T-W5-024 | 黄金路径端到端补齐至 ≥10 种交互类型 | tests-go/golden-path | 重锚定 |
| T-W5-025 | 关键覆盖空洞补齐（并发/门/认证/API 集成） | tests-go/ | 重锚定 |
| T-W5-026 | 打包与部署正确性 | Dockerfile（Go 多阶段构建）+ compose | 重锚定 |
| T-W5-027 | 强制实证矩阵（宪法条款 ↔ 可执行实证） | specs/contracts/TRACEABILITY.md（指向 Go 实证） | 重锚定 |
| T-W5-028 | API v1.1 契约变更申请（认证引入） | ADR-0006（原预留 ADR-0004 编号被立项占用，见 ADR-0004 编号说明） | 重锚定 |
| T-W5-029 | W5 出口脚本与不退化基线 | scripts/wave-exit/w5r.sh | 重锚定 |
| T-W5-T01/T02/T03 | 验证卡 ×3 | 验收脚本指向 Go 服务 | 重锚定 |

## 四、新增基建卡（本 PR 物化）

T-W5-030（spike）/ T-W5-031（Go 骨架）/ T-W5-032（迁移移植）/ T-W5-033（gate 切 Go 工具链），卡片见同目录。
