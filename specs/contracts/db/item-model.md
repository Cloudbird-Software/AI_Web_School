# 契约：统一内容模型（Item Model）DDL（冻结候选）

> **地位**：全系统只有一个内容资产域（宪法 A1）；内容版本账只增不改（D1）。
> **来源**：架构 v2 §2.2「统一内容模型」、附录 A 数据模型清单；评审报告 D1/D2/D3 决策；需求 R-Q-20/21/22/26。
> **范围**：Item 族（身份/版本/谱系）+ 母题 + 素材 + 题组 + 语料库的结构契约。本文件为结构冻结文本，W1 经 Alembic 迁移落地为真实 DDL。
> 契约版本：1.0.0 ｜ 状态：frozen-candidate（人类逐行审查批准后转 frozen）

## 1. 模型总览

```
Item（不变身份）
 └── ItemVersion（不可变内容快照；任何修改产生新版本，旧版本永不覆盖/删除）
      ├ objective        知识标注集 + 认知层级 + 多点关系声明 + 学段
      ├ interaction_ref  交互类型（registries/interaction.yaml）+ 交互参数
      ├ content          题面语义 AST（块+槽位）+ 素材引用（含题组结构）
      ├ scoring_ref      评分器（registries/scorer.yaml）+ 评分参数
      ├ error_bindings   选项/评分维度 → 错误类型 + 置信规则
      └ lineage          生产谱系（tier + 生产线 + 参数/素材源 + AI台账 + 门证书 + 签发）

ItemTemplate ── ItemTemplateVersion(dsl_version, spec)   （A/B 级母题）
      │ instantiate(params, seed)：确定性、内容寻址
      ▼ 产出 ItemVersion（实例即 Item）

Material（素材：语篇/图/表/音频，独立资产，含来源与许可）
ItemGroup（题组/testlet：一材多题 + 组内顺序 + ≤6，R-Z-06）
CorpusAsset（语料库：字/词/篇/句/词表/音标/函数/图库；版本化、带许可、带谱系）
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
| current_version_id | text | FK→item_version | 当前版本指针（视图便利字段；历史版本永不删除） |
| created_at | timestamptz | NOT NULL | 创建时间 |

**只增不改**：`item` 行创建后仅 `current_version_id` 可随新版本发布而前移；历史 `item_version` 永不 UPDATE/DELETE（D1/R-Q-20/R-Q-26）。

### 2.2 item_version（不可变内容快照）

| 字段 | 类型 | 约束 | 说明 |
|---|---|---|---|
| item_version_id | text | PK | 版本身份。A/B 级 = 内容寻址哈希（§3）；C/D 级 = `item_id` + 版本序号哈希 |
| item_id | text | FK→item，NOT NULL | 所属身份 |
| status | enum | NOT NULL | `draft` → `quarantined` → `published` → `retired`（§4 状态机） |
| objective | jsonb | NOT NULL | 知识标注，结构见 §2.2.1 |
| interaction_ref | jsonb | NOT NULL | `{ interaction_id, interaction_params }`；interaction_id 必须在 interaction.yaml 注册（D4） |
| content | jsonb | NOT NULL | 题面语义 AST（块+槽位）+ 素材引用（material_id 列表，含题组结构） |
| scoring_ref | jsonb | NOT NULL | `{ scorer_id, scorer_params }`；scorer_id 必须在 scorer.yaml 注册（D4） |
| error_bindings | jsonb | NOT NULL | 选项/评分维度 → 错误类型 + 置信规则（R-Q-06/07） |
| lineage | jsonb | NOT NULL | 生产谱系，结构见 §2.2.2（R-Q-22） |
| rendered_snapshot | jsonb | 可空 | 物化时的渲染文本快照（校验门受检对象；复现不依赖引擎重放——评审报告 D2） |
| gate_certificate_id | text | FK→gate_certificate，可空 | 门证书引用；发布强制（§4） |
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
  "gate_certificate_id": "...",
  "signed_by": "签发人 id",
  "signed_at": "timestamptz"
}
```

### 2.3 item_template / item_template_version（A/B 级母题）

| item_template_version 字段 | 类型 | 说明 |
|---|---|---|
| template_version_id | text PK | `sha256` of spec（版本即内容寻址） |
| template_id | text FK | 母题不变身份 |
| dsl_version | text NOT NULL | DSL 语法版本（DSL 自身版本化，架构 v2 §4.1） |
| spec | jsonb NOT NULL | 母题定义六大块：`objective` / `slots`（含 difficulty_relevant 标志）/ `variation_axes` / `presentation` / `answer_program` / `distractor_rules` |
| status | enum | draft/published/retired |
| created_at | timestamptz | |

### 2.4 material / item_group / corpus_asset（统一内容模型的素材侧）

| 表 | 关键字段 | 要点 |
|---|---|---|
| material | material_id PK, kind enum(`passage`/`image`/`table`/`audio`), content_ref, license_id FK, status | 素材独立管理、一材多题（R-Q-17）；来源不合规无法入库（R-G-03） |
| material_license | license_id PK, source, rights_holder, scope, LicenseDecision | 许可决策留痕（R-Q-18；content/sources/ 登记） |
| item_group | item_group_id PK, material_id FK, item_version_ids text[], ordered bool, testlet bool | 题组 ≤6 题（R-Z-06）；组内顺序可定义 |
| corpus_asset / corpus_version | asset_id / version_id PK, kind, digest, license_id, lineage | 语料库一等资产：版本化、带许可、带谱系；被生产线与校验门共同消费（架构 v2 §4.1 B 线） |

## 3. 身份与内容寻址规则（D3；评审报告 D2）

```
A/B 级实例 item_version_id =
  H( template_version_digest,
     normalized_params,          # 规范化参数（定点/分数运算，禁浮点漂移）
     pack_digest,                # 学科包版本
     engine_digest,              # 实例化引擎版本
     corpus_digests,             # 语料库版本链
     locale )
```

- 同一实例键唯一约束；重复实例化请求返回同一 `item_version_id`（构造保证可复现，R-Q-03）。
- 潜在实例空间组合爆炸不占存储；在线与组卷只消费**已发布实例池**，批任务预生成补池；用户请求链路永不临时调 AI 出题（X5）。

## 4. 状态机与门强制（D2；架构 v2 §4.3）

```
draft ──提交校验──> quarantined ──门证书签发──> published ──退役签发──> retired
```

1. 发布事务必须持门证书（全部阻断项通过）；**数据库触发器强制**：`published_at` 非空必伴随合法 `gate_certificate_id`——绕过写入服务直写必须在数据库层失败（验收标准 #2）。
2. authoring / serving 逻辑分区：组装服务只读 serving 视图（`status='published'` 且未退役且许可未过期）。
3. 退役是状态不是删除：退役题不得进入任何新卷，但历史作答/历史试卷中的引用永久有效（R-Q-26）。
4. `gate_certificate` 表结构属「校验签发账」契约（W1 状态机契约补充，本卡非目标）。

## 5. 与宪法/需求对照

| 条款 | 本契约的承载 |
|---|---|
| A1 题是数据卷是视图 | 单一内容资产域；试卷为（快照, 约束集, 种子）输出（组装域契约引用本模型） |
| A7 生产线对等 | §1 统一 ItemVersion 结构 + tier 谱系字段 |
| D1 内容版本账 | §2.1/2.2 只增不改规则 |
| D2 门 DB 级强制 | §4 触发器规则 |
| D3 内容寻址 | §3 寻址公式与唯一约束 |
| D4 注册表纪律 | interaction_ref/scoring_ref 必须引用注册表 id |
| R-Q-03 可复现 | §3 规范化参数 + digest 链 |
| R-Q-15 分步 | objective.steps 步骤级标注 |
| R-Q-20/26 版本与退役 | §2.1/§4 |
| R-Q-22 谱系 | §2.2.2 lineage |
