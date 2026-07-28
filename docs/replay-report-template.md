# 年度全量重放首演报告模板

> 架构 v2 §4.7 / 宪法 D6 / R-D-05：估计器可替换 + 可重算实证（E2E-8 承载卡）。
> 本模板由 `scripts/jobs/annual_replay_report.py` 渲染填充。

## 用法

```bash
# 生成 Markdown 报告（默认）
python scripts/jobs/annual_replay_report.py --scope practice \
    --run-label annual-replay-2026 \
    --output docs/replay-report-2026.md

# 生成 JSON 报告（机器可读）
python scripts/jobs/annual_replay_report.py --scope practice --json \
    --output docs/replay-report-2026.json
```

## 报告段落结构（验收 #1-#3 对应）

| 段落 | 验收点 | 内容 | 数据来源 |
|------|--------|------|----------|
| 元信息 | — | 场景 / 批次标签 / 生成时刻 | CLI 参数 |
| 摘要 | #1 | 事件总数 / 重算成功 / 跳过 / 失败 / 一致性率 / 摘要哈希 / 活跃版本 / 并存题目数 | replay_all + estimator_run + item_param |
| ActiveModelPointer 版本映射 | #2 | 当前活跃版本 / 切换前旧版本 / 切换后新版本 / 版本历史表 | `estimator_run` 表 |
| 重算参数分布与差异统计 | #1 | 旧版本参数摘要 / 新版本参数摘要 / 参数差异分布（difficulty delta） | `replay_all.old_param_summary` / `new_param_summary` / `param_diff_distribution` |
| 一致性率 | #1 | 新旧 correct 一致率 / 重算所用 scorer_version | `replay_all.consistency` / `scorer_version` |
| 异常项列表 | #2 | event_id / item_version_id / reason（评分失败或题目缺失） | `replay_all.failures` |
| 新旧参数并存实证 | #3 | item_param 总行数 / 并存题目数 / 并存题目详情（同题多版本参数） | `item_param` 表 |

## 验收对照

### 验收 #1：读取全部历史 response_event，用当前活跃估计器重算

- **数据来源**：`replay_all(purpose_scope=...)` 读取该场景全部 `response_event` 行，
  用当前活跃估计器（`ActiveModelPointer.get_active(scope)` 返回的 `model_version`）
  对应的评分器重算。
- **报告字段**：
  - 摘要段：`total_events` / `rescored` / `skipped` / `failed`
  - 重算参数分布段：`old_param_summary` / `new_param_summary` / `param_diff_distribution`
  - 一致性率段：`consistency` / `scorer_version`
- **可重放性**：`summary_hash`（SHA256）—— 同代码版本 + 同数据快照必同输出（D6）。

### 验收 #2：报告含版本映射 + 异常项列表

- **版本映射**：`estimator_run` 表查询——每场景所有版本登记历史（activated_at /
  retired_at / code_digest / input_snapshot_id / graph_release_id）。
  - `current_active`：当前活跃版本（retired_at IS NULL）
  - `old_versions`：已退役版本列表（切换前）
  - `new_version`：当前活跃版本（切换后）
- **异常项列表**：`replay_all.failures`——重算失败的事件（评分器未注册 / 题目缺失等），
  含 `event_id` / `item_version_id` / `reason` 三字段。

### 验收 #3：新旧参数并存验证

- **数据来源**：`item_param` 表查询——同一 `item_version_id` 在多个 `method_version`
  下有参数行 → 并存实证。
- **报告字段**：
  - `total_param_rows`：该场景 item_param 总行数
  - `coexisting_items`：有 ≥2 个版本参数的 item_version_id 列表
  - `by_item`：`{item_version_id: {method_version: {params, sample_size, as_of}}}`
- **并存语义**：ActiveModelPointer 切换版本后，旧版本参数行不被删除（item_param
  只增不改，D1 风格），新版本参数行 INSERT——切换前后报告各引用各自版本的参数
  （`get_params(timestamp=...)` 按时刻回溯当时活跃版本）。

### 验收 #5：不 import 学科包/学段包

- `scripts/jobs/annual_replay_report.py` 只 import 核心域模块
  （`src.core.data.replay` / `src.core.data.active_model_pointer` /
  `src.core.models.estimator_run` / `src.core.scoring.platform_scorers`）。
- 平台评分器（`exact_match` / `keypoint_hit` / `stepwise_rubric`）是核心域兜底桶，
  非学科包；学科评分器由部署入口加载学科包触发注册，本脚本不 import 学科包。

## 报告示例（节选）

```markdown
# 年度全量重放首演报告 — practice

## 元信息
- 场景（purpose_scope）：`practice`
- 批次标签：`annual-replay-2026`
- 生成时刻（UTC）：2026-07-28T08:00:00+00:00

## 摘要
- 历史事件总数：120
- 重算成功：120
- 幂等跳过：0
- 重算失败：0
- 新旧一致性率：0.9833
- 摘要哈希（D6 可重放）：`a1b2c3...`
- 当前活跃估计器版本：`ctt-v2`
- 已退役版本：['ctt-v1']
- 新旧参数并存的题目数：8

## ActiveModelPointer 版本映射（验收 #2）
- 当前活跃版本：`ctt-v2`
- 切换前旧版本：['ctt-v1']
- 切换后新版本：`ctt-v2`

| run_id | model_version | activated_at | retired_at | is_active |
|--------|---------------|--------------|------------|-----------|
| `run_01...` | `ctt-v1` | 2026-01-15... | 2026-07-01... | False |
| `run_02...` | `ctt-v2` | 2026-07-01... | None | True |
```

## 与 `scripts/jobs/annual_replay.py`（T-W4-003）的关系

| 脚本 | 任务卡 | 定位 | 输出 |
|------|--------|------|------|
| `annual_replay.py` | T-W4-003 | 原始重放演练脚本 | JSON（ReplayReport） |
| `annual_replay_report.py` | T-W4-046 | 首演报告生成器 | Markdown / JSON（AnnualReplayReport） |

`annual_replay_report.py` 在 `annual_replay.py` 的 `replay_all` 之上叠加版本映射、
异常项提取、参数并存实证，并渲染为 Markdown 报告（本模板）。两者共用 `replay_all`
核心，避免重复实现。
