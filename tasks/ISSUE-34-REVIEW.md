# Issue #34 开发规划审查报告与修正案

> 审查人：owner（经 agent 执行）· 日期：2026-08-20 · 范围：issue #34 全文（三项定案 / 目标状态 / 技术基线 / harness / 基准策略 / 题型三档 / 波次重排 / 风险）
> 结论：**规划总体成立，批准继续；但存在 1 个致命缺口与 5 处不可判定项，按本修正案修订后生效。**
> 落地载体：本文件 + `specs/test-freeze/` + `tests/holdout/` + T-W5-034 + issue #34 §十一（已追加到 issue 正文）。

## 一、总体判断

三项定案（D-A LLM 生产资料 / D-B 摒弃人工标注 / D-C Go+BAML 重写）逻辑自洽，治理闭环完整（ADR、依赖审批、gate 规则齐备），波次重排理由（先可信再能力后规模）成立。风险表覆盖了主要技术风险。**规划的骨架不需要动，需要补的是验收的牙齿。**

## 二、致命缺口：测试体系缺席（本次修订的核心）

原规划对"如何判定完成"只有方向性描述（出口证据列），三处硬缺口：

1. **任务级验收测试没有提前写死**：任务卡模板中 `accept_script` 是"可选"，意味着验收标准可以和实现同时诞生——这在 agent 开发模式下等于让运动员兼任裁判（违反 X1/X11 的精神）。
2. **测试资产无机器级防篡改保护**：既有 `tests/`、accept 脚本、波次出口脚本只有纪律条文保护；FROZEN.txt 只覆盖 specs/contracts。agent 完全可以"修测试过验收"。
3. **没有基于人类意图的端到端 Holdout 测试**：各波出口证据由开发者围绕实现编写，缺少一份在实现开始前由人类写死、只问效果不问实现的最终判据——尤其缺 issue #34 总目标（"平台完全可运行且跑通题目生产"）的总体验收。

**修订（已落地）**：

- `accept_script` 由可选改**必填**，必须先于实现存在（`tasks/templates/task-card.md`）。
- 新增 `specs/test-freeze/`：哈希清单 + `tools/ci/check_test_freeze.py` + CI `test-freeze` job（并入唯一 required check `gate`）+ CODEOWNERS 显式归属 + `[TEST-FREEZE-APPROVE]` 人类例外。开发 agent 对受保护测试资产**只能跑，不能改**。
- 新增 `tests/holdout/`：`w5r/w6/w7/w8/final.md` 五份人类意图效果测试，`make holdout WAVE=<name>` 执行；波次出口 = wave-exit 脚本全绿 **且** holdout machine 项全绿 **且** human 项 owner 签字。
- **owner 待办（人工，不可委托）**：开启 `require_code_owner_review`（机器标记可伪造，人审是最终防线）——验证方式已写入 `tests/holdout/w5r.md` H-W5R-13。

## 三、不可判定项（出口证据必须可判定，逐条修订）

| # | 原文 | 问题 | 修订（判定口径） |
|---|---|---|---|
| 1 | W6「10 个数学母题各产出 ≥30 结构互异合格实例」 | "结构互异"无操作定义 | **结构互异 = 同母题下已发布实例的 content 摘要两两不同（唯一率 100%）**；机器判定见 `tests/holdout/w6.md` H-W6-1 |
| 2 | §4.2「评价者在共识基准集上达标才允许注册」 | "达标"无阈值 | **达标 = 跨模型共识一致率 ≥0.90**（基准集版本化，阈值随 W6 BRIEF 冻结；准入留痕要求见 H-W6-4/H-W6-8） |
| 3 | §4.3「一致率跌破阈值自动降回 shadow」 | 阈值与窗口未定 | **阈值 = 周滑动窗口内跨版本一致率 <0.85**，触发自动降级并留痕（在岗性验证见 H-W8-3） |
| 4 | W7「≥10 名真实小学生」 | 机器不可判定 | 拆分为 machine（链路通畅）+ human（owner 按台账逐人核对）双轨，见 `tests/holdout/w7.md` H-W7-1/H-W7-5 |
| 5 | §九「LLM 成本预算硬顶 + 超限熔断」 | 熔断行为未定义 | **预算硬顶耗尽后 AI 总线拒绝新调用（429/503）**，故障演练式验证见 H-W6-6 |

## 四、次要观察（不阻塞，记入对应波次任务卡时消化）

- §十 spike 无出口标准：建议 T-W5-030 验收即"最小 .baml 函数走通 生成→golden test→gate"的可复现命令（该卡已存在，验收脚本已冻结）。
- 契约测试 Python→Go 移植的"全绿"判定依赖 REANCHORING.md 对照完整性；E2E-11 已兜底，不再单独立项。
- BAML golden 数据集（`tools/golden/`）已纳入 `baml-golden-check`；其期望文件如需加强保护，后续可追加进 `MANIFEST.txt`（走 §五例外流程）。

## 五、修订动作清单（本次提交全部落盘）

| 动作 | 位置 |
|---|---|
| 审查与修正案（本文件） | `tasks/ISSUE-34-REVIEW.md` |
| 测试冻结规则 + 清单 + 哈希 | `specs/test-freeze/` |
| 冻结校验器 + holdout 执行器 | `tools/ci/check_test_freeze.py`、`tools/ci/run_holdout.py` |
| CI 门禁并入 gate | `.github/workflows/ci.yml`（`test-freeze` job）——**暂缓**：App 缺 Workflows 权限（GitHub 平台限制），已在 Cloudbird-Software/.github#102 立项；修复后由 agent 回本仓回迁 diff（diff 全文附在该 issue 中），回迁后 `tools/accept/t_w5_034.sh` 第 4 条转绿 |
| CODEOWNERS 补 `/tests/` `/scripts/` | `.github/CODEOWNERS` |
| `make test-freeze-check` / `make holdout` | `Makefile` |
| 任务卡模板 accept_script 必填 | `tasks/templates/task-card.md` |
| 波次出口挂接 holdout | `tasks/roadmap.md` §二 |
| 波次+总体 Holdout 测试 | `tests/holdout/{w5r,w6,w7,w8,final}.md` |
| issue #34 正文追加 §十一 | GitHub issue #34（已更新） |
