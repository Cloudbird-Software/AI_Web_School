package scoring

// 平台评分器装配（scorer.yaml 冻结注册表的 Go 实现注册面；PyR 评分域补全）。
//
// 注册清单（id 与 status 对齐冻结的 scorer.yaml，AI_Web_School/registry
// ScorerTable 契约校验为准入门）：
//
//	id                 确定性  Go 实现
//	exact_match        true    ExactMatchScorer
//	math_equivalence   true    MathEquivalenceScorer
//	keypoint_hit       true    KeypointHitScorer
//	stepwise_rubric    true    StepwiseRubricScorer
//	ai_rubric          false   AIRubricScorer（Caller 注入）
//
// 不注册条目（结构对齐注册表，非遗漏）：
//   - human_confirm：ADR-0005 已申请废弃为 model_arbiter（人工结论无
//     model_version/prompt 版本，破坏 D10 可回放统一口径）——本形态过不了
//     ScorerTable 装配门，不提供注册通道；
//   - asr_oral：scorer.yaml status=reserved，首年不实现.

import (
	"github.com/Cloudbird-Software/AI_Web_School/registry"
)

// RegisterDeterministicScorers 注册四个确定性评分器（exact_match /
// math_equivalence / keypoint_hit / stepwise_rubric）。AI 面（ai_rubric）
// 由装配方经 NewAIRubricScorer + RegisterAIRubricScorer 显式补注册——
// Caller 缺失的装配不许带病入库.
func RegisterDeterministicScorers(tb *registry.ScorerTable) error {
	if err := tb.Register("exact_match", NewExactMatchScorer()); err != nil {
		return err
	}
	if err := tb.Register("math_equivalence", NewMathEquivalenceScorer()); err != nil {
		return err
	}
	if err := tb.Register("keypoint_hit", NewKeypointHitScorer()); err != nil {
		return err
	}
	// stepwise 持表引用做步骤级子评分器查找：先注册其余评分器再装配，
	// 步骤引用的子评分 id 在 Score 期从表内解析（D4）.
	sw, err := NewStepwiseRubricScorer(tb)
	if err != nil {
		return err
	}
	return tb.Register("stepwise_rubric", sw)
}

// RegisterAIRubricScorer 注册 AI 量规评分器（Caller/模型身份由装配面注入；
// D10：非确定性条目必须声明 prompt 版本，构造期已强制）.
func RegisterAIRubricScorer(tb *registry.ScorerTable, scorer *AIRubricScorer) error {
	return tb.Register("ai_rubric", scorer)
}

// NewPlatformScorerTable 装配 scorer.yaml 五个现役评分器的平台评分表
// （AI 面经 AIRubricConfig 注入；任一注册失败即整体失败——无静默残表）.
func NewPlatformScorerTable(cfg AIRubricConfig) (*registry.ScorerTable, error) {
	tb := registry.NewScorerTable()
	if err := RegisterDeterministicScorers(tb); err != nil {
		return nil, err
	}
	ar, err := NewAIRubricScorer(cfg)
	if err != nil {
		return nil, err
	}
	if err := RegisterAIRubricScorer(tb, ar); err != nil {
		return nil, err
	}
	return tb, nil
}
