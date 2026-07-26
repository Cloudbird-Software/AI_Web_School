# DevOS 骨架建设手册
## ——开箱即建的骨架文件包 + 人类审查指南

> 本手册配套 `muti-platform-skeleton.zip`（解压即得 `muti-platform/` 完整骨架）。
> 本包落实《OPC·AI原生开发组织方案》的附录 B–F 全部内容：角色 prompt（B）、规则文件（C）、模型基准赛（D）、遥测仪表盘（E）、波次出口脚本（F），以及 W0 所需的全部基建文件。
> **边界声明**：本包到"可以开始派发任务"为止。完成 §3 全部步骤且审查全部通过后，才允许在 Trae / Claude Code 中启动 agent 工作（§4 明确启动线）。

---

## 1. 文件清单与审查优先级

### P0 —— 必须逐字审查（决定 agent 行为上限，错了全盘皆输）

| 文件 | 是什么 | 审什么 | 怎么审 |
|---|---|---|---|
| `specs/constitution.md` | **L0 架构宪法**。所有 agent 的最高约束 | ①四部分（公理/铁律/纪律/禁令）是否与架构 v2 一致、有无你不同意的条文；②禁令 X1–X10 是否够狠够明确 | **逐条过**：每条问自己"我愿不愿意让 10 个 agent 把这条当真理执行"。任何一条不理解或不认同 → 改掉或删掉再开工。agent 不会质疑宪法，只会放大宪法 |
| `.agent/roles/builder.md` | Builder 行为模板 | 禁止事项是否完备；失败报告格式是否强制诚实 | 重点看"禁止事项"与"失败时"两节。试想你最想防的 agent 恶习（伪造成功/改测试/越界改文件）是否都被明文禁止 |
| `.agent/roles/verifier.md` | Verifier 行为模板 | 独立性条款（不同家族/禁读汇报）；测试强度审查是否入列 | 确认输出 schema 里每条结论都要带 `文件:行号` 证据——没证据的 PASS 一文不值 |
| `.agent/roles/judge.md`、`scribe.md` | 裁决/文书模板 | Judge 只裁决不改代码；Scribe 不碰 src/ | 快速通读，确认权限边界 |

### P1 —— 认真通读（影响成本与日常运转）

| 文件 | 审什么 |
|---|---|
| `.agent/rules/core.md` | 它会注入**每一个** prompt：①内容是否准确（宪法摘要）；②长度——每多 1000 tokens，每次调用都多花钱。觉得长就删，它是给你控制 token 成本的阀门 |
| `.agent/routing.yaml` | 三个梯队的占位模型换成你实际有 key 的模型；`total_hard_cap_cny` 改成你的真实预算 |
| `tests/model-bench/README.md` | 30 题任务集是否代表你的真实工作（B/C 类是核心）；确认它放私有位置 |
| `.agent/telemetry/README.md` | 指标阈值是否符合预期（如升级率 ≤25%）；JSONL 字段是否够用 |
| `tasks/w0/T-W0-001~006.md` | 每张卡的验收标准是否你真的认；T-W0-004（L1 契约）标注了"人类逐行审查后才冻结"——这是全项目最重要的一张卡 |

### P2 —— 跑通即可（有问题会在运行中暴露）

`Makefile`、`docker-compose.yml`、`.env.example`、`tools/opc`、`tools/make_accept.sh`、`tools/sync_rules.sh`、三个 CI workflow、`scripts/wave-exit/w0.sh`、`tasks/board.md`、`requirements-dev.txt`。

**审查方法总纲（三遍法）**：
1. **通读**（P0 逐字，P1 通读，P2 扫读）；
2. **对照**：拿架构 v2 对照 constitution.md 的 A1–A7 与 D1–D8——这是骨架与架构的唯一硬连接，错一处后面全歪；
3. **提问测试**：在 Trae 里打开文件问 AI"这条原则在防止什么失败？"——AI 答得清，说明条文可执行；答得含糊，说明条文要改具体。疑问不要带进口袋，全部写进 `specs/adr/` 作为待决项。

---

## 2. 包内已实现的关键机制（对应开发方案附录）

| 附录 | 文件 | 状态 |
|---|---|---|
| B 角色 prompt | `.agent/roles/{builder,verifier,judge,scribe}.md` | ✅ 完整可用，含输出 schema 与禁止事项 |
| C 规则文件 | `.agent/rules/core.md`（源）+ 已同步生成 `.trae/rules/core.md`、`CLAUDE.md`、`AGENTS.md` | ✅ 完整可用；改源后跑 `make sync-rules` |
| D 模型基准赛 | `tests/model-bench/README.md`（30 题全定义+评分+路由产出规则）+ `suite.yaml` | ✅ 设计完整；题目本体实现是 W1 的一张任务卡 |
| E 遥测仪表盘 | `.agent/telemetry/README.md`（JSONL schema+12 项指标+周报模板+对账机制）+ `tools/opc dashboard` | ✅ 完整可用 |
| F 波次出口 | `scripts/wave-exit/w0.sh`（7 项出口检查，任一失败即不通过） | ✅ 可用；w1–w4 脚本随各波生成 |
| 任务卡系统 | `tasks/templates/task-card.md` + `tasks/board.md` + `tasks/w0/` 六张种子卡 | ✅ 可直接派发 |
| CI 三道门禁 | `pr-check.yml`（任务卡引用/泄密/反削弱/学科边界/规则同步/测试）+ `contract.yml`（冻结契约）+ `nightly.yml`（迁移演练+黄金路径+失败建 issue） | ✅ 配置完整 |

---

## 3. 建骨架操作序列（照做即可，约半天）

```bash
# ① 解压并初始化
unzip muti-platform-skeleton.zip && cd muti-platform
git init && git add -A && git commit -m "chore: DevOS 骨架"

# ② 配置环境
cp .env.example .env    # 填入数据库/MinIO 密码（本地开发随意）；模型 key 先不填也行

# ③ 审查（按 §1 的 P0→P1→P2 顺序，不要跳）
#    ——这是本序列中唯一无法加速的部分，预留 2–3 小时

# ④ 环境与规则联通
make bootstrap          # 需 Docker；三项 ✅ 即通过
make sync-rules         # 重新生成并校验 IDE 规则文件

# ⑤ 骨架自检
python tools/opc board  # 任务板校验（应输出 ✅ 6 张卡）
python -m pytest tests/ # 占位烟测绿
make demo-w0            # W0 出口验收（7 项全过）

# ⑥ 托管与 CI
git remote add origin <你的私有仓库> && git push -u origin main
# 到 GitHub 仓库 Settings → Secrets 配置 CI 所需密钥（如需要）

# ⑦ 模型基准赛（可选但强烈建议，决定路由表质量）
#    题目本体实现是 W1 任务；也可先手工用 3–5 题快速校准三个梯队

# ⑧ 终点检查（启动线，见 §4）
```

**注意**：`make demo-w0` 中"迁移演练"项在 T-W0-005（Alembic）完成前会失败——这是设计使然：出口脚本同时充当 W0 进度仪表盘。骨架初始状态下它应报"迁移演练失败"，其余 6 项全绿。

---

## 4. 启动线：什么时候才允许开 agent

同时满足以下五条，才允许派发第一张任务卡（在此之前，不让任何 agent 写任何代码）：

1. ✅ P0 文件全部逐字审过，你签字（在 `specs/adr/` 写一条"宪法 v1.0 批准"即完成签字）；
2. ✅ `make bootstrap` 与 `make sync-rules` 全绿；
3. ✅ 任务板 `python tools/opc board` 校验通过，且 W0 六张卡你已读过、认可验收标准；
4. ✅ 路由表填入了你真实可用的模型与预算硬顶；
5. ✅ CI 已连通（推一个空 PR 验证 pr-check 真实运行）。

**第一张派发的卡**：T-W0-001（环境 bootstrap）——它风险最低、且验证"任务卡→agent→PR→CI→Verifier"整条流水线是否真实运转。**第一周内，你的每一个操作都慢半拍**：每张 PR 都亲自看 diff、亲自跑 `make accept`，第二周起再逐步放手到"只看信号"。

---

## 5. 常见问题

- **rules 文件改了哪里生效？** 只改 `.agent/rules/core.md`，然后 `make sync-rules`。直接改 CLAUDE.md 会被 CI 拦（一致性检查）。
- **想加任务卡？** `python tools/opc new-task --wave w1 --title "..."`，或让 Scribe 按规格批量生成后你抽检 20%。
- **模型没 key 怎么起步？** routing.yaml 全部梯队先指向你唯一有的模型也能跑（梯队降级但流程完整），拿到更多 key 后再跑基准赛分化梯队。
- **这个包与《OPC开发组织方案》什么关系？** 方案是"为什么"，本包是"拿来即用"。方案附录 B–F 的声明全部由本包文件兑现；若两者有出入，以本包文件为准。

*手册完。骨架就位、审查通过后，你就站在启动线上：此后的一切，按任务板来。*
