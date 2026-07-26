# 遥测与现实仪表盘 —— 附录 E 完整版
> 原则：仪表盘是机器生成的唯一事实呈现层；agent 文字汇报仅为线索，与仪表盘矛盾时以仪表盘为准，并给该 agent 记一次"虚报"。

## 1. 事件记录（JSONL，每次任务一行，文件 telemetry/events-YYYYMM.jsonl）

```json
{
  "task_id": "T-W2-014",
  "wave": "W2",
  "role": "builder|verifier|judge|scribe",
  "model": "provider/model-name",
  "model_tier": "T0|T1|T2",
  "task_type": "impl|fix|test|docs|bench|content",
  "owner_module": "core/instantiation",
  "started_at": "2026-08-01T09:00:00Z",
  "finished_at": "2026-08-01T11:30:00Z",
  "tokens_in": 182000,
  "tokens_out": 24000,
  "cost_cny": 3.42,
  "attempts": 1,
  "escalated": false,
  "gate_results": {"unit": "pass", "contract": "pass", "golden": "fail"},
  "verifier_verdict": "FAIL",
  "pr": "#123",
  "merged": false,
  "misreport": false
}
```
- `misreport`：汇报结论与机器信号矛盾时置 true（由 Scribe 对账时回填）。
- 写入方式：opc dispatch 完成时自动写一行；CI 通过 webhook/workflow 补 gate_results；Scribe 每周对账补 misreport。

## 2. 仪表盘指标（make dashboard 输出，指标定义即下表）

| 指标 | 计算 | 健康阈值 | 异常含义 |
|---|---|---|---|
| CI 红绿灯 | pr-check/nightly 最近状态 | 全绿 | 红>24h：全项目停新卡先修 |
| 黄金路径 | tests/golden-path 最近全量结果 | 30/30 | 任何红即 P4 宪法触发 |
| 契约覆盖 | 冻结契约中有契约测试的比例 | 100% | 缺口=合入违规 |
| 测试趋势 | 测试总数周环比 | 单调不降 | 下降=有人删测试（X1） |
| 任务吞吐 | 本周 merged 任务卡数 | 对照波次计划 | 连续 2 周 <计划 70% → 重估产能 |
| 一次通过率 | attempts=1 且 merged 的卡占比 | ≥60% | 低=任务卡质量或模型梯队问题 |
| 升级率 | escalated=true 占比 | ≤25% | 高=model_floor 定低或规格不清 |
| 虚报率 | misreport=true 占比 | 0 | >0 逐案复盘，对应角色 prompt 加固 |
| 模型×任务类型门通过率 | 按 (model, task_type) 分组通过率（n≥20 才生效） | 见路由纪律 | <60% → routing.yaml 降权 |
| 单位合入成本 | 本周总成本 / merged PR 数 | 按周观察趋势 | 异常升高→查重试与上下文膨胀 |
| T0 token 占比 | T0 tokens / 总 tokens | <15% | 超标→路由表过保守，下沉任务 |
| 累计成本 vs 预算 | 累计 / 总预算硬顶 | <80% 告警线 | ≥80% 强制路由复核 |

## 3. 周报模板（Scribe 每周生成 telemetry/weekly/YYYY-Www.md）

```
# 开发周报 YYYY-Www
## 信号总览（自动）: CI / 黄金路径 / 契约覆盖 / 测试趋势
## 吞吐与质量（自动）: 完成卡数、一次通过率、升级率、虚报事件清单
## 成本（自动）: 本周/累计、按角色、按模型、单位合入成本、T0 占比
## 路由建议（自动）: 触发降权/升权的模型×任务类型清单
## 需人类决策（人工填）: 升级队列遗留、契约变更申请、范围调整
```

## 4. 对账机制（防虚报的闭环）
每周 Scribe 执行：逐任务比对"builder 汇报结论" vs "gate_results+verifier_verdict"→ 矛盾即 misreport=true 并列入周报；虚报 agent 的同角色后续任务自动提升 Verifier 抽检强度（连续 2 次虚报的模型在该角色禁用，直至基准赛重验）。
