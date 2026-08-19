// Package packs 定义学科包与学段包的装配契约（宪法 A5/D4，T-W5-031 骨架）。
//
// 学科包（SubjectPack）与学段包（GradeBandPack）是平台能力的"供给方"：
// 它们只能引用注册表条目（交互/评分器），不能携带私造实现——
// 作答交互与评分器一律来自 registry（D4），packs 在此只做引用与参数化。
// 引用必须经 ResolveInteractions/ResolveScorers 在对应注册表内解析：
// 未注册 id 在装配期即失败，私造实现没有进入 core 的类型通道
// （契约上拿 registry.Interaction/Scorer 值的唯一途径就是注册表本身）。
//
// 依赖方向（X6/GO-3 由 tools/go-lint/import-boundary 强制）：
// packs → registry ✓；core → packs ✗（核心域零学科特判）。
package packs

import (
	"fmt"

	"github.com/Cloudbird-Software/AI_Web_School/registry"
)

// InteractionRef 是对已注册作答交互类型的引用 + 参数化（D4）。
// 学科包不携带 Interaction 实现——实现只能经 registry.Register 进平台。
type InteractionRef struct {
	ID     string         // registry.Interaction 条目 id（须已注册）
	Params map[string]any // 参数化复用（如选项数上限、呈现约束）
}

// ScorerRef 是对已注册评分器的引用 + 参数化（D4）。
// 学科包不携带 Scorer 实现——确定性/AI 评分器一律来自注册表。
type ScorerRef struct {
	ID     string         // registry.Scorer 条目 id（须已注册）
	Params map[string]any // 参数化复用（如容差、置信阈值）
}

// SubjectPack 是学科包契约：某学科供给的交互类型与评分器集合。
// 学科差异的全部表达 = 注册表条目引用 + 参数，不 = core 里的分支。
type SubjectPack interface {
	// Entry 学科包自身也是版本化资产（可审计，§八）。
	Entry() registry.Entry
	// Interactions 该学科供给的作答交互类型（已注册条目的参数化引用）。
	Interactions() []InteractionRef
	// Scorers 该学科供给的评分器（确定性 + AI 评分器，均为已注册条目的参数化引用）。
	Scorers() []ScorerRef
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

// ResolveInteractions 把交互引用解析为注册表条目——pack 装配进 core 的
// 唯一通道（D4）。未注册 id → 装配失败：学科包引用了平台未登记的私造实现。
func ResolveInteractions(
	reg *registry.Registry[registry.Interaction],
	refs []InteractionRef,
) ([]registry.Interaction, error) {
	out := make([]registry.Interaction, 0, len(refs))
	for _, ref := range refs {
		v, ok := reg.Get(ref.ID)
		if !ok {
			return nil, fmt.Errorf(
				"packs: interaction %q 未在注册表登记（D4 禁止私造：条目须先经 registry.Register）",
				ref.ID,
			)
		}
		out = append(out, v)
	}
	return out, nil
}

// ResolveScorers 把评分器引用解析为注册表条目（同 ResolveInteractions）。
func ResolveScorers(
	reg *registry.Registry[registry.Scorer],
	refs []ScorerRef,
) ([]registry.Scorer, error) {
	out := make([]registry.Scorer, 0, len(refs))
	for _, ref := range refs {
		v, ok := reg.Get(ref.ID)
		if !ok {
			return nil, fmt.Errorf(
				"packs: scorer %q 未在注册表登记（D4 禁止私造：条目须先经 registry.Register）",
				ref.ID,
			)
		}
		out = append(out, v)
	}
	return out, nil
}
