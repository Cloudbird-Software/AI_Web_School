# 契约 ↔ 架构 v2 对照表（T-W0-004 验收标准 2）

> 用途：证明 L1 首批契约的每个结构可逐项回溯到架构 v2 章节。审查时逐行核对。
> 冲突裁决：契约与架构 v2 不一致处以 `specs/constitution.md` 为准；本表记录设计澄清点。

## 1. registries/interaction.yaml

| 契约元素 | 架构 v2 来源 | 备注 |
|---|---|---|
| 10 现役 + 2 预留（12 种） | §2.3「首年实现 10 种…预留注册不实现：书写抄写、口语」；§2.1 V1 维度 | — |
| 判断题=单选预设（非独立类型） | §2.3 正文 10 种清单无「判断」；§2.1 矩阵表「单选/多选/判断」并列 | **设计澄清**：以正文清单为准，矩阵表的并列是题型族表述；`true_false` 作 single_choice 的 preset |
| 每类型四要素（response_schema/render_component/paper_spec/scoring_input） | §2.3「每种交互 = 作答采集 schema + 在线渲染组件 + 纸卷呈现规范 + 评分输入契约」 | — |
| numeric_blank 规范化数值字符串 | §1.3 取舍表「实例身份内容寻址」+ 评审报告 D2「定点/分数运算避免浮点漂移」 | — |
| stepwise_process 步骤级作答/判分 | §2.2「综合题分步（R-Q-15）」；§4.5「结构化分步录入/答题区」 | — |
| drawing_operation 简化（结构化元素，非自由画笔） | §2.3「作图操作（简化：结构化元素点选/涂色，非自由画笔）」 | — |
| handwriting_copy 拍照+人确认过渡 | §2.3；§10 开放项 12 | — |
| oral 预留不实现 | §1.3「口语：注册接口、首年不实现」 | — |

## 2. registries/scorer.yaml

| 契约元素 | 架构 v2 来源 | 备注 |
|---|---|---|
| 6 现役 + 1 预留（7 种） | §2.3「6 种现役…预留：ASR 口语」；§2.1 V2 维度 | — |
| 统一契约签名与输出五要素 | §2.3「score(response, item_version, params) -> {维度分, 错误类型推断, 置信度, 证据, scorer_version}」 | — |
| 置信度四层分离 | §4.5「图像识别/评分/错误推断/掌握结论各自独立记录」；评审报告 GPT 独有 | output.confidence 承载识别/评分两层；推断层在 error_inferences[].confidence；掌握层属数据域 |
| deterministic 标志 | §4.7「评分逻辑（含 AI 评分）版本化：历史作答在新版本评分逻辑下可重判」（R-D-05） | 确定性评分器是重判可复现的前提 |
| math_equivalence 双实现 | §4.3「双实现独立重算（验算器与实例化引擎不共享代码）」 | — |
| ai_rubric 上线四步 | §4.5「公开基准验证→影子运行→抽检伴随→灰度」 | — |
| human_confirm 与抽检共用工作台 | §4.5「低置信自动转人工队列（与抽检共用工作台）」 | — |

## 3. events/response_event.md

| 契约元素 | 架构 v2 来源 | 备注 |
|---|---|---|
| 字段全要素（§1 字段表） | §4.7「全要素字段：事件 id、匿名学生 id、item_version_id、场景、原始作答载荷、耗时、评分轨迹、错误推断、testlet_id、会话 id、播放行为、时间戳」 | — |
| source_ref 来源追溯 | §4.6「追溯链：题码→render_run→卷 Spec→ItemVersion」；A4 | **设计补充**：§4.7 字段列表未明示来源字段，A4 入水口要求必须有；取 paper_id + placement_token / assembly_run_id 二形态 |
| append-only + 按月分区 + DB 权限 | §4.7「按月分区 append-only（DB 权限禁 UPDATE/DELETE+定期哈希锚定）」；D1 | — |
| Parquet 每日增量归档 | §4.7「每日增量导出 Parquet 至对象存储+schema 注册表」 | — |
| scene 三值与分场景禁混估 | §4.7 参数标定；D5；评审报告 D4（练习暴露偏差仅粗校准） | — |
| 重判写平行 score_run | §4.7「新 scorer 重放历史事件写平行 score_run，原序列不动；增量重判」（R-D-05） | — |
| 仅 student_alias_id | §4.8「学生仅 student_alias_id」；D7 | — |

## 4. db/item-model.md

| 契约元素 | 架构 v2 来源 | 备注 |
|---|---|---|
| Item/ItemVersion 六块结构 | §2.2 统一内容模型图（objective/interaction_ref/content/scoring_ref/error_bindings/lineage） | — |
| 四级生产线 tier 为谱系字段 | §2.2「四级产物的 ItemVersion 结构完全一致——tier 只是谱系字段」；A7 | — |
| 内容寻址公式（含 corpus/引擎/pack digest 链 + locale） | §2.2「instance_id = H(模板版本, 规范化参数, 引擎/学科包/语料库 digest, locale)」；评审报告 D2（GPT digest 链最完整） | — |
| 状态机 draft→quarantined→published→retired | §4.3「产物状态机」 | — |
| published_at 触发器强制 | §4.3「数据库触发器强制 published_at 非空必伴随合法 gate_certificate_id」；D2 | — |
| rendered_snapshot 渲染快照 | 评审报告 D2「实例物化时连同渲染文本快照入库」（FB 要求，校验门受检对象） | — |
| kp_set_mode 三值 | §2.2「kp_set_mode（single/all_required/compensatory）」；R-Q-14；评审报告 D8（compensatory 只佐证不定位） | — |
| 退役是状态不是删除 | §2.2「退役是状态不是删除」；R-Q-26 | — |
| 题组 ≤6 | §4.4 约束目录「题组≤6」；R-Z-06 | — |
| gate_certificate 表结构不在本卡 | §4.3（属校验签发账/W1 状态机契约） | 任务卡 non_goals：状态机契约 W1 |

---

## 5. 修订记录（v1.1，ADR-0002 · 专家审查裁决）

> 来源：双专家独立审查反馈（2026-07-26）。两条阻断级问题 + 两条内部矛盾全部采纳；歧义点 5–10 全部裁决；小问题全部修复。本次修订经人类预批准，人类签署后生效。

| # | 问题 | 裁决 | 落点 |
|---|---|---|---|
| 1 | material 无版本表，违反 D1「Item/Material/Corpus 全版本化」 | **补 `material_version` 表**：素材与 Item 同构（身份+不可变版本两段式），题组/题目引用 `material_version_id` | item-model §1/§2.4/§7 |
| 2 | `drawing_operation.compatible_scorers` 含 `stepwise_rubric`，但后者 input_contract 未声明——双向断裂 | **补 input_contract**（架构矩阵 B.1「操作题=作图操作×分步+人确认」证明该组合存在）；**双向闭合新增为契约测试**（机器校验，不再依赖人肉） | scorer.yaml；tests/contract/registries/ |
| 3 | `rerun_of` 放在不可变的 scoring_trace 中，永远写不进 | 从 response_event §3 结构中删除；**`rerun_of` 属 score_run 独立表**（W1 数据域契约，本行登记归属） | response_event §3/§6 |
| 4 | `duration_ms`/`session_id` 必填与纸卷回录（S2）冲突，逼链路造数据 | 双双改**可空**：NULL=未知/无会话；禁止填 0 或伪造会话（耗时是健康度监控维度）；S2 批次标识放 `source_ref.batch_id` | response_event §1/§5 |
| 5 | `current_version_id` 语义未定义 | 定义为**最新 published 版本指针**；仅发布事务可更新（W1 触发器兜底），应用层直写触发审计告警 | item-model §2.1/§6.3 |
| 6 | `gate_certificate_id` 列字段与 lineage 双存 | **列字段为唯一真源**，lineage 内不再重复存储 | item-model §2.2/§2.2.2 |
| 7 | C/D 级 id 非内容寻址，同内容可得不同 id | **升级为内容寻址**（§3 公式二：H(canonical 内容快照)）；重复内容入库作去重提示而非拒绝——D3 精神扩展至 C/D | item-model §3 |
| 8 | 状态机 quarantined 失败后去向不明 | **无回边**：失败版本永久留存（审计证据），修改=新 draft 版本 | item-model §4.2 |
| 9 | `rendered_snapshot` 可空但它是门受检对象 | **进入 quarantined 前必填**（W1 以 CHECK/触发器承载） | item-model §2.2 |
| 10 | keypoint_hit 正则方言未指定，重放可复现性风险 | **锁定 Python re 子集**（禁后向引用/原子组/条件断言等实现相关特性） | scorer.yaml keypoint_hit |
| 11 | scorer_version flow mapping 跨行断裂（编辑事故） | 已修为一行 | scorer.yaml |
| 12 | item↔item_version 循环 FK、response_event 分区 PK 含分区键 | 补实现注记（DEFERRABLE/后加约束；PK=(event_id, created_at)） | item-model §6；response_event §2 |
| 13 | stepwise_process 子步骤仅三种交互——有意或遗漏？ | **有意收敛**（首年结构化录入保证评分确定性），契约已注明 | interaction.yaml stepwise_process |
| 14 | summary 全条目都有但未进 required_fields | 双注册表 required_fields 均补 summary | 双 yaml |
| 15 | item-model 的 objective/lineage 只有示例无机器 schema | **补齐 JSON Schema**（§5.1/§5.2，含多知识点模式、A/B 级 params 必填等约束） | item-model §5 |
| 16 | §2.5 corpus_version 行缺状态机/门字段，与 §2.4 material_version 不对齐（v1.1 修订时本行遗漏） | **补字段** `status`（同 §4 状态机四态）/ `gate_certificate_id`（唯一真源，纪律同 §2.2）/ `published_at` / `retired_at`，与 §2.4 material_version 对齐（v1.1.1） | item-model §2.5 |
