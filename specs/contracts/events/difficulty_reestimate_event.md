# 契约：difficulty_reestimate 难度重估事件（冻结候选）

> **地位**：参数标定数据飞轮的任务事件——实例化后检测到 difficulty_relevant 槽变更时发布，
> 供 W3+ 参数标定消费（宪法 D5 参数分场景独立估计）。
> 契约版本：1.0.0 ｜ 状态：frozen-candidate

## 1. 事件定义

事件类型：`difficulty_reestimate`
传输：Redis 任务队列（stream/list），W2 仅落事件 + 验证 schema。
消费方：W3+ 参数标定批处理（按 scene 分场景消费，禁止混估 D5）。

## 2. 事件字段

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| event_type | string | ✅ | 固定值 `"difficulty_reestimate"` |
| event_id | string(ULID) | ✅ | 事件唯一 id（应用层生成） |
| item_version_id | string | ✅ | 触发重估的实例 id（公式一内容寻址哈希） |
| template_version_id | string | ✅ | 母题版本 id |
| pack_digest | string | ✅ | 学科包摘要（D5 分包估计） |
| changed_slots | array[string] | ✅ | 发生变更的 difficulty_relevant 槽名列表 |
| params | object | ✅ | 当前实例化参数（规范化后） |
| baseline_params | object | 可空 | 基准参数（变更对比基准；NULL=无基准，首次实例化） |
| scene | enum | ✅ | `practice` / `diagnosis` / `measurement`（D5 禁止混估） |
| created_at | string(ISO 8601) | ✅ | 事件时间戳（UTC） |

## 3. JSON Schema

```json
{
  "type": "object",
  "required": [
    "event_type", "event_id", "item_version_id", "template_version_id",
    "pack_digest", "changed_slots", "params", "scene", "created_at"
  ],
  "properties": {
    "event_type": {"type": "string", "const": "difficulty_reestimate"},
    "event_id": {"type": "string", "minLength": 1},
    "item_version_id": {"type": "string", "pattern": "^sha256:"},
    "template_version_id": {"type": "string", "pattern": "^sha256:"},
    "pack_digest": {"type": "string", "pattern": "^sha256:"},
    "changed_slots": {
      "type": "array",
      "items": {"type": "string"},
      "minItems": 1
    },
    "params": {"type": "object"},
    "baseline_params": {"type": ["object", "null"]},
    "scene": {"type": "string", "enum": ["practice", "diagnosis", "measurement"]},
    "created_at": {"type": "string", "format": "date-time"}
  },
  "additionalProperties": false
}
```

## 4. 写入规则

1. **仅 difficulty_relevant 槽变更时发布**：非 difficulty_relevant 槽变更不触发。
2. **幂等**：同一 (item_version_id, changed_slots) 多次发布仅保留首条（消费方去重）。
3. **不入三本账**：本事件是任务事件，非作答事件账/内容版本账/校验签发账；
   落 Redis 队列即可，不需要 DB 持久化（W2 阶段）。
4. **PII 安全**：事件不含任何学生 PII（D7），仅含题目参数级信息。
