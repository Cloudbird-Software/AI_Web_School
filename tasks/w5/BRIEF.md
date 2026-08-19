# W5 任务卡撰写简报（信任硬化波 · 宪法兑现）

> 目标：把宪法从"文档承诺"变成"物理强制"。W5 出口 = 强制实证矩阵首次全绿，三类攻击（伪造证书发布 / 匿名冒充学生 / 绕过写入服务直写）全部被 DB 或认证层拒绝。
> 输入：W0–W4 全部成果；specs/constitution.md v2.0（A8/A9/A10、D9/D10/D11、P9、X11–X13）；specs/adr/0003；《2026-07-30 代码审查报告四份.txt》复验清单。
> 铁律：**W5 期间不接受任何新功能任务**。理由见 tasks/roadmap.md §一——三本账只增不改，脏数据不可回滚，比没数据更贵。

## W5 工作流

| # | 工作流 | 内容 | 依据 |
|---|---|---|---|
| S1 | 门与账的物理强制 | 内容版本账补 append-only 触发器；内容表 `gate_certificate_id` 补 FK 并落地失败留痕方案；发布服务证书验真（存在/类型/artifact_ref 匹配）；内容寻址缺参 fail-loud（禁止退化 UUID）；会话题序不可变 | D1 D2 D3 A8 |
| S2 | 认证与主体绑定 | 认证框架（主体类型：学生 alias / 教研 / 运维 / 机构）；全端点接入并校验主体与 path 中 alias 一致；服务端凭证不落库不回传；CORS 白名单、限流、异常映射不泄露内部信息；渲染出口沙箱 | D9 A9 X13 |
| S3 | 合规硬约束落地 | 家长授权接入在线入口（会话/诊断/测量），未授权 403；授权账版本原子分配与唯一约束；PII 保险库角色与审计独立事务；姓名脱敏边界修复；AI 总线 PII fail-closed（删除 bypass 开关）与台账全覆盖；TTS 同规格 | D7 D10 X12 |
| S4 | 事务与并发正确性 | 领域服务不自行 commit，事务边界上移；作答提交幂等键 + 行锁 + 并发测试；估计器指针切换并发安全 | D11 A9 |
| S5 | 校验门缺陷修复 | 查重验证器走真实内容 digest 列（禁止用 digest 伪造 ID 骗测试）；语篇事实核查干净语篇必须 pass；评分器契约校验；等价判定全角表补全 | A2 D4 X11 |
| S6 | 测试与 CI 可信度 | 迁移可逆全量验证（downgrade base）；冻结契约守卫遍历 FROZEN.txt 全量；无排名静态扫描盲区；黄金路径补至 ≥10 种交互类型真实端到端；并发/门 FK/认证/API 集成覆盖空洞；打包正确性 | P3 P4 A8 |
| S7 | 实证矩阵与出口 | `specs/contracts/TRACEABILITY.md` 新增「强制实证矩阵」（宪法条款 ↔ 可执行实证）+ CI 校验；认证引入的 API 契约变更申请（ADR-0004）；w5.sh 出口 | A8 P5 P9 |

## W5 端到端出口定义

- **E2E-1（伪证书）**：以最高权限直接 `INSERT/UPDATE` 一条 `status='published'`、`gate_certificate_id='cert_FAKE'` 的内容版本 → 被 FK 拒绝；换成合法但 artifact 不匹配的证书 → 被写入服务拒绝。
- **E2E-2（改历史）**：对 `item_version` / `material_version` / `corpus_version` / `item_template_version` 执行 UPDATE 与 DELETE（含 `WHERE FALSE`）→ 全部被 append-only 触发器拒绝。
- **E2E-3（冒充）**：不带凭证 / 带他人凭证访问 `POST /sessions`、`POST /sessions/{id}/responses`、`GET /reports/weakness/{alias}` → 401/403；任何公开端点响应体中不出现服务端凭证。
- **E2E-4（未授权未成年人）**：无有效家长授权的 alias 开练习/诊断/测量会话 → 403，且 `response_event` 零写入。
- **E2E-5（PII）**：构造含姓名/手机号的 prompt 与 TTS 文本，令剥离器抛异常 → 调用被拒绝（fail-closed），台账记录失败原因；全仓不存在 bypass 开关。
- **E2E-6（事务与并发）**：同一 session 并发提交同一题 10 次 → 恰好 1 条 `response_event`、`current_index` 恰好推进 1；评分失败时事件与会话状态同进同退。
- **E2E-7（AI 可回放）**：一次 AI 量规评分 → `scoring_trace` 含 model_version 与 prompt 版本，AI 台账可按 item_revision 归集；替换模型版本后历史报告仍引用当时版本。
- **E2E-8（查重真实生效）**：写入两条内容完全相同的内容版本 → 第二条被查重验证器判 review/fail；测试不得通过伪造 ID 制造命中。
- **E2E-9（CI 可信度）**：`alembic downgrade base` 后 `upgrade head` 成功；任意冻结契约被修改 → contract-watch 红；黄金路径覆盖 ≥10 种交互类型且全绿。
- **E2E-10（实证矩阵）**：`TRACEABILITY.md` 强制实证矩阵中每条宪法条款都有实证路径，CI 校验矩阵完整性；矩阵缺项即红。
- **E2E-11（不退化）**：W0–W4 出口脚本与既有测试全绿，测试收集数不低于 W4 基线。

## 非目标（W5 明确排除）

新交互类型、新学科包内容、新组卷策略、学生端 UI、支付与商业化、多租户、自适应算法、性能优化（除非是安全/正确性修复的副产品）。**任何"顺手加个功能"的 PR 一律拒绝。**
