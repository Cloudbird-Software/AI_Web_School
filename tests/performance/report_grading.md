# T-W4-039 客观题批改链路压测报告

> 生成时间：2026-07-28 13:39:18（由 test_grading_latency.py 刷新）

> 验收依据：任务卡 T-W4-039 §验收 #1/#2/#3；E2E-7 承载项


## 1. 环境信息

| 项 | 值 |
|---|---|
| python | 3.12.10 |
| platform | Windows-11-10.0.26200-SP0 |
| processor | Intel64 Family 6 Model 141 Stepping 1, GenuineIntel |
| cpu_count | 16 |
| total_calls | 100 |
| interactions | single_choice/numeric_blank/matching |
| db | PostgreSQL 16 @ localhost:5432/muti_w4_perf（本地，含 DB 写入） |

## 2. 延迟分布（含 DB 写入的完整批改链路）

- 样本数：100
- 平均延迟：6.84 ms（阈值 10000 ms）
- **p95：10.84 ms**（阈值 15000 ms）
- 平均判定：✅ 通过
- p95 判定：✅ 通过

### 延迟分布直方图（10 桶）

| 桶下限 (ms) | 桶上限 (ms) | 计数 | 占比 |
|---|---|---|---|
| 3.58 | 8.71 | 90 | 90.0% |
| 8.71 | 13.85 | 7 | 7.0% |
| 13.85 | 18.98 | 0 | 0.0% |
| 18.98 | 24.11 | 0 | 0.0% |
| 24.11 | 29.25 | 0 | 0.0% |
| 29.25 | 34.38 | 2 | 2.0% |
| 34.38 | 39.51 | 0 | 0.0% |
| 39.51 | 44.64 | 0 | 0.0% |
| 44.64 | 49.78 | 0 | 0.0% |
| 49.78 | 54.91 | 1 | 1.0% |

## 3. DB 写入耗时拆分

- 评分器计算（run_scorer，无 DB）平均：0.088 ms
- DB 写入 + 落账编排平均：6.750 ms
- 总链路平均：6.838 ms
- 占比：评分器 1.3% / DB+编排 98.7%

### 按交互类型拆分

| 交互类型 | 样本数 | 平均 (ms) | p95 (ms) |
|---|---|---|---|
| single_choice | 34 | 7.272 | 9.808 |
| numeric_blank | 33 | 6.638 | 10.323 |
| matching | 33 | 6.589 | 9.426 |

## 4. 评分准确率（与期望对比）

- 总批改数：100
- 评分与期望一致数：100
- **准确率：100.0%**
- 不一致样本：无（评分器 100% 准确）

## 5. 测量方法说明

- 100 次批改：33 单选 + 34 数值填空 + 33 匹配连线（轮转）。
- 每次调用 score_and_record：run_scorer（exact_match）→ infer_option_errors → build_scoring_trace → record_event（response_event INSERT）。
- DB 写入经 async_session 事务回滚隔离：INSERT 真实执行（测延迟），测试结束回滚不污染 DB。
- 延迟拆分：总延迟 - run_scorer 单独延迟 = DB 写入 + 落账编排。
- 阈值 10s/15s 极宽松：客观题评分微秒级、DB 写入毫秒级，为慢 CI 留余量。
