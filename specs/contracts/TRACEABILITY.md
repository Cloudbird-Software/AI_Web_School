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
