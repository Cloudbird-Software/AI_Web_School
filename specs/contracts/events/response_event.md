# 契约：response_event 作答事件（冻结候选）

> **地位**：三本只增不改的账之一「作答事件账」（宪法 D1）；数据飞轮的入水口（宪法 A3/A4）。
> **来源**：架构 v2 §4.7 数据域；需求 R-D-01/R-D-02/R-D-03；评审报告 D11 决策。
> **纪律**：append-only——数据库权限禁 UPDATE/DELETE（宪法 D1），每日增量导出 Parquet 开放归档（十年数据主权）。
> 契约版本：1.0.0 ｜ 状态：frozen-candidate（人类逐行审查批准后转 frozen）

## 1. 表定义

表名：`response_event`（PostgreSQL，**按月分区**）

| 字段 | 类型 | 必填 | 说明 | 架构来源 |
|---|---|---|---|---|
| event_id | uuid | ✅ | 事件唯一 id，主键 | §4.7「事件 id」 |
| student_alias_id | uuid | ✅ | 匿名学生 id；直接标识只允许存在于独立 PII 保险库 schema（D7） | §4.7、§4.8 合规层 |
| item_version_id | text | ✅ | 作答题目版本（A/B 级实例=内容寻址哈希，D3） | §4.7、§2.2 |
| scene | enum | ✅ | `practice` / `diagnosis` / `measurement`——分场景独立统计禁止混估（D5） | §4.7、§4.7 参数标定 |
| raw_payload | jsonb | ✅ | **原始作答载荷**（作答内容本身，非仅存对错，R-D-01）；结构由交互类型 response_schema 保证 | §4.7 |
| duration_ms | integer | ✅ | 作答耗时（毫秒）；健康度监控维度之一 | §4.7「耗时」 |
| scoring_trace | jsonb | ✅ | 评分轨迹，结构见 §3 | §4.7「评分轨迹」 |
| error_inferences | jsonb | ✅ | 错误类型推断数组，结构见 §4（可为空数组） | §4.7「错误推断」 |
| testlet_id | text | 可空 | 题组/testlet id（题组内相关性统计用，R-Z-06） | §4.7、§4.4 |
| session_id | uuid | ✅ | 作答会话 id（复习排程/会话分析用） | §4.7 |
| audio_play_events | jsonb | 可空 | 音频播放行为（播放次数/时长/限次策略命中），音频题必填 | §4.7「播放行为」 |
| source_ref | jsonb | 可空 | 来源追溯：`{paper_id, placement_token}`（静态卷 S2）或 `{assembly_run_id}`（在线 S3/S4）；A4 入水口 | §4.6 追溯链 |
| created_at | timestamptz | ✅ | 事件时间戳（UTC），分区键 | §4.7 |

## 2. 写入与存储规则（物理强制）

1. **append-only**：数据库角色对 `response_event` 仅授予 `INSERT`/`SELECT`，禁 `UPDATE`/`DELETE`（D1/X7）；定期哈希锚定入审计哈希链。
2. **按月分区**：以 `created_at` 为分区键；分区创建走 Alembic 迁移，禁止手工 DDL（X7）。
3. **每日增量归档**：导出 Parquet 至对象存储 + schema 注册表登记——十年可用的本质是原始数据不依赖单一厂商的开放归档（§4.7）。
4. **场景不可为空、不可混估**：下游估计器按 `scene` 独立取数（D5）；`practice` 场景数据因暴露偏差仅用于粗校准与差题预警（评审报告 D4）。

## 3. scoring_trace 结构（评分轨迹）

```json
{
  "scorer_id": "exact_match",
  "scorer_version": "1.0.0+sha256...",
  "process": { "note": "评分器自描述的过程明细（命中点/步骤判定/量规逐维理由）" },
  "confidence": {
    "recognition": 0.0,
    "scoring": 1.0,
    "note": "四层置信度之识别与评分层；推断层在 error_inferences[].confidence，掌握层在掌握度估计，禁止混为单一 AI 置信度（§4.5）"
  },
  "rerun_of": null
}
```

- `scorer_id` 必须是 `registries/scorer.yaml` 中注册的 id（D4 注册表纪律）。
- 重判（R-D-05）：新 scorer 版本重放历史事件时**写平行 score_run**，原 `scoring_trace` 不变；`rerun_of` 记录原始 score_run 引用；增量重判防成本爆炸（评审报告 D4）。

## 4. error_inferences 结构（错误推断）

```json
[
  {
    "error_type_id": "math.decimal.digits_more_is_larger",
    "confidence": 0.85,
    "rule_version": "1.2.0",
    "evidence": { "selected_option": "B" }
  }
]
```

- 推断规则版本化；真实发生率回流修正规则与证据计数（R-Q-08）。
- 「选某项」是证据非因果（§4.5）；compensatory 题只佐证不定位，定位必须由孤立题完成（评审报告 D8）。

## 5. 机器可校验 Schema（JSON Schema 2020-12 子集）

```json
{
  "type": "object",
  "required": ["event_id", "student_alias_id", "item_version_id", "scene", "raw_payload", "duration_ms", "scoring_trace", "error_inferences", "session_id", "created_at"],
  "properties": {
    "event_id": { "type": "string", "format": "uuid" },
    "student_alias_id": { "type": "string", "format": "uuid" },
    "item_version_id": { "type": "string", "minLength": 1 },
    "scene": { "enum": ["practice", "diagnosis", "measurement"] },
    "raw_payload": { "type": "object" },
    "duration_ms": { "type": "integer", "minimum": 0 },
    "scoring_trace": {
      "type": "object",
      "required": ["scorer_id", "scorer_version", "confidence"],
      "properties": {
        "scorer_id": { "type": "string" },
        "scorer_version": { "type": "string" },
        "confidence": {
          "type": "object",
          "required": ["scoring"],
          "properties": {
            "scoring": { "type": "number", "minimum": 0, "maximum": 1 },
            "recognition": { "type": "number", "minimum": 0, "maximum": 1 }
          }
        }
      }
    },
    "error_inferences": {
      "type": "array",
      "items": {
        "type": "object",
        "required": ["error_type_id", "confidence", "rule_version"],
        "properties": {
          "error_type_id": { "type": "string" },
          "confidence": { "type": "number", "minimum": 0, "maximum": 1 },
          "rule_version": { "type": "string" }
        }
      }
    },
    "testlet_id": { "type": ["string", "null"] },
    "session_id": { "type": "string", "format": "uuid" },
    "audio_play_events": { "type": ["array", "null"] },
    "source_ref": { "type": ["object", "null"] },
    "created_at": { "type": "string", "format": "date-time" }
  }
}
```

## 6. 与宪法/需求对照

| 条款 | 本契约的承载 |
|---|---|
| D1 三本账只增不改 | §2 写入规则 1（DB 权限禁 UPDATE/DELETE + 哈希锚定） |
| D3 内容寻址身份 | `item_version_id` 引用内容寻址实例 |
| D5 参数分场景 | `scene` 枚举必填 + §2 规则 4 |
| D6 估计器可替换 | §3 重判规则（平行 score_run，原序列不动） |
| D7 PII 隔离 | 仅 `student_alias_id`，无直接标识 |
| R-D-01 原始保存 | `raw_payload` 存作答内容本身 |
| R-D-02 全要素 | §1 字段表（身份/场景/耗时/评分/推断） |
| R-D-05 可重判 | §3 scoring_trace 版本与重判规则 |
