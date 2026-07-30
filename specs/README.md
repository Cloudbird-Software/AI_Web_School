# specs/ — 契约与规格目录

本目录是平台所有机器校验契约、导入导出规格、以及示例数据的单一事实源。

---

## 目录结构

```
specs/
├── item_version_import_schema.json   # W0-1: item_version 导入 JSON Schema（机器校验入口）
├── pydantic_item_version.py          # W0-1: 对应的 Pydantic 导入模型（供 loader/adapter 使用）
├── examples/                         # W0-1: 每学科一个最小可运行示例
│   ├── math_item_example.json        #   数学：单选 + exact_match 评分
│   ├── chinese_item_example.json     #   语文：拼音填空 + exact_match 评分
│   └── english_item_example.json     #   英语：词义单选 + exact_match 评分
├── contracts/                        # 冻结级契约（波内只增不改）
│   ├── api/                          #   OpenAPI 契约（FROZEN.txt 标记冻结）
│   ├── db/                           #   DB 模型契约（item-model、paper-model）
│   ├── events/                       #   事件契约（response_event 等）
│   ├── gate/                         #   校验门 policy schema 与默认值
│   └── registries/                   #   双类型注册表（interaction / scorer）
├── adr/                              # 架构决策记录（0001-adopt-devos 等）
├── modules/                          # 模块职责文档
└── constitution.md                   # 项目宪法
```

---

## W0-1: item_version 导入契约

### 六大块结构（契约 §2.2）

每个 `item_version` 由 **六大块 + 顶层元数据** 组成。以下是 required / optional 清单：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `item_version_id` | `string` | ✅ | 内容寻址哈希 ID（同内容必同 ID） |
| `item_id` | `string` | ✅ | 题目逻辑 ID（跨版本不变） |
| `status` | `enum` | ✅ | `draft \| quarantined \| published \| retired` |
| **objective** | `object` | ✅ | 知识标注集 + 认知层级 + 学段 |
| **interaction_ref** | `object` | ✅ | 交互类型引用（`interaction_id` 必须注册） |
| **content** | `object` | ✅ | 题面语义 AST（blocks permissive） |
| **scoring_ref** | `object` | ✅ | 评分器引用（`scorer_id` 必须注册） |
| **error_bindings** | `array` | ✅ | 选项/评分维度 → 错误类型绑定 |
| **lineage** | `object` | ✅ | 生产谱系（tier A/B/C/D） |
| `rendered_snapshot` | `object` | ❌ | 渲染快照（quarantined 前必填，导入可省略） |
| `gate_certificate_id` | `string` | ❌ | 校验门证书 ID（由 gate 服务签发，导入通常为空） |
| `published_at` | `string` | ❌ | 发布时间（ISO 8601） |
| `retired_at` | `string` | ❌ | 退役时间（ISO 8601） |

### 1. objective（知识标注块）

```json
{
  "kp_set": [{"dimension": "kp", "code": "math.arithmetic.addition"}],
  "kp_set_mode": "single",
  "cognitive_level": "apply",
  "gradeband": "L",
  "graph_release": "v1"
}
```

**必填枚举：**
- `kp_set_mode`：`single`（单知识点）/ `all_required`（全点必修）/ `compensatory`（多知识点补偿）
- `cognitive_level`：`remember` / `understand` / `apply` / `analyze` / `evaluate` / `create`（Bloom 修订版）
- `gradeband`：`L`（低段 1-2）/ `M`（中段 3-4）/ `H`（高段 5-6）

### 2. interaction_ref（交互类型引用）

```json
{
  "interaction_id": "single_choice",
  "interaction_params": {"shuffle": false}
}
```

- `interaction_id` **必须**在 [`contracts/registries/interaction.yaml`](contracts/registries/interaction.yaml) 中注册且 `status=active`（宪法 D4）。
- 注册的现役交互类型：`single_choice` / `multi_choice` / `text_blank` / `numeric_blank` / `matching` / `ordering` / `short_answer` / `stepwise_process` / `writing` / `drawing_operation`。

### 3. content（题面语义 AST）

```json
{
  "blocks": [
    {"type": "stem", "text": "题干文本"},
    {"type": "options", "choices": [{"id": "A", "label": "选项A"}, {"id": "B", "label": "选项B"}]}
  ]
}
```

- `blocks` 是 permissive `list[dict]`：结构因交互类型差异很大（单选/填空/写作 blocks 各不相同）。
- 校验策略：**JSON Schema 只校验 blocks 存在且为数组**，具体 block 结构由 per-interaction 校验器在 registry 层负责（见 W0-1 Notes："Keep schema permissive in content.blocks"）。

### 4. scoring_ref（评分器引用）

```json
{
  "scorer_id": "exact_match",
  "scorer_params": {
    "answer": {"selected": "B"},
    "partial_credit": null,
    "normalization": {"case_insensitive": true, "trim": true}
  }
}
```

- `scorer_id` **必须**在 [`contracts/registries/scorer.yaml`](contracts/registries/scorer.yaml) 中注册且 `status=active`（宪法 D4）。
- 注册的现役评分器：`exact_match` / `math_equivalence` / `stepwise_rubric` / `keypoint_hit` / `ai_rubric` / `human_confirm`。
- 兼容矩阵（`compatible_scorers`）在 interaction.yaml 中声明，加载时交叉校验。

### 5. error_bindings（错误绑定，顶层为数组）

```json
[
  {"option_id": "A", "error_type_id": "math.arithmetic.off_by_one_minus", "confidence": 0.8, "rule_version": "v1"},
  {"option_id": "C", "error_type_id": "math.arithmetic.off_by_one_plus", "confidence": 0.8, "rule_version": "v1"}
]
```

- 元素结构 permissive：具体字段由错误类型注册表承载（R-Q-06/07）。
- 常见字段：`option_id` / `blank_id` / `error_type_id` / `confidence` / `rule_version`。

### 6. lineage（生产谱系）

```json
{
  "tier": "A",
  "pipeline": {"id": "manual_import", "version": "v0"},
  "template_version_id": null,
  "params": null,
  "seed": null,
  "corpus_refs": null,
  "ai_ledger_refs": null,
  "signed_by": "importer",
  "signed_at": "2026-07-28T00:00:00Z"
}
```

- `tier`：`A`（教研手工）/ `B`（模板实例化）/ `C`（AI 起草 + 人审）/ `D`（AI 全自动 + 抽检）
- `pipeline`：`{id, version}`，生产线标识
- tier A/B 通常 `template_version_id` + `params` 非空
- tier C/D + AI 起草时 `ai_ledger_refs` 非空
- `signed_at`：ISO 8601 格式（`YYYY-MM-DDTHH:MM:SSZ`）

---

## 质量检查清单（导入前必过）

1. **Schema 校验**：`jsonschema.validate(data, schema)` 无错误
2. **Pydantic 校验**：`ItemVersionImport.model_validate(data)` 无错误
3. **注册表交叉校验**：
   - `interaction_ref.interaction_id` 在 interaction.yaml 中 `status=active`
   - `scoring_ref.scorer_id` 在 scorer.yaml 中 `status=active`
   - `scorer_id ∈ interaction.compatible_scorers`（兼容匹配）
4. **语义校验（per-interaction）**：
   - 单选：`content.blocks` 含 options 且 options 数量 ≥ 2；`scorer_params.answer.selected` ∈ option IDs
   - 填空：`interaction_params.blank_count` 与 `scorer_params.answer.blanks` key 数一致
   - 数值填空：`scorer_params.answer.blanks.*.value` 为合法规范化数值（整数/有限小数/`a/b` 分数）
5. **lineage 完整性**：
   - tier A/B：`template_version_id` 非空（模板线）或注明 `manual_import`（手工线）
   - tier C/D 且 `ai_ledger_refs` 非空：对应 AI 台账条目存在
6. **KP 图谱一致性**：`objective.kp_set[*].code` 在 `graph_release` 版本中存在

---

## 示例加载与校验（快速上手）

```python
"""最小校验脚本：load + jsonschema + pydantic"""
import json
from pathlib import Path

import jsonschema
from specs.pydantic_item_version import ItemVersionImport

ROOT = Path(__file__).resolve().parents[1]  # /workspace

# 1. Load schema & example
schema = json.loads((ROOT / "specs" / "item_version_import_schema.json").read_text())
example = json.loads((ROOT / "specs" / "examples" / "math_item_example.json").read_text())

# 2. JSON Schema 校验
jsonschema.validate(instance=example, schema=schema)  # 无异常 = 通过
print("✓ JSON Schema 校验通过")

# 3. Pydantic 校验（带强类型）
obj = ItemVersionImport.model_validate(example)
print(f"✓ Pydantic 校验通过：item_version_id={obj.item_version_id}")
print(f"  interaction_id={obj.interaction_ref.interaction_id}")
print(f"  scorer_id={obj.scoring_ref.scorer_id}")
print(f"  cognitive_level={obj.objective.cognitive_level}")
```

---

## 机器校验入口

- **CI 测试**：`tests/unit/test_item_import_schema.py`（加载 schema 并校验 3 个示例）
- **注册表契约测试**：`tests/contract/registries/`（双类型注册表 + 双向交叉引用）
- **冻结契约保护**：`specs/contracts/FROZEN.txt` 列出已冻结文件，CI 禁止修改
