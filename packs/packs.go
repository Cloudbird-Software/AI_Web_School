// Package packs 定义学科包与学段包的装配契约（宪法 A5/D4，T-W5-031 骨架）。
//
// 学科包（SubjectPack）与学段包（GradeBandPack）是平台能力的"供给方"：
// 它们只能携带注册表条目（交互/评分器/参数），不能携带私造实现——
// 作答交互与评分器一律来自 registry（D4），packs 在此只做引用与参数化。
//
// 依赖方向（X6/GO-3 由 tools/go-lint/import-boundary 强制）：
// packs → registry ✓；core → packs ✗（核心域零学科特判）。
package packs

import "github.com/Cloudbird-Software/AI_Web_School/registry"

// SubjectPack 是学科包契约：某学科供给的交互类型与评分器集合。
// 学科差异的全部表达 = 注册表条目 + 参数，不 = core 里的分支。
type SubjectPack interface {
	// Entry 学科包自身也是版本化资产（可审计，§八）。
	Entry() registry.Entry
	// Interactions 该学科供给的作答交互类型（须为已注册条目的参数化复用）。
	Interactions() []registry.Interaction
	// Scorers 该学科供给的评分器（确定性 + AI 评分器，均须来自注册表）。
	Scorers() []registry.Scorer
}

// GradeBandPack 是学段包契约：学段只影响参数选择与呈现适配。
// 学段不引入新交互类型、不引入新评分器（呈现适配的物理约束除外，
// 如低学段免键盘作答——经参数表达，不经类型分叉表达）。
type GradeBandPack interface {
	// Entry 学段包自身也是版本化资产。
	Entry() registry.Entry
	// AppliesTo 学段覆盖的年级区间（含端点），供组卷过滤。
	AppliesTo() (minGrade, maxGrade int)
}
