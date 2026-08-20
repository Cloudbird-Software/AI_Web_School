# 测试冻结治理（Test Freeze）

> 依据：宪法 A8（宪法即测试）/ P9（实证矩阵常绿）/ X1（禁删改弱化测试）/ X11（禁测试与实现互证）；issue #34 §十一修正案；任务卡 T-W5-034。
> 一句话：**测试是人类的验收意志，不是开发 agent 可以商量的对象。** 本目录定义"哪些测试资产被冻结、机器如何强制、人类如何例外"。

## 一、保护范围（MANIFEST.txt）

`MANIFEST.txt` 每行一个受保护路径（目录以 `/` 结尾，递归生效）：

| 路径 | 保护对象 | 理由 |
|---|---|---|
| `tests/holdout/` | 波次与总体端到端 Holdout 测试（人类意图的效果测试） | 验收意志本体，开发 agent 只能跑，不能改 |
| `tests/contract/` | 冻结契约的机器校验 | 契约守卫本身不可被动摇 |
| `tests/golden/` | 黄金回归用例与加载器 | 期望值即防线（新增用例见 §三例外） |
| `tools/accept/` | 任务专属验收脚本（先于实现存在） | 任务级验收测试提前写死的载体 |
| `scripts/wave-exit/` | 波次出口脚本 | 波次出口口径不可被实现方修改 |
| `tools/ci/check_test_freeze.py` / `tools/ci/run_holdout.py` | 守卫自身 | 守卫必须自我冻结，否则冻结是假的 |
| `specs/test-freeze/README.md` | 本规则 | 规则变更必须留痕且经人批 |

## 二、机器强制（三层）

1. **哈希冻结**：`MANIFEST.sha256` 记录每个受保护文件的 sha256。`python tools/ci/check_test_freeze.py` 校验：
   - 任何受保护文件内容变化、被删除 → 红；
   - 受保护目录下出现未登记的新文件 → 红（例外见 §三）。
2. **清单变更拦截**：CI（`ci.yml` 的 `test-freeze` job，已并入唯一 required check `gate`）在 PR 上检查 `MANIFEST.txt` / `MANIFEST.sha256` 被修改时，要求 PR 任一提交信息含 `[TEST-FREEZE-APPROVE]` 标记——该标记语义 = **人类已审阅并批准本次测试资产变更**（与既有 `[FROZEN-APPROVE]` 契约例外同构）。
3. **人类合并闸**：`/tests/`、`/scripts/`、`/tools/`、`/specs/` 均在 CODEOWNERS 显式单列；agent 写仓身份（cloudbrid-agent App）不在任何 owner 列表。**owner 须一次性完成动作（人工，不可委托）**：在 org ruleset `main-protection` 或本仓分支保护中开启 `require_code_owner_review`。开启后任何触碰受保护路径的 PR 必须 owner 本人批准，机器标记无法伪造人类批准。

> 诚实声明：`[TEST-FREEZE-APPROVE]` 标记本身可被 agent 写入提交信息，它只是"无标记必拦"的机器闸门；**真正的最终防线是第 3 层的人审**。这与既有 `[FROZEN-APPROVE]` 的威胁模型一致。

## 三、唯一例外：纯新增的黄金用例

`tests/golden/items/**` 下**新增**用例文件允许不走红：新增一个自包含用例（含自身 `expected_*` 哈希）只会加强防线，不可能削弱既有断言。其余一切新增（新 accept 脚本、新 holdout 条目、新出口脚本、新契约测试）都必须登记进 `MANIFEST.sha256`，即触发 §二.2 的人类标记流程——这正是"验收测试提前由人类写死"的落地形态：波次启动时由人类（或人类审阅的规划 PR）批量物化。

## 四、开发 agent 的允许动作

- 允许：`make accept TASK=<id>`、`make holdout WAVE=<w5r|w6|w7|w8|final>`、`make test-freeze-check`、`pytest tests/...` —— 只跑，不改。
- 禁止：修改/删除/重命名任何受保护文件；在受保护目录新增文件而不登记清单；以任何方式让失败测试"变绿"（X1/X11，违反即任务失败并记录）。
- 测试与实现冲突时：停下来升级给人类（escalation），永远不允许"修测试"。

## 五、人类例外流程（确实需要改测试时）

1. 人（owner）在 PR 中修改受保护文件并重签清单：`python tools/ci/check_test_freeze.py --resign`（重算 `MANIFEST.sha256`）。
2. 提交信息含 `[TEST-FREEZE-APPROVE]`，PR 描述说明变更理由与影响面。
3. owner 本人 approve + squash 合并。三步缺一不可。
