# 工程规则（全 agent 必读 · 本文件由 tools/sync_rules.sh 同步到 AGENTS.md 与 .trae/rules/）

<!-- entry-protocol v2 -->

### 入口协议（陌生 agent 从这里开始——宪法 §11 / ADR-0055/0095）

0. **按意图定角色**（指引=.github 仓 `docs/agent/ROLE-*.md`，ADR-0095）：开新意图→ROLE-IR · 把已签署 IR 写成 spec→ROLE-SPEC · 实现卡片→ROLE-IMPLEMENT · 验收/人类让你处理 issues→ROLE-ACCEPT
1. 取 ghcb（钉 SHA，禁浮动 main）：`curl -fsS -o ghcb https://raw.githubusercontent.com/Cloudbird-Software/.github/f72d9520706c8fca974d92456f65cae5c1412bb7/scripts/ghcb && chmod +x ghcb`（凭据用你自己的：`gh auth login` 或 `export GH_TOKEN=<PAT>`；`-f` 必带——404 时 curl 无 -f 仍退出 0，会把错误页当脚本落盘）
2. 找活：`bash ghcb next [owner/repo]` → 列 state:ready 卡（卡 issue 是唯一工作凭证，无卡不开工）
3. 认领：`bash ghcb claim <n> [owner/repo]` → 评论 /claim——conductor 转介 arbiter 原子 CAS 租约，先到先得；败者换下一张（`bash ghcb status <n>` 看持有者）
4. 开工：`make card-test CARD=<n>`（读卡 AC、测试先行）→ `make gates-pr`（本地复现 CI 关卡）
5. 提 PR：body 必带一行卡元数据 `Card: <owner>/<repo>#<n>`（`bash ghcb card-meta <n>` 生成；缺失=后续关卡 exit 3）
6. front-desk 命令（卡 issue 评论，conductor 转介 arbiter 处理）：/claim 认领 · /release 释放租约 · /retry 隔离回流

<!-- /entry-protocol -->

## 角色路由（按你的意图选路——ADR-0095；指引文件在 .github 治理仓 docs/agent/）

- 开 IR：feature 意图=本仓 issue（issue 即 IR，无需 PR）；治理意图=.github 仓 → [ROLE-IR.md](https://github.com/Cloudbird-Software/.github/blob/main/docs/agent/ROLE-IR.md)
- IR→spec：spec PR 必带测试设计逐类讨论（差分/属性/模糊…）+ holdout；**spec agent 不得直接实现** → [ROLE-SPEC.md](https://github.com/Cloudbird-Software/.github/blob/main/docs/agent/ROLE-SPEC.md)
- 实现卡片（PM 职责）：弱模型优先（子 agent / CNB 池）· fan-out=工具非流程 · 边做边推 PR · 3 次熔断自己接手 → [ROLE-IMPLEMENT.md](https://github.com/Cloudbird-Software/.github/blob/main/docs/agent/ROLE-IMPLEMENT.md)
- 验收 / 人类让你处理 issues：卡/IR 完成度检查 · bug 复现三值判定 → [ROLE-ACCEPT.md](https://github.com/Cloudbird-Software/.github/blob/main/docs/agent/ROLE-ACCEPT.md)
> 本文件是 specs/constitution.md 的执行摘要。全文以宪法为准。保持 <2000 tokens——它会注入每个 prompt。

## 系统是什么
小学语数英个性化练习平台：母题/实例→校验门→组卷（练习/诊断/测量）→渲染→作答→评分→参数标定的数据飞轮。模块化单体（**Go 1.25 + pgx + PostgreSQL 16**），学科=SubjectPack，学段=GradeBandPack。Python 运行时已退役：src/ 只读冻结归档（ADR-0007），冻结契约测试仍是跨语言黄金锚。

## 北极星（做取舍时看这里）
卖的是"知道孩子哪里弱、接下来练什么"的确定性，不是题。**真实作答参数是唯一不可复制的资产**——宁可少上一个功能，不可丢一条作答证据、不可写入一条脏数据。完成度以端到端闭环衡量，不以模块数衡量；终点是真实小学生拿到弱项报告，不是演示脚本跑通。

## 最高铁律（违反即 FAIL）
1. 三本账只增不改：内容版本 / 作答事件 / 校验签发。禁止 UPDATE/DELETE 历史。
2. 未过校验门的产物禁止入已发布区；绕过写入服务直写必须失败。
3. 作答交互与评分器只能来自注册表；学科包只能复用，禁止私造。
4. 核心域禁止 import 任何学科包/学段包（学科零特判）。
5. 参数按 source（先验/实测）与场景（practice/diagnosis/measurement）分开，禁止混估。
6. PII 只在保险库 schema；调用 LLM/TTS 前必须剥离，剥离失败一律 fail-closed（禁止降级放行开关）。
7. 禁止跨用户排名查询。
8. 每个请求必须有已认证主体；学生只能访问自己 alias 的数据；服务端凭证永不经 API 回传。
9. 服务层不自行 commit：一次业务写入=一个事务；写入端点必须幂等且对并发加锁；审计副作用走独立事务。
10. 生成式调用必须经 AI 总线并落台账（模型+版本+prompt 版本+成本+产物 id）；AI 评分必须把 model_version 写入 scoring_trace。
11. **宪法即测试**：任何"已强制/已保证"的说法必须在 specs/contracts/TRACEABILITY.md 强制实证矩阵里有可执行实证；没有实证就写"未实现"，不许宣称。

## 开发纪律（SDD）
- 无规格无代码：动手前必读任务卡中 spec 列出的全部文件；PR 必须引用任务卡 id。
- 验收标准是可执行脚本（make accept TASK=<id>），全绿才算完成；自评汇报不算数。
- 波内契约冻结：只增不改；要改契约，停下来升级给人类。
- 失败 2 次即升级：输出"已完成/失败点/已尝试/建议"，禁止继续盲目重试。

## 组织治理基线（Cloudbird-Software，ADR-0023）
- PR 唯一 required check = `gate`（ci.yml 聚合 CI-Workflows@v1 复用工作流，CI-1）；main 仅经 PR+squash 进入（BP-1，org ruleset main-protection）。
- agent 写仓唯一身份 = cloudbrid-agent App（AG-1）；automerge 仅限 dependabot 非 major 更新（SC-3）。
- 新增依赖须 owner 批（组织 languages.yaml#dependency_policy），禁 AGPL/GPL-3.0/SSPL。
- 治理索引（按需读）：[GOVERNANCE.yaml](https://github.com/Cloudbird-Software/.github/blob/main/governance/GOVERNANCE.yaml) · [REPOS.yaml](https://github.com/Cloudbird-Software/.github/blob/main/governance/REPOS.yaml) · [policy/](https://github.com/Cloudbird-Software/.github/tree/main/governance/policy)

## 代码与测试
- 测试先行：实现前先确认验收脚本存在且理解其期望；新功能必须带测试。
- 禁止删除/修改/弱化任何既有测试与断言；禁止用 skip 绕过失败。
- 禁止在 owner_module 之外创建/修改文件。
- 代码风格：类型标注完整；公开函数写 docstring；复杂逻辑写"为什么"注释；提交信息格式 `[<task-id>] <做了什么>`。
- 新依赖必须说明理由并更新锁定文件。

## 上下文与成本
- 上下文按需读取：只读任务卡列出的路径与必须理解的文件，禁止整库灌入；spec 只读锚点章节，不读整文件；本文件（core.md）已由工具注入时，提示词不再要求重读。
- 输出务实：代码与事实说话，不写奉承性总结；失败如实说。
- token 预算硬上限：接近预算时优先收尾并写清遗留，而非压缩质量。

## 并行与工作环境
- worktree 随用随清：任务合并后立即 `git worktree remove` 并删除本地/远端分支，禁止残留。
- 禁止修改 git config（remote/user/credential 等）；禁止 force push main。
- 临时日志与脚本（*.log、tmp_*、run_tests*）禁止入库；pytest 用 addopts 内 --basetemp=.pytest_tmp。

## 绝对禁令（复述，违反=任务失败+记录）
伪造成功 / 改测试通过验收 / 密钥进仓库或日志 / 删改历史数据 / 核心引学科包 / 绕过校验门 / **测试与实现互证**（伪造被测数据形态或预插生产不存在的占位行来骗过测试）/ **合规降级**（PII、家长授权、许可失败时放行）/ **无主体端点**（新端点未绑定认证主体与授权规则）。
