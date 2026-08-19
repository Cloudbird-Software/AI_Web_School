# ADR-0005：scorer 契约变更申请——human_confirm 人工兜底改为 L3 模型仲裁

- 状态：**申请中**（冻结契约变更申请，待人类批准——宪法 P5：波内冻结的 L1 契约只增不改，修改须走变更申请）
- 目标契约：specs/contracts/registries/scorer.yaml（已列入 FROZEN.txt）
- 依据：issue #34 D-B（摒弃人工标注：人工只保留治理角色，不进入数据生产链）· ADR-0004 §五
- 关联：specs/contracts/TRACEABILITY.md 第 29 行（human_confirm 的架构来源登记）需随批准同步修订

## 一、为什么必须改

现 scorer.yaml 第 6 个评分器 `human_confirm`（L166–178）把低置信作答汇入**人工队列**，`ai_rubric.notes`（L163）亦写明"低置信自动转 human_confirm 队列"。这与 issue #34 D-B 直接冲突：

1. **不可扩展**：人工裁决吞吐无法匹配内容规模化（W6 万题级、W7 真实用户级）。
2. **不可复现**：人工结论无 model_version/prompt 版本，破坏 D10「AI 可回放」的统一台账口径。
3. **职责错位**：宪法体系内人工的角色是治理（owner 批依赖/ADR/license），不是数据生产。

## 二、申请的变更内容（批准后执行）

### 2.1 scorer.yaml `scorers` 列表

- `human_confirm` 条目 **status: active → deprecated**（保留条目本身：契约只增不改，历史 scoring_trace 仍可能引用；新增字段 `deprecated_since: "2026-08-19"`、`superseded_by: model_arbiter`）。
- 新增评分器条目（草案）：

```yaml
  - id: model_arbiter
    name: L3 模型仲裁
    status: active          # 待本 ADR 批准 + 共识基准认证后激活
    deterministic: false
    summary: 低置信作答升级至高档模型重判（issue #34 D-B），不转人工队列。
    input_contract: 任何交互类型的作答（由其他评分器的低置信输出触发）
    params_schema:
      type: object
      properties:
        model_tier: { type: string, description: L3 仲裁模型档位（AI 台账登记版本） }
        vote_rounds: { type: integer, description: N 次采样多数投票轮数（默认 3） }
        reroute_threshold: { type: number, description: 触发仲裁的置信阈值 }
    notes: 分歧样本回流合成基准集（数据飞轮）；仲裁全链路落 AI 台账（model_version + prompt 版本 + 成本）
```

### 2.2 `ai_rubric.notes`（L163）

"低置信自动转 human_confirm 队列" → "低置信自动升级 model_arbiter 仲裁；跨模型分歧样本回流合成基准集"。

### 2.3 契约测试

tests/contract/registries/test_scorer_registry.py 同步：断言 `human_confirm.status == deprecated` 且 `model_arbiter` 通过 schema 校验与共识基准认证门槛（认证在共识基准集系统就绪前以占位测试登记为「未实现」，不宣称已强制——宪法 A8）。

## 三、影响面

| 受影响方 | 影响 | 处置 |
|---|---|---|
| 低置信作答去向 | 人工队列 → L3 模型仲裁 | W6 harness（操作员/评价者）先落地仲裁通道；过渡期内低置信按 ai_rubric 原置信输出降权标记，不阻塞 |
| 教研工作台抽检 | human_confirm 与抽检共用工作台的设计作废 | 抽检保留为治理行为（质量度量），不再是评分链路兜底 |
| TRACEABILITY.md | human_confirm 架构来源登记需修订 | 随本 ADR 批准一并更新并保留变更记录 |
| W5 原任务卡 | 无直接引用 human_confirm 的卡片 | 无需重锚定 |

## 四、风险

| 风险 | 对策 |
|---|---|
| L3 仲裁仍分歧 | 分歧样本丢弃或降权（不冒充实测），回流合成基准集换血重验 |
| 共识打标系统性偏差 | 三源分离铁律：synthetic 永不写入实测参数；漂移监控跌破阈值自动降回 shadow |
| 仲裁成本 | AI 台账成本归集 + W6 预算硬顶 |

## 五、结论

human_confirm 保留为 deprecated 历史条目，评分兜底切换为 model_arbiter（L3 模型仲裁 + 台账 + 回流）。**本 ADR 批准前，scorer.yaml 一字不动。**
