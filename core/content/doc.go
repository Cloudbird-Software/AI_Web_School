// Package content 承载内容生产线的核心域：母题/实例 → 校验门 → 已发布区
// （三本账之"内容版本账"，宪法 A2/A7；append-only 物理强制见 T-W5-001/002）。
//
// DB 层物理强制范围以迁移 0024（内容版本账 append-only）为准，与 W5-R 之前
// 的"DB 触发器兜底"笼统说法区分（A8 注释只可宣称已实证的范围）：
//   - item_template_version / material_version / corpus_version / passage：
//     整表拒绝 UPDATE/DELETE（BEFORE ... FOR EACH STATEMENT 触发器，复用
//     raise_append_only_error）。
//   - item_version：不挂整表触发器——契约 §4 允许受控状态机字段前移
//     （status/gate_certificate_id/published_at/retired_at，无回边）；历史由
//     publication 签发账与 item_lifecycle_transition（0018，均 append-only）
//     承载，内容快照不可变靠 D3 换行新 id。
//   - item/item_template/material/corpus_asset 指针表：current_version_id 前
//     移是合法 UPDATE，不在账表之列。
//
// W5-R 骨架期仅锚定包语义与依赖方向；表结构消费经 db 层（T-W5-032），
// 学科差异经 registry 条目表达，本包零学科特判（X6）。
package content
