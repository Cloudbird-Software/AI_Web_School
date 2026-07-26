# 契约：统一内容模型（Item Model）DDL（冻结候选）

> **地位**：全系统只有一个内容资产域（宪法 A1）；内容版本账只增不改（D1：**Item/Material/Corpus 全版本化**）。
> **来源**：架构 v2 §2.2「统一内容模型」、附录 A 数据模型清单；评审报告 D1/D2/D3 决策；需求 R-Q-20/21/22/26。
> **范围**：Item 族（身份/版本/谱系）+ 母题 + 素材（含版本）+ 题组 + 语料库的结构契约。本文件为结构冻结文本，W1 经 Alembic 迁移落地为真实 DDL。
> 契约版本：1.1.1（corpus_version 补门字段对齐 material_version）｜ 状态：frozen-candidate（人类逐行审查批准后转 frozen）

## 1. 模型总览

```
Item（不变身份）
 └── ItemVersion（不可变内容快照；任何修改产生新版本，旧版本永不覆盖/删除）
      ├ objective        知识标注集 + 认知层级 + 多点关系声明 + 学段
      ├ interaction_ref  交互类型（registries/interaction.yaml）+ 交互参数
      ├ content          题面语义 AST（块+槽位）+ 素材引用（含题组结构）
      ├ scoring_ref      评分器（registries/scorer.yaml）+ 评分参数
      ├ error_bindings   选项/评分维度 → 错误类型 + 置信规则
      └ lineage          生产谱系（tier + 生产线 + 参数/素材源 + AI台账 + 签发）

ItemTemplate ── ItemTemplateVersion(dsl_version, spec)   （A/B 级母题）
      │ instantiate(params, seed)：确定性、内容寻址
      ▼ 产出 ItemVersion（实例即 Item）

Material ── MaterialVersion（素材同样「身份+不可变版本」两段式，D1 全版本化）
ItemGroup（题组/testlet：一材多题 + 组内顺序 + ≤6，R-Z-06）
CorpusAsset ── CorpusVersion（语料库：版本化、带许可、带谱系）
```

四条生产线（A 规则模板 / B 半模板装配 / C 素材驱动 / D 开放命题）地位对等：四级产物的 ItemVersion 结构完全一致，**tier 只是谱系字段**（宪法 A7；架构 v2 §2.2）。

## 2. 表结构定义

### 2.1 item（不变身份）

| 字段 | 类型 | 约束 | 说明 |
|---|---|---|---|
| item_id | text | PK | 不变身份。A/B 级实例 = 内容寻址哈希（见 §3）；C/D 级 = ULID |
| pack_id | text | NOT NULL | 所属学科包（`subject-math` 等）；核心域不解释其语义（A5） |
| tier | enum | NOT NULL | `A`/`B`/`C`/`D` 生产谱系等级（A7 生产线对等） |
| template_version_id | text | FK→item_template_version，可空 | A/B 级实例的母题来源；C/D 级为 NULL |
| current_version_id | text | FK→item_version，可空 | **最新 `published` 版本指针**（语义见下）；无已发布版本时为 NULL |
| created_at | timestamptz | NOT NULL | 创建时间 |

**`current_version_id` 语义与维护纪律**：仅指向最新 **published** 版本（serving 视图与组装消费的一致性来源）；指向 draft/quarantined 属非法。**仅发布事务（内容写入服务）可在发布动作中更新本字段，禁止任何其他路径直写**；W1 落地时以 DB 触发器兜底（发布后自动前移），应用层直写触发告警。除本字段外，`item` 行只增不改；历史 `item_version` 永不 UPDATE/DELETE（D1/R-Q-20/R-Q-26）。

### 2.2 item_version（不可变内容快照）

| 字段 | 类型 | 约束 | 说明 |
|---|---|---|---|
| item_version_id | text | PK | 版本身份。A/B 级 = 内容寻址哈希（§3 公式一）；C/D 级 = 规范化内容快照哈希（§3 公式二） |
| item_id | text | FK→item，NOT NULL | 所属身份 |
| status | enum | NOT NULL | `draft` → `quarantined` → `published` → `retired`（§4 状态机） |
| objective | jsonb | NOT NULL | 知识标注，结构见 §2.2.1（机器可校验 schema 见 §5.1） |
| interaction_ref | jsonb | NOT NULL | `{ interaction_id, interaction_params }`；interaction_id 必须在 interaction.yaml 注册（D4） |
| content | jsonb | NOT NULL | 题面语义 AST（块+槽位）+ 素材版本引用（material_version_id 列表，含题组结构） |
| scoring_ref | jsonb | NOT NULL | `{ scorer_id, scorer_params }`；scorer_id 必须在 scorer.yaml 注册（D4） |
| error_bindings | jsonb | NOT NULL | 选项/评分维度 → 错误类型 + 置信规则（R-Q-06/07） |
| lineage | jsonb | NOT NULL | 生产谱系，结构见 §2.2.2（R-Q-22；机器可校验 schema 见 §5.2） |
| rendered_snapshot | jsonb | 可空 | 物化时的渲染文本快照（校验门受检对象；复现不依赖引擎重放——评审报告 D2）。**进入 `quarantined`（提交校验）前必填**，W1 以 CHECK/触发器承载；A/B 级实例物化时写入 |
| gate_certificate_id | text | FK→gate_certificate，可空 | 门证书引用；**唯一真源**（lineage 内不重复存储，谱系追溯经本字段）；发布强制（§4） |
| published_at | timestamptz | 可空 | 发布时间；**非空必伴随合法 gate_certificate_id（DB 触发器强制，D2）** |
| retired_at | timestamptz | 可空 | 退役时间；退役=状态不是删除，历史作答可回溯（R-Q-26） |
| created_at | timestamptz | NOT NULL | 版本创建时间 |

#### 2.2.1 objective 结构

```json
{
  "kp_set": [{ "dimension": "kp", "code": "math.nal.decimal.compare" }],
  "kp_set_mode": "single | all_required | compensatory",
  "cognitive_level": "remember | understand | apply | analyze | evaluate | create",
  "gradeband": "L | M | H",
  "graph_release": "2026.1",
  "steps": [{ "step_id": "s1", "kp": ["..."], "note": "分步过程题步骤级标注，与题目级并存（R-Q-15）" }]
}
```

- `kp_set_mode`：多知识点题必须声明 `all_required`（全部必需）或 `compensatory`（可相互补偿），否则不得用于诊断（R-Q-14）；compensatory 只佐证不定位（评审报告 D8）。
- `graph_release`：标注时的图谱版本；支持「按当时图谱/映射到当前图谱」双模式查询（R-K-05）。

#### 2.2.2 lineage 结构（生产谱系，R-Q-22）

```json
{
  "tier": "A",
  "pipeline": { "id": "instantiation-engine", "version": "1.0.0" },
  "template_version_id": "sha256:...",
  "params": { "normalized": "规范化参数（A/B 级实例必填，可复现的证据）" },
  "seed": 42,
  "corpus_refs": [{ "corpus_version_id": "...", "digest": "sha256:..." }],
  "ai_ledger_refs": ["ai_call_ledger id（C/D 级 AI 起草必填）"],
  "signed_by": "签发人 id",
  "signed_at": "timestamptz"
}
```

- 门证书引用**不在 lineage 内重复存储**——以 `item_version.gate_certificate_id` 列字段为唯一真源（§2.2），谱系查询经列字段关联。

### 2.3 item_template / item_template_version（A/B 级母题）

| item_template_version 字段 | 类型 | 说明 |
|---|---|---|
| template_version_id | text PK | `sha256` of spec（版本即内容寻址） |
| template_id | text FK | 母题不变身份 |
| dsl_version | text NOT NULL | DSL 语法版本（DSL 自身版本化，架构 v2 §4.1） |
| spec | jsonb NOT NULL | 母题定义六大块：`objective` / `slots`（含 difficulty_relevant 标志）/ `variation_axes` / `presentation` / `answer_program` / `distractor_rules` |
| status | enum | draft/published/retired |
| created_at | timestamptz | |

### 2.4 material / material_version（素材：身份 + 不可变版本，D1 全版本化）

素材与 Item 同构：**material 是不变身份，material_version 是不可变内容快照**——素材修订产生新版本，旧版本永不覆盖/删除（D1「Item/Material/Corpus 全版本化」；题组/题目引用素材时**必须引用 material_version_id**，保证历史试卷可精确回溯）。

| material 字段 | 类型 | 说明 |
|---|---|---|
| material_id | text PK | 素材不变身份（ULID） |
| kind | enum(`passage`/`image`/`table`/`audio`) NOT NULL | 素材类型 |
| pack_id | text | 所属学科包（跨学科通用素材为 `platform`） |
| current_version_id | text FK→material_version，可空 | 最新 published 版本指针（维护纪律同 §2.1） |
| created_at | timestamptz | |

| material_version 字段 | 类型 | 说明 |
|---|---|---|
| material_version_id | text PK | 内容寻址：`H(content_digest)`（§3 公式三） |
| material_id | text FK NOT NULL | 所属身份 |
| content_ref | text NOT NULL | 对象存储引用（内容哈希寻址；音频/图/语篇本体） |
| license_id | text FK→material_license NOT NULL | 许可；来源不合规无法入库（R-Q-18/R-G-03） |
| status | enum NOT NULL | draft/quarantined/published/retired（同 §4 状态机；素材独立过门，架构 v2 §4.1 C 线） |
| lineage | jsonb NOT NULL | 生产谱系（同 §2.2.2 结构） |
| gate_certificate_id | text FK，可空 | 唯一真源（同 §2.2 纪律） |
| published_at / retired_at / created_at | timestamptz | 同 §2.2 |

| material_license 字段 | 类型 | 说明 |
|---|---|---|
| license_id | text PK | |
| source / rights_holder / scope / expires_at | text/timestamptz | 来源、权利人、用途范围、期限（LicenseDecision 留痕；content/sources/ 登记，R-Q-18） |
| decision | enum(`approved`/`rejected`/`expired`) | 过期许可素材不得用于新组卷（§4 serving 规则） |

### 2.5 item_group / corpus_asset / corpus_version

| 表 | 关键字段 | 要点 |
|---|---|---|
| item_group | item_group_id PK, material_version_id FK, item_version_ids text[], ordered bool, testlet bool | 题组 ≤6 题（R-Z-06）；组内顺序可定义；引用素材版本（非素材身份），保证可回溯 |
| corpus_asset | asset_id PK, kind, pack_id, current_version_id FK, created_at | 语料库身份（字/词/篇/句/词表/音标/函数/图库） |
| corpus_version | version_id PK（内容寻址 digest）, asset_id FK, content_ref, license_id FK, lineage, status, gate_certificate_id, published_at, retired_at, created_at | 语料库版本：版本化、带许可、带谱系；被生产线与校验门共同消费（架构 v2 §4.1 B 线）；digest 进实例寻址链（§3 公式一）。`status` 同 §4 状态机四态（draft/quarantined/published/retired）；`gate_certificate_id` 为门证书唯一真源（纪律同 §2.2，lineage 内不重复存储）；`published_at`/`retired_at` 语义同 §2.2——与 §2.4 material_version 对齐（v1.1.1 补，原为 v1.1 修订时本行遗漏） |

## 3. 身份与内容寻址规则（D3；评审报告 D2）

**公式一（A/B 级实例 item_version_id）**：
```
H( template_version_digest, normalized_params, pack_digest,
   engine_digest, corpus_digests, locale )
```
- normalized_params 使用定点/分数运算，禁浮点漂移；同一实例键唯一约束，重复实例化返回同一 id（构造保证可复现，R-Q-03）。

**公式二（C/D 级 item_version_id）**：
```
H( canonical( objective, interaction_ref, content, scoring_ref, error_bindings ), locale )
```
- canonical = 规范化序列化（键序固定、空白规整）；**同一内容必得同一 id**（D3 精神扩展至 C/D 级：重复命题/粘贴产生同 id，入库时作去重提示而非拒绝）。

**公式三（material_version_id / corpus_version_id）**：`H( content_digest )`（对象存储内容哈希）。

- 潜在实例空间组合爆炸不占存储；在线与组卷只消费**已发布实例池**，批任务预生成补池；用户请求链路永不临时调 AI 出题（X5）。

## 4. 状态机与门强制（D2；架构 v2 §4.3）

```
draft ──提交校验──> quarantined ──门证书签发──> published ──退役签发──> retired
```

1. 发布事务必须持门证书（全部阻断项通过）；**数据库触发器强制**：`published_at` 非空必伴随合法 `gate_certificate_id`——绕过写入服务直写必须在数据库层失败（验收标准 #2）。
2. **无回边**：`quarantined` 校验失败的产物**不退回 draft**——按不可变快照哲学，修改=新 draft 版本，失败版本永久留存（审计证据链的一部分）。实现者不得自设回边。
3. authoring / serving 逻辑分区：组装服务只读 serving 视图（`status='published'` 且未退役且素材许可未过期）。
4. 退役是状态不是删除：退役题不得进入任何新卷，但历史作答/历史试卷中的引用永久有效（R-Q-26）。
5. `gate_certificate` 表结构属「校验签发账」契约（W1 状态机契约补充，本卡非目标）。

## 5. 机器可校验 Schema（JSON Schema 2020-12 子集）

### 5.1 objective

```json
{
  "type": "object",
  "required": ["kp_set", "kp_set_mode", "cognitive_level", "gradeband", "graph_release"],
  "properties": {
    "kp_set": {
      "type": "array", "minItems": 1,
      "items": {
        "type": "object", "required": ["dimension", "code"],
        "properties": { "dimension": {"type": "string"}, "code": {"type": "string"} },
        "additionalProperties": false
      }
    },
    "kp_set_mode": {"enum": ["single", "all_required", "compensatory"]},
    "cognitive_level": {"enum": ["remember", "understand", "apply", "analyze", "evaluate", "create"]},
    "gradeband": {"enum": ["L", "M", "H"]},
    "graph_release": {"type": "string"},
    "steps": {
      "type": ["array", "null"],
      "items": {
        "type": "object", "required": ["step_id", "kp"],
        "properties": {
          "step_id": {"type": "string"},
          "kp": {"type": "array", "items": {"type": "string"}}
        }
      }
    }
  }
}
```

约束：`kp_set` 多于 1 项时 `kp_set_mode` 不得为 `single`；用于诊断（diagnosis）时多知识点题不得为未声明关系（R-Q-14 由组装域在组卷时核验）。

### 5.2 lineage

```json
{
  "type": "object",
  "required": ["tier", "pipeline", "signed_by", "signed_at"],
  "properties": {
    "tier": {"enum": ["A", "B", "C", "D"]},
    "pipeline": {
      "type": "object", "required": ["id", "version"],
      "properties": {"id": {"type": "string"}, "version": {"type": "string"}}
    },
    "template_version_id": {"type": ["string", "null"]},
    "params": {"type": ["object", "null"]},
    "seed": {"type": ["integer", "null"]},
    "corpus_refs": {
      "type": "array",
      "items": {
        "type": "object", "required": ["corpus_version_id", "digest"],
        "properties": {"corpus_version_id": {"type": "string"}, "digest": {"type": "string"}}
      }
    },
    "ai_ledger_refs": {"type": "array", "items": {"type": "string"}},
    "signed_by": {"type": "string"},
    "signed_at": {"type": "string", "format": "date-time"}
  }
}
```

约束：tier ∈ {A, B} 时 `template_version_id` 与 `params` 必填（应用层校验）；tier ∈ {C, D} 且经 AI 起草时 `ai_ledger_refs` 非空。

## 6. 实现注记（W1 迁移必读，PostgreSQL 硬约束）

1. **item ↔ item_version 循环外键**（item.current_version_id ↔ item_version.item_id）：迁移中先建两表、后加 `current_version_id` 的 FK 约束（或声明 `DEFERRABLE INITIALLY DEFERRED`）；material ↔ material_version、corpus_asset ↔ corpus_version 同理。
2. **唯一约束与 FK 指向分区表**：`item_version_id` 作被引用键，如需在分区表上建 FK 须含分区键（本契约 item_version 不分区，无此问题；response_event 分区见其契约注记）。
3. **`current_version_id` 前移触发器**：`item_version.status → published` 时自动更新 `item.current_version_id`；应用层直写该字段应触发审计告警（§2.1 纪律）。
4. **`published_at` 门证书触发器**与 §4 规则 1 同一实现，迁移中一并落地。

## 7. 与宪法/需求对照

| 条款 | 本契约的承载 |
|---|---|
| A1 题是数据卷是视图 | 单一内容资产域；试卷为（快照, 约束集, 种子）输出（组装域契约引用本模型） |
| A7 生产线对等 | §1 统一 ItemVersion 结构 + tier 谱系字段 |
| D1 内容版本账（Item/Material/Corpus 全版本化） | §2.1/2.2（Item）、§2.4（Material 身份+版本两段式）、§2.5（Corpus 身份+版本） |
| D2 门 DB 级强制 | §4 触发器规则 + §6 实现注记 3/4 |
| D3 内容寻址 | §3 公式一（A/B）+ 公式二（C/D）+ 公式三（素材/语料） |
| D4 注册表纪律 | interaction_ref/scoring_ref 必须引用注册表 id |
| R-Q-03 可复现 | §3 规范化参数 + digest 链 |
| R-Q-15 分步 | objective.steps 步骤级标注 |
| R-Q-18 素材许可 | §2.4 material_license + serving 过期拦截 |
| R-Q-20/26 版本与退役 | §2.1/§4 |
| R-Q-22 谱系 | §2.2.2 lineage（schema §5.2） |
