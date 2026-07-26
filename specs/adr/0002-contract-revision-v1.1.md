# ADR-0002：L1 契约修订 v1.0 → v1.1（专家审查裁决）

- 状态：已接受（2026-07-26，人类预批准；人类签署后生效）
- 依据：宪法 P5（契约变更申请→影响面分析→人类批准）；变更申请=双专家独立审查报告（2026-07-26）

## 变更清单（15 项，明细见 specs/contracts/TRACEABILITY.md §5）

**阻断级**
1. 补 `material_version` 表（素材身份+版本两段式）——修复对 D1「Item/Material/Corpus 全版本化」的违反
2. 修复双注册表唯一双向断裂（stepwise_rubric.input_contract 补 drawing_operation），并将双向闭合固化为契约测试

**内部矛盾**
3. `rerun_of` 移出 response_event.scoring_trace，归属 score_run 独立表（W1 数据域契约）
4. `duration_ms`/`session_id` 改可空（NULL=未知/无会话），消除纸卷回录（S2）造数据压力

**歧义裁决（5–10）**
5. `current_version_id` = 最新 published 版本指针，仅发布事务可更新（触发器兜底）
6. `gate_certificate_id` 以列字段为唯一真源，lineage 不再重复存储
7. C/D 级 item_version_id 升级为内容寻址（H(canonical 快照)），D3 精神扩展
8. 状态机明示无回边（quarantined 失败版本永久留存）
9. `rendered_snapshot` 进入 quarantined 前必填（CHECK/触发器承载）
10. keypoint_hit 正则方言锁定 Python re 子集

**小问题（11–15）**：scorer_version 编辑事故修复；循环 FK 与分区 PK 实现注记；stepwise_process 子步骤收敛注明；双注册表 required_fields 补 summary；item-model 补 objective/lineage 机器 schema（§5）。

**测试核对中新发现的第二处断裂**（专家报告之外）：math_equivalence.input_contract 点名 text_blank 但未反向声明——text_blank.compatible_scorers 已补 math_equivalence。

## 影响面分析

| 受影响方 | 影响 | 处置 |
|---|---|---|
| W1 任务卡（未开工） | T-W1-002 表清单 +material_version/+corpus_version；T-W1-003 实体数 7→9；T-W1-005 需 DB 层强制；T-W1-006 gate_run 加 policy_version；T-W1-008 grep 模式修正 | 已同步修订任务卡 |
| 既有契约测试 | response_event CORE_FIELDS 变更；新增双向闭合/material 版本/schema 断言 | 已同步，35 项全绿 |
| W0 已交付物 | 无代码依赖契约变更部分（alembic 0001 为占位表，将被 W1 新迁移结构替换） | 无影响 |
| 数据库现状 | 本地开发库仅有占位表，W1 迁移按新契约落地 | 无影响 |

## 结论

变更收益（消除 D1 违反、双向断裂、重判语义矛盾、纸卷链路数据污染）显著大于成本（W1 未开工，无返工）。裁决：接受全部 15+1 项修订。

- 批准：待人类签署（____）
