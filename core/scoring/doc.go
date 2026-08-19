// Package scoring 承载评分核心域：评分器只能来自 registry（D4）、
// AI 评分必须落 scoring_trace 且可回放（D10；T-W5-016）。
//
// 参数按 source（先验/实测）与场景（practice/diagnosis/measurement）
// 分开管理，禁止混估（宪法第一部分）。
package scoring
