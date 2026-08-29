// Package assembly 是组卷引擎域的 Go 移植（PyR 波；Python 冻结语义基准
// src/core/assembly/ 全量，逐函数对齐）。
//
// 组卷引擎 = 约束集四维编译（profile.go）+ 候选筛选（candidates.go）
// + 确定性预算装填求解（heuristic.go）+ 曝光账本双轨（exposure.go）
// + 学段约束 overlay（gradeband_constraints.go）+ 听力 overlay
// （listening_overlay.go）+ 测量卷线（spec_table.go / solver.go /
// measurement_paper.go）。
//
// 冻结语义保留（架构 v2 §4.4）：
//   - R-Z-01：三用途同一引擎同一题库，差异收敛为版本化 Profile；
//     确定性 = 快照 id + Profile 版本 + 种子（stable_key = sha256(seed:id)，
//     非 random.shuffle，跨进程可复现）；
//   - R-Z-02：题量/知识点配比/目标正确率区间/序列梯度单调/曝光互斥/题组≤6；
//   - R-Z-03：诊断 Profile 孤立题强制、每知识点≥3、多点关系声明核验；
//     已知冲突（约20题×每点≥3）编译期软目标化裁决并留档；
//   - §4.4 铁律：不可行返回结构化冲突原因（InfeasibleError.Report /
//     CpSatInfeasible），禁止静默放松。
//
// 本包是纯逻辑层：不接 DB。serving 候选池与曝光账本经查询面端口注入
// （CandidateStore / ExposureStore，Memory 实现供测试；PG 实现属装配层）。
// Profile digest 与 SpecTable JSON 序列化按 Python json.dumps(sort_keys)
// 规则规范化，跨实现指纹可比（canon.go）。
//
// 宪法 A5/A7/X6：本包不 import 任何学科包/学段包（学科零特判）；
// 学科 overlay 以 map[string]any 传入（调用方从包内 yaml 加载）。
package assembly
