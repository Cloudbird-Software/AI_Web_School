// Package report 承载弱项报告聚合核心域（W3 S5；Python 冻结实现
// src/core/report/ 的 Go 重锚定）：贝叶斯累积聚合纯函数核 + 报告类型。
//
// 北极星（V1）：让家长一眼知道孩子哪里弱——按错误类型聚合作答事件中的
// 错误推断，输出「结论已定 / 证据不足」两级状态（D8 不排名：等级非分数，
// 条目排序只为展示确定性，不构成能力名次）。
//
// 贝叶斯累积（架构 §4.5「多题证据贝叶斯累积，报告置信度即后验」）：
//   - 每错误类型一个 Beta(α, β) 后验，先验 Beta(1, 1)（无信息均匀先验）
//   - 每条错误推断（error_inferences[] 元素）是一次证据：α += confidence，
//     β += 1 - confidence（置信度即该证据支持归因的强度）
//   - 报告置信度 = 后验均值 α / (α + β)
//
// 为什么置信度加权而非 0/1 计数：契约 §4 的 confidence 是规则给出的推断强度
// （§4.5 置信度四层分离之推断层），直接计入后验让弱证据自然稀释——
// 「选某项是证据非因果」，高置信孤立题证据与低置信 compensatory 佐证
// 对后验的拉动应当不同。
//
// 证据阈值：evidence_count < min_evidence ⇒ 「证据不足」（§4.7 允许输出
// 证据不足）。v1 的 min_evidence 默认 3（诊断 Profile 每知识点 ≥3 孤立题的
// 同源直觉：3 条独立证据以下不做定论）。
//
// 已知限制（v1 明示，与冻结实现同口径）：未区分孤立题/compensatory 题的
// 定位效力（D8「compensatory 只佐证不定位」由规则置信度间接承载），证据
// 强度完全委托给 error_inferences[].confidence。聚合键是 error_type_id——
// 知识点（kp）维度由错误类型的 kp 归属承载，v1 不做 kp×error_type 二维展开。
//
// 场景口径（D5）：分场景取数在取数层（SQL WHERE）定型，聚合层不混合后再拆；
// 报告如实回显取数口径（Scene），跨场景汇总必须由调用方显式选择。
//
// IO 面（显式留白，本波不在 Go 核心域实现）：response_event 取数、
// recommend_practice（已发布实例池按 error_bindings 查题组 5 题小卷）、
// 报告落库/序列化出口（Python service.py 的 AsyncSession 面）；测量卷报告
// （measurement_report.py + CTT 统计）属估计器域，不在本包。
package report
