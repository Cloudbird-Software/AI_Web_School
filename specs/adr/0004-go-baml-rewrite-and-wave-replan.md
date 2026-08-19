# ADR-0004：Go+BAML 重写立项与 W5–W8 波次重排

- 状态：已接受（2026-08-19，依据 GitHub issue #34 人类定案；组织治理 languages.yaml `language_change.rule`——语言更换 = 重新立项，本 ADR 即立项备案）
- 依据：issue #34《最终开发方案定案》· specs/constitution.md v2.0 · Cloudbird-Software/.github `governance/policy/languages.yaml`
- 取代：tasks/roadmap.md 原 W5 信任硬化波的"在 Python src/ 上硬化"路径（出口语义不变，实现载体变更，见 §四）
- 编号说明：tasks/w5/BRIEF.md S7 原预留"ADR-0004"给认证引入的 API 契约变更申请；本 ADR 按人类最新定案占用 0004，该 API 契约变更申请顺延为 ADR-0006+（T-W5-028 重锚定时更新）

## 一、为什么要改（问题陈述）

1. **语言合规**：组织 `languages.yaml` 的 `layers.application.allowed = go(default) / typescript(frontend only)`，Python 不在应用层允许清单。现有 src/（FastAPI）自 W0 起累积的服务代码处于治理灰区，必须重写归位。
2. **LLM 进产线缺 harness**：宪法 D10/A2 要求所有生成式调用受控（注册表 + 校验门 + AI 台账）。Python 侧的 prompt 散落在代码字符串中，违反 `layers.llm_prompt`（allowed = baml，forbidden: `prompt_string_interpolation_in_code`）。
3. **人工标注不可扩展**：原设计含 human_confirm 人工队列（scorer.yaml），与"数据生产链无人"的目标冲突（issue #34 D-B）。
4. **W5 硬化只做一次**：信任硬化（门/账/认证/合规/事务）若先做在 Python 再移植 Go，等于同一高危工作做两遍。直接做在 Go 上。

## 二、三项定案（issue #34，不再讨论）

| # | 决策 | 内容 |
|---|---|---|
| D-A | LLM 作为生产资料 | LLM 操作员（生成侧）+ LLM 评价者（验证/评分侧），全部运行在受控 harness（注册表 + 校验门 + AI 台账）内，不做裸调用 |
| D-B | 摒弃人工标注 | 黄金基准 = 公开数据集 + 合成数据（≥3 异构模型共识打标，全一致才采纳）；人工只保留治理角色（owner 批依赖、ADR 裁决、license 审查）；低置信路由到高档模型仲裁，不转人工 |
| D-C | 语言重写 | 应用层 Python → Go；LLM prompt 层 → BAML |

## 三、技术基线与依赖提案（owner 审批清单）

| 层 | 选型 | 新增依赖（提案，待 owner 批） | 许可 |
|---|---|---|---|
| 应用 | Go 模块化单体：入口 `cmd/`，`core/`（六边形核心域）、`api/`、`packs/`（学科/学段包）、`registry/` | `github.com/boundaryml/baml`（BAML Go runtime） | Apache-2.0（黑名单外） |
| 数据 | PostgreSQL 16；迁移纯 SQL（`db/migrations/*.up.sql`/`*.down.sql`，golang-migrate 目录约定）、只增不改；查询类型生成 | `github.com/sqlc-dev/sqlc`（codegen，开发期） + `github.com/golang-migrate/migrate/v4`（运行期） | MIT（黑名单外） |
| 验证 | Go 原生 fuzz + 表驱动属性测试；schema contract；import 边界 lint（go vet + 自研 GO-3 检查） | `github.com/stretchr/testify`（仅测试断言便利，可选） | Apache-2.0 |
| LLM prompt | `baml_src/`（源）→ `baml_client/`（生成物，提交入库）；操作员/评价者/量规评分全部 .baml 函数 | BAML CLI（npm `@boundaryml/baml`，开发期） | Apache-2.0 |

stdlib_alternative 说明：baml 无 stdlib 等价物（prompt 工程的代码外置是本 ADR 的目的）；sqlc/golang-migrate 的 stdlib 替代是手写 SQL + database/sql，代价是失去类型生成与迁移版本管理，不建议。

治理规则映射（进 gate 或 review，languages.yaml）：GO-1 gofmt 零 diff / GO-2 errcheck / GO-3 无循环依赖 / GO-4 `go test -race` / GO-5 goleak / BAML-1 prompt 变更过 golden test / SQL-1 迁移 up+down 成对 / SQL-2 查询经 sqlc / MOD-1..5 / IF-1。

## 四、重写策略：保留 vs 重写 vs 冻结

- **保留**：DB schema 与迁移语义（SQL 与语言无关；22 个 alembic 迁移移植为纯 SQL 并沿用既有 down 语义，见 tasks/w5/REANCHORING.md §二）；specs/（宪法、契约、ADR——本 ADR 除外不动宪法）；tasks/ 台账；治理文件。
- **重写**：src/ 全部服务代码 → Go；tests/ → Go 测试；全部 LLM 调用 → BAML；gate 工具链切 Go。
- **冻结**：Python src/ 与 Streamlit workbench 只读冻结、不接新功能；Go 版 E2E 全绿后归档（历史保留，不删除——作答证据与审计不可抹除）；workbench 由 W7 学生端取代。
- **W5 信任硬化只做一次，直接做在 Go 上**（认证 / append-only / PII fail-closed / 事务幂等 / 门 FK），出口语义 = 原 E2E-1..11 全部对 Go 实现成立。

## 五、影响面

| 受影响方 | 影响 | 处置 |
|---|---|---|
| 原 W5 任务卡 T-W5-001..029/T01..T03 | 实现载体 Python→Go，验收语义不变 | 重锚定表 tasks/w5/REANCHORING.md；卡片语义不重写，验收脚本指向 Go 服务 |
| specs/contracts/registries/scorer.yaml（冻结） | human_confirm 与 D-B 冲突 | 走 P5 契约变更申请：ADR-0005（本 PR 只申请，不改文件） |
| roadmap.md / board.md | W5 → W5-R | 本 PR 重排；W6–W8 顺延但目标不变 |
| Python src/ | 冻结 | Go E2E 全绿前不删不改（安全修复除外，如 T-W0-010） |
| CI gate | 工具链切 Go | T-W5-033：gofmt/errcheck/race/goleak + baml golden + sqlc diff 进 gate；Python 检查在双轨期保留 |

## 六、风险与对策

| 风险 | 对策 |
|---|---|
| BAML Go codegen 边角 bug（上游 #3690 等） | 生成代码纳入 build/vet 必过；BAML 版本锁定；规避 null-only class |
| 共识打标系统性偏差 | 三源分离（public/synthetic/real）保证污染不进真实资产；漂移监控跌破阈值自动降回 shadow；基准集轮次化可整体换血 |
| 重写期功能冻结被破坏 | W5-R 不接受功能 PR（铁律沿用）；gate 切 Go 后即刻生效 |
| LLM 成本失控 | AI 台账成本核算已有；W6 起预算硬顶 + 超限熔断 |
| 双轨期 Python/Go 漂移 | Python 冻结只读；契约测试与黄金数据集共享同一份（SQL/数据与语言无关） |

## 七、立即行动子任务（issue #34 §十，随本 ADR 落地）

- [x] ADR-0004（本文件）
- [x] ADR-0005：scorer 契约变更申请（human_confirm → 模型仲裁）
- [x] W5-R BRIEF + 任务卡物化（T-W5-030..033 新增；原卡重锚定表）
- [x] 迁移 SQL up/down 审计与移植清单（REANCHORING.md §二：22/22 已有 down）
- [x] 公开数据源 license 审查清单（content/sources/CANDIDATES.md，语文英语优先）
- [x] roadmap.md / board.md 重排（本 PR）
- [ ] 技术验证 spike（T-W5-030：baml-go runtime + sqlc + 最小 .baml 走通"生成→golden test→gate"）——独立 PR

## 八、结论

应用层重写为 Go、prompt 层收敛到 BAML、数据层契约与三本账语义原样保留、W5 信任硬化与重写合并为 W5-R 一次做完。本 ADR 生效后，roadmap.md 以 W5-R 为唯一就绪波次。
