# W2 任务卡撰写委托简报（Scribe 专用）

> 用途：本简报是撰写 W2 任务卡的**唯一输入**。它将 W1 欠账与 W2 原定工作合并为 W2 总范围，并给出端到端出口定义。
> 读者：Scribe（任务卡撰写者）。撰写前必须读完 §1 全部输入文件。
> 日期：2026-07-27 ｜ 状态：经人类批准生效

---

## 1. 必读输入（写卡前全部阅读）

- `specs/constitution.md`（L0 宪法，全部）
- `specs/contracts/db/item-model.md`、`specs/contracts/events/response_event.md`、`specs/contracts/registries/*.yaml`（L1 冻结契约 v1.1.1）
- `ADR/母题与知识体系-工程架构设计方案v2.md`：§2.2（统一内容模型）、§4.1（生产域 A/B 线）、§4.2（知识域）、§4.3（校验域）、§4.6（交付域追溯）、§5.1（SubjectPack 契约）、§9（验收标准）
- `ADR/OPC-AI原生开发组织方案.md` §6.2/§6.3（W1/W2 原始定义）
- `tasks/templates/task-card.md`（任务卡模板）
- 现状代码：`src/core/`（models/events/gate/content）、`src/registry/`、`alembic/versions/0001–0005`、`scripts/wave-exit/w1.sh`——**W1 已建成的内容底座，W2 全部在其上扩展，禁止推翻重来**

## 2. W2 总范围（11 个工作流，含来源标注）

| # | 工作流 | 内容 | 来源 |
|---|---|---|---|
| S1 | **母题 DSL 与实例化引擎（A 线）** | 母题 DSL v1（YAML + JSON Schema + Linter，六大块 objective/slots/variation_axes/presentation/answer_program/distractor_rules）；安全表达式求值器（纯函数/无 IO/无循环）；确定性实例化 + 内容寻址（契约 §3 公式一）；受控变式引擎（六变式轴→槽子集重采样、其余冻结）+ **受控变式证书**（VariantCertificate：仅已认证变换算子的实例可标受控，AI 自由改写永标 UNPROVEN）；difficulty_relevant 槽变更触发难度重估任务 | W2 原定 |
| S2 | **校验门执行框架** | 验证器插件统一契约 `validate(artifact_ref, ctx) -> {pass/fail/review, evidence, confidence, validator_id+version, cost}`；门策略矩阵（学科包×产物类型的版本化验证器链，W2 先落 schema+数学/通用两条链）；门编排（任务队列异步、廉价先行、阻断短路）；gate_run/verdict 留痕写盘（W1 表已建）；门证书签发服务；**"绕过写入服务直写 serving 区必须在 DB 层失败"的自动化测试**（W1 门强制双层已有，本项补 serving 视图/角色层面的直写失败实证） | **W1 欠账**（执行框架）+ W2 原定 |
| S3 | **知识图谱底座** | 表：kp_node / kp_edge / kp_closure / relation_type / graph_release（契约锚点：架构 v2 §4.2、附录 A）；多维并行（dimension 一等公民）；类型化边（先修/易混淆/组成，relation_type 元数据 directed/transitive/acyclic/symmetric）；传递闭包扁平表 + 演进纪律（code 冻结语义、deprecated+supersede 链、graph_release 版本）；数学包 3–4 年级首批图谱种子数据（课标锚点，~100 节点级即可，全量属内容管线） | **W1 欠账** |
| S4 | **语料库底座 + 来源登记 + B 线装配线** | corpus 种子数据（数学函数库 v1）；`content/sources/` 来源登记表（许可字段强制，无登记不得入库的 CI 拦截）；B 线语料装配线 v1（框架模板+语料库填充，1 个数学题型实证：如单位换算） | W2 原定 + **W1 欠账**（来源登记） |
| S5 | **黄金数据集 v1** | 50 个真实母题（覆盖全部 10 种现役交互类型，数学 3–4 年级为主）+ 实例化期望输出；`tests/golden/` 回归测试（实例化结果与期望逐字节一致） | **W1 欠账** |
| S6 | **数学包（subject-math）** | 按 SubjectPack 契约（架构 v2 §5.1）落：数学函数库（槽位类型+安全函数，版本化全量单测）、数学验证器（双实现独立验算=验算器与实例化引擎不共享代码；可解性穷举/采样：除零/选项重复/干扰项=正解）、数学等价评分器实现（scorer.yaml 的 math_equivalence 契约实现：分数化简/单位换算/数值容差）、数学渲染组件（数轴/方格，W2 仅需静态卷需要的）、约束 overlay 预设 | W2 原定 |
| S7 | **语文包 A 线（subject-chinese，最小实证）** | 字词库+拼音字库管线接入（课标字词表+pypinyin 类可商用源）；**一个题型走通全链路**：看拼音写词语（A 线：词库驱动模板→实例化→语文验证器（规范字表校验）→入库→渲染）；混淆图数据（形近/音近种子，干扰项生成用） | W2 原定（缩减为最小实证） |
| S8 | **渲染底座 + 追溯码** | Render IR v1（内容-样式分离中间态：Item AST+版式提示）→ HTML/CSS → 无头 Chromium → PDF（试卷+解析册）；品牌模板 v1（页面模板+CSS）；追溯体系：paper / paper_item 表（迁移）、卷码+QR（码内只含卷 Spec ID+校验位，不含实例明文）、每题纠错短码、`paper_item` 固化"卷→题→版本"映射；周更批处理 v1（手工指定范围快照→组卷→渲染→发布，教学上下文服务属 W3） | W2 原定 |
| S9 | **只读 API + 教研工作台** | FastAPI 只读 API v1（题库/母题/版本/门状态查询，OpenAPI 产出）；教研工作台 v1（登录+题库 CRUD 只读，Web）→ v2 最小签发闭环（母题表单+按轴抽样预览 20 例+签发按钮写 publication；不做完整装配编排） | **W1 欠账**（API/工作台骨架）+ W2 原定（签发闭环） |
| S10 | **W1 遗留收尾** | ①`publish_corpus_asset` 写门字段（对齐 material 两段式，Verifier 遗留）；②测试隔离：conftest 引入事务回滚或独立测试 schema（消除测试数据互染）；③`pip-compile` 重跑对齐 requirements.txt 头注释；④遥测：每张卡的模型/成本/门信号 JSONL 落 `.agent/telemetry/`（W2 出口产出产能报告） | **W1 欠账** |
| S11 | **W2 出口脚本 + CI 集成** | `scripts/wave-exit/w2.sh`（出口演示+全部门禁检查）；nightly 加入 W2 检查项；Makefile 加 `demo-w2` | W2 原定 |

## 3. W2 端到端出口定义（全部完成为止，机器可验）

**E2E-1（主链路，S1 后端就绪）**：教研在工作台审阅并签发一个数学母题 → 实例化引擎产出实例 → 实例过校验门（结构→许可→查重占位→数学双实现验算）→ 入库 published → 周更批处理生成静态卷（含卷码 QR+题短码）→ PDF 试卷+解析册可下载。**全程任一打印题码可回溯：题→item_version→生产谱系→门证书→签发人。**

**E2E-2（DSL 实证）**：DSL 定义 ≥3 个母题（覆盖 ≥3 种不同交互类型）→ 实例化 → 过门 → 入库 → 只读 API 可查到（含谱系与门状态）。

**E2E-3（黄金数据集回归）**：50 母题实例化期望输出回归全绿（`tests/golden/`）。

**E2E-4（语文最小链路）**：看拼音写词语题型走通"模板→实例化→语文验证器→入库→渲染进 PDF"。

**E2E-5（门物理阻断）**：自动化测试证明——绕过内容写入服务直写 serving 视图/表，在数据库层失败（角色权限或触发器）。

**E2E-6（受控变式证书）**：对一个母题按单轴生成变式，产出 VariantCertificate（含目标不变性证据：objective 为槽值显式函数+技能集合恒等校验）；AI 自由改写产物正确标记 UNPROVEN。

**E2E-7（矩阵实证，降级口径）**：10 种现役交互 × 6 种现役评分的组合空间，每类至少 1 个真实题型样本走通"定义→生产→门→入库→渲染"（C/D 生产线仅骨架样本，组卷/作答/评分/估计全链路属 W3+，不在 W2 出口）。**注：OPC §6.2 原"每格实证"与"30 题型全链路"经人类裁决降级为本口径。**

**E2E-8（遥测产能）**：W2 全部任务卡的（模型， 任务类型， 门信号， token 成本）JSONL 落 `.agent/telemetry/`；`python tools/opc dashboard` 产出 W2 产能报告（吞吐/一次通过率/单位成本），作为 W3 重排依据。

**E2E-9（出口脚本）**：`make demo-w2`（= `bash scripts/wave-exit/w2.sh`）全绿，含 E2E-1 的现场演示（生成一份真实 PDF 试卷）。

**E2E-10（不退化）**：W0/W1 出口脚本（w0.sh、w1.sh）持续全绿；全部测试（contract/golden/golden-path/unit）绿；CI 三道门禁绿。

## 4. 写卡要求（Scribe 必须遵守）

1. **粒度**：单卡 = 单 agent 一次会话可完成且可独立验收（2–6 小时 agent 工时）；大工作流必须拆卡（S1/S8 预计各拆 4–6 张）。
2. **依赖图**：S3/S4 与 S1 并行可启动；S5/S6 依赖 S1 引擎；S2 依赖 W1 gate 表（已就绪）可与 S1 并行；S8 依赖 S1+S6；S9 依赖 S2（门状态可查）；S11 依赖全部。允许 W2 内分两个子波：**W2a（引擎与地基：S1/S2/S3/S4/S10）→ W2b（学科与出口：S5/S6/S7/S8/S9/S11）**，子波间不设硬门禁。
3. **每张卡必填**：spec（精确到文件#章节）、context_paths、deliverables（文件级）、acceptance（`make accept TASK=<id>`，验收脚本必须先于实现存在）、model_floor（引擎/门/寻址=S→T0，其余 T1，文书/数据 T2）、token_budget、owner_module（互斥）、depends_on、non_goals（必填，防镀金）、escalation。
4. **学科零特判**：任何卡的 deliverables 不得让核心域 import 学科包（CI 扫描强制）；学科逻辑只能进 `src/packs/subject-math|chinese/`。
5. **非目标（W2 全域）**：C/D 线完整工坊（素材工坊/命题工坊全功能）、在线练习（S3）、诊断组卷（S4）、测量（S5）、自适应、复习排程、TTS/音频、小程序端、组卷引擎（CP-SAT）——组卷在 W2 仅为"按约束清单选题的确定性批处理"，求解器属 W3。
6. 卡写完后：`python tools/opc board` 校验通过（owner 互斥+依赖存在），人类抽检 20% 后放行。

## 5. 规模提示（供人类决策，不约束 Scribe）

合并后 W2 估算约 45–60 张卡（原 W2 口径 4–5 周×8–10 agent，叠加欠账后偏紧）。若实测产能不足，降级顺序：S7 语文实证 → S9 工作台 v2 简化 → S5 黄金数据集减半（25 母题）。**S1/S2/E2E-1/E2E-5 不可降级**（引擎与门是全线根基）。
