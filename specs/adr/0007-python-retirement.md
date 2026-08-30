# ADR-0007: Python 运行时退役声明——Go 单轨生效，src/ 只读冻结归档（PyR-RETIRE）

> 状态：**已批准（Accepted）**——执行 ADR-0004 §四预授权的退役条款（2026-08-30）。
> 关联：任务卡 [PyR-RETIRE #142](https://github.com/Cloudbird-Software/AI_Web_School/issues/142) ·
> 依据 ADR-0004 §四「Go 版 E2E 全绿后归档（历史保留，不删除）」· issue #34 §一 layers.application
> allowed = go(default)/typescript(frontend only)。

## 一、背景与退役条件判定

ADR-0004 确立「重写而非移植」策略时对 Python 侧的处置是两段式：rewrite 期间
**只读冻结、不接新功能**（安全修复除外）；**Go 版 E2E 全绿后归档**（历史保留，
不删除——作答证据与审计不可抹除）。

退役条件已满足（2026-08-30 判定）：

| 条件 | 证据 |
| --- | --- |
| Go 业务对等全绿 | GO-RW-000…015（#125–#140）全部关闭：会话全链路/评分/组卷/渲染/报告/复习/标定/校验门/知识图谱/实例化/句子重组/运维域 Go 实现落地 |
| 内容与生产线对等 | PyR 系列移植（#116–#124）：models/assembly/render/datastat/production/audio/instantiation 与冻结实现逐字节互验 |
| 运行时面接线 | GO-RW-001（#141）内容查询四端点去 501；cmd/school 生产装配 |
| 门禁面 Go 化 | GO-1/GO-2/GO-4/X6/BAML-1/SQL-1/SQL-2（gofmt/errcheck/race/import-boundary/baml golden/sqlc diff）进 `make check` |
| main 持续绿 | gate/org-gate/adversary 三必需检查通过（squash-only，PR 唯一入口） |

## 二、决定

1. **Go 1.25 是唯一应用运行时**。`layers.application` 允许清单（go / typescript
   frontend only）自此为排他事实；新功能一律 Go（学生端 W7 为 TypeScript，另立）。
2. **src/ 整目录转退役归档**：api/core/packs/registry/workbench 保持树内只读，
   **不删除任何文件**（ADR-0004：历史保留——作答证据与审计不可抹除；git 历史
   与树内冻结副本同为取证面）。禁止接新功能；安全修复为例外，且必须连带重签
   src 冻结清单（见第四条）。
3. **机器强制**：src/** 逐文件 SHA256 钉扎（specs/src-freeze/MANIFEST.sha256）+
   Go 校验器 tools/srcfreeze（任何增/删/改即红；`--resign` 为人类例外重签通道，
   对齐 specs/test-freeze/ §五人类例外流程的形态）。挂载进 `make check`。
4. **退役范围界定（如实声明，防过度宣称）**——以下 Python 内容**不在**退役范围：
   - `alembic/` 迁移链：DB schema 治理双轨期组成（ADR-0004 §四），migrate-go-check
     以 alembic/golang-migrate parity 为验收面；
   - `tests/`（contract/golden/golden-path/holdout）：冻结契约测试与黄金数据集是
     **跨语言黄金锚**（Go 实现的地面真值来源），受 specs/test-freeze/ 治理保护；
   - `tools/*.py` 门禁胶水（check_sources/check_pairs/migrate_check/baml_golden/
     check_test_freeze/run_holdout 等）与 `requirements*.txt`：CI 门禁基础设施，
     非业务服务，其退役（Go 化）另行立项，不随本 ADR 宣称。
   故本 ADR 宣称的退役对象是 **Python 业务服务运行时（src/）**，不是「仓库内
   一切 Python 字节」。

## 三、后果

- 正面：双轨期心智负担终止——「改 Python 还是改 Go」的取舍题消失；src/ 漂移
  从纪律约束升级为机器强制（与 test-freeze 同构的确定性红绿）；README/规则
  摘要与事实一致，新进 agent 不再被「系统是什么」误导。
- 代价与风险：src/ 冻结副本长期留存仓库（体积成本，换取审计不可抹除——宪法
  级取舍，接受）；门禁胶水仍为 Python（技术债如实挂账，见第四条）；alembic/
  golang-migrate 双轨继续，直到迁移链单轨化另行决策。
- 回退路径：无。退役是单向门（one-way door）——src/ 冻结副本保证取证可回读，
  但不接受服务流量回切；若极端情形需回切，属新 ADR 立项。

## 四、执行与例外流程

- 本次退役以 [PyR-RETIRE #142](https://github.com/Cloudbird-Software/AI_Web_School/issues/142)
  承载：ADR-0007 + 规则/文档同步 + src 冻结机器强制，一个 PR 一件事分两个 PR。
- src/ 安全修复例外：修复 PR 必须带 `--resign` 重签的 MANIFEST.sha256 与修复
  理由（引用本节），reviewer 按安全修复逐行审；除安全外的一切 src/ 变更一律红。
