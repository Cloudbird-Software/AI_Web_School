# 契约：卷追溯模型（Paper Model）DDL（冻结候选）

> **地位**：卷（paper）是组卷产物账本——一份卷 = 一次组卷选定的 item_version 集合 + 顺序；卷内题目（paper_item）承载位置与纠错短码。两表只增不改（D1 风格：重新组卷生成新行，旧卷永不覆盖）。
> **来源**：架构 v2 §4.6「渲染域」/§4.4「组装域」、附录 A 数据模型清单；T-W2-037 落地（迁移 0009）；W3 S9-③ 补写本冻结契约文本。
> **范围**：paper / paper_item 两表结构契约 + 卷码/QR/题短码规则 + 追溯链。组装约束（配比/梯度/互斥）属组卷引擎契约，不在本文。
> 契约版本：1.0.0 ｜ 状态：frozen-candidate（人类逐行审查批准后转 frozen）

## 1. 模型总览

```
paper（一份卷的组卷产物：卷码 + 卷规格 id + 知识点快照 + 确定性种子）
 └── paper_item（卷内题目：位置标识 placement_token + 题号 + 题短码）
      └→ item_version（卷选定的不可变内容快照，D3 内容寻址）
           └→ gate_certificate（签发证书，追溯链终点）
```

追溯链（R-Q-22 / 架构 v2 §4.6）：
**题短码 → paper_item → item_version → gate_certificate → 签发人**。
应用层实现：`src/core/render/trace_codes.py::build_trace_chain`。

## 2. 表结构定义

### 2.1 paper（卷主表）

| 字段 | 类型 | 约束 | 说明 |
|---|---|---|---|
| paper_id | text | PK | 卷内部 id（应用层 ULID） |
| paper_code | text | NOT NULL, UNIQUE | 人类可读卷码 = ULID + Luhn 校验位（27 字符），打印在卷面，防手抄错 |
| paper_spec_id | text | NOT NULL, UNIQUE | 卷规格 id；QR payload 仅含此 id + 校验位，**不含 item_version_id 等实例明文**（QR 公开打印，不能泄露题目） |
| paper_title | text | NOT NULL | 卷名 |
| gradeband | text | NOT NULL, CHECK ∈ ('L','M','H') | 学段 |
| subject_pack_id | text | NOT NULL, CHECK ∈ ('subject-math','subject-chinese','subject-english') | 学科包 |
| weekly_batch_id | text | 可空 | 周更批次 id（非周更产出的卷为 NULL） |
| kp_snapshot_ref | text | NOT NULL | 知识点范围快照引用（确定性组卷的输入快照，D3 可复现前提） |
| seed | bigint | NOT NULL | 确定性种子（同快照+同种子+同 Profile 版本 → 同题序） |
| rendered_snapshot_path | text | 可空 | 渲染产物（PDF）落盘路径（可复现验证） |
| created_at | timestamptz | NOT NULL, server_default now() | 创建时间 |
| created_by | text | NOT NULL | 创建人/生产线标识（追溯用） |

### 2.2 paper_item（卷内题目表）

| 字段 | 类型 | 约束 | 说明 |
|---|---|---|---|
| paper_item_id | text | PK | 内部 id（应用层 ULID） |
| paper_id | text | FK→paper, NOT NULL, ondelete=RESTRICT | 所属卷 |
| item_version_id | text | FK→item_version, NOT NULL, ondelete=RESTRICT | 卷选定的题目版本 |
| placement_token | text | NOT NULL | 卷内位置标识（`'q1'` / `'q2.sub1'`——题组子题用点分路径）；**W3 起随题号印于卷面**（见 §3.3） |
| item_number | int | NOT NULL, CHECK > 0 | 题号（卷内顺序，1-based） |
| item_short_code | text | NOT NULL, UNIQUE | 题短码 = base32(SHA1(paper_item_id) 前 30 bit) 6 字符 + Luhn 校验位（共 7 字符），打印在卷面供学生/家长扫码/口述查源 |
| created_at | timestamptz | NOT NULL, server_default now() | 创建时间 |

联合唯一：`UNIQUE(paper_id, placement_token)`——同卷内位置标识唯一。

## 3. 不变式与规则

### 3.1 只增不改（D1 风格）

paper / paper_item 行只增不改：BEFORE UPDATE OR DELETE 触发器
（迁移 0009，`raise_paper_append_only_error()`）物理强制。
需改题 = 生成新卷 + 新 paper_item，历史卷永不 UPDATE/DELETE。

### 3.2 卷码与 QR

- `paper_code`：ULID（26 字符 Crockford base32）+ 1 位 Luhn 校验位；
  生成与校验：`trace_codes.generate_paper_code / verify_paper_code`。
- QR payload = `paper_spec_id` + Luhn 校验位；扫码后端反查 paper 表定位卷；
  payload 禁止含 item_version_id 等实例明文（防题目泄露）。

### 3.3 题短码与卷面印刷（W3 S9-① 补充）

- `item_short_code`：7 字符（6 base32 + 1 Luhn），全局唯一（UNIQUE 约束），
  反查入口：`paper_item.item_short_code → item_version → gate_certificate`。
- **卷面印刷**：组卷批处理（`src/core/render/weekly_batch.py`）把
  `placement_token` 与 `item_short_code` 透传进 RenderIR，由渲染模板
  （`src/core/render/templates/item.html` / `html_renderer.render_item`）
  以 `.item-trace` 行印于每题题尾；单题渲染（无卷上下文）不输出该行。

### 3.4 外键与删除纪律

两 FK 均为 RESTRICT：存在 paper_item 引用的 paper / item_version 不可删除
（与 D1「历史永不删除」一致，RESTRICT 为物理兜底）。

## 4. 与实现的对照

- DDL：`alembic/versions/0009_paper_trace.py`（逐字对齐本文 §2）。
- ORM：`src/core/models/paper.py` / `src/core/models/paper_item.py`。
- 码制：`src/core/render/trace_codes.py`（Luhn / base32 / QR SVG）。
- 组卷批处理：`src/core/render/weekly_batch.py`（paper/paper_item 行产出）。
- DDL 对照测试：`tests/unit/test_paper_trace.py`；
  卷面短码测试：`tests/unit/test_render_placement_token.py`。
