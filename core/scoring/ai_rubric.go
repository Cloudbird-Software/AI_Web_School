package scoring

// ai_rubric 评分器（scorer.yaml §134；Python 冻结实现
// src/core/scoring/ai_rubric_scorer.py 的 Go 移植）。
//
// 量规即数据：解析量规 → 按维度构建评分 prompt → 经注入的出站执行面调用
// 强模型 → 解析逐维分数+理由+置信度。本评分器不直接调用任何 LLM 供应商
// SDK（X6 等价约束）——AI 调用经 core/ai 的 Caller 接口注入（生产由装配层
// 把总线装订的执行面（含 D10 台账）适配为 Caller；测试注入 fake），与在线
// 评分走同一注入面，台账口径一致。
//
// D10 双向强制（registry.ValidateResult）：AI 评分结果必须携带
// model/model_version——模型身份在构造期注入并随结果固定.
//
// 上线四步（scorer.yaml ai_rubric.notes）：公开基准验证 → 影子运行 →
// 抽检伴随 → 灰度；影子模式见 shadow.go.

import (
	"context"
	"encoding/json"
	"fmt"
	"sort"
	"strings"

	"github.com/Cloudbird-Software/AI_Web_School/core/ai"
	"github.com/Cloudbird-Software/AI_Web_School/registry"
)

const (
	// versionAIRubric 评分器版本（Python 冻结实现同值；重判时据此写平行
	// score_run，R-D-05）.
	versionAIRubric = "1.0.0+ai-rubric"

	// DefaultTaskLevel ai_rubric 评分器的 AI 总线档位缺省值（L2 强模型生产
	// 与裁判——架构 v2 §4.8）。档位路由在装配层完成：注入的 Caller 已是
	// 档位选定的执行面，本评分器只校验 tier 词表防参数越域.
	DefaultTaskLevel = "L2"
)

// AIRubricConfig 是 ai_rubric 的装配面（D10 台账模型身份的构造期注入点）.
type AIRubricConfig struct {
	// Caller 出站执行面（生产=总线装订适配器；测试=fake）。nil 拒绝构造.
	Caller ai.Caller
	// Target 出站目标名（allowlist 键，多路 Caller 复用一个执行面时辨向）.
	Target string
	// Model/ModelVersion 模型标识与版本（D10 台账两维度，随结果固定）.
	Model        string
	ModelVersion string
	// PromptVersion prompt 版本锚（空取 ai.DefaultPromptVersion）；落
	// scoring_trace——十年后回答「当时用什么提示词给的分」.
	PromptVersion string
}

// AIRubricScorer 是 ai_rubric 评分器（非确定性）.
type AIRubricScorer struct {
	caller        ai.Caller
	target        string
	model         string
	modelVersion  string
	promptVersion string
}

// NewAIRubricScorer 构造 AI 量规评分器；Caller 与 D10 模型身份缺失直接报错
// ——不能定位模型版本的 AI 评分就是违宪产物（D10），从构造期堵死.
func NewAIRubricScorer(cfg AIRubricConfig) (*AIRubricScorer, error) {
	if cfg.Caller == nil {
		return nil, fmt.Errorf("%w: ai_rubric 缺 Caller（AI 调用必须经注入的出站执行面）", ErrInvalidInput)
	}
	if cfg.Target == "" || cfg.Model == "" || cfg.ModelVersion == "" {
		return nil, fmt.Errorf("%w: ai_rubric 缺 target/model/model_version（D10 台账要素，缺一不可）", ErrInvalidInput)
	}
	pv := cfg.PromptVersion
	if pv == "" {
		pv = ai.DefaultPromptVersion
	}
	return &AIRubricScorer{
		caller:        cfg.Caller,
		target:        cfg.Target,
		model:         cfg.Model,
		modelVersion:  cfg.ModelVersion,
		promptVersion: pv,
	}, nil
}

// Entry 实现 registry.Scorer.
func (s *AIRubricScorer) Entry() registry.Entry {
	return registry.Entry{ID: "ai_rubric", Version: versionAIRubric}
}

// ScorerContract 实现 registry.Contracted（scorer.yaml §142 required=[rubric]；
// 非确定性评分器必须声明 prompt 版本——D10 注册期投影）.
func (s *AIRubricScorer) ScorerContract() registry.ScorerSpec {
	return registry.ScorerSpec{
		Entry:         s.Entry(),
		InputSchema:   map[string]registry.ParamKind{"rubric": registry.KindObject},
		Deterministic: false,
		PromptVersion: s.promptVersion,
	}
}

// ScoreRubric 是 AI 量规评分主入口（T-W4-019 验收①语义）：解析量规 → 构建
// prompt → 出站调用 → 解析响应。modelTier 须在契约词表 {L2, L3}（scorer.yaml
// params_schema.model_tier enum）；gradeBand 影响 prompt 上下文、不改量规分值.
func (s *AIRubricScorer) ScoreRubric(ctx context.Context, responseText string, rubric map[string]any, modelTier, gradeBand string) (*AIRubricScore, error) {
	switch modelTier {
	case "", DefaultTaskLevel, "L3":
	default:
		return nil, fmt.Errorf("%w: ai_rubric model_tier %q 越域（须为 L2/L3）", ErrInvalidInput, modelTier)
	}
	parsed, err := ParseRubric(rubric)
	if err != nil {
		return nil, err
	}
	prompt := BuildScoringPrompt(responseText, parsed, gradeBand)

	out, err := s.caller.Call(ctx, ai.OutboundRequest{
		Target: s.target,
		Model:  s.model,
		Prompt: prompt,
	})
	if err != nil {
		return nil, fmt.Errorf("scoring: ai_rubric 出站调用失败: %w", err)
	}
	return ParseAIResponse(out.Content, parsed), nil
}

// Score 实现 registry.Scorer：作答文本提取 → ScoreRubric → ScoreResult 五要素
// 投影（registry.ScoreResult 最小契约）。
//
// 口径：Score = total_score（Python dimension_scores["total"] 同源，分值点数）；
// Correct = 整体置信度未越人工复核阈值（Python 冻结实现该键恒 False 的退化
// 行为不移植——低置信待复核不计为有效判定更有信息量）；evidence 随行逐维
// 理由与低置信推断（契约 output_schema 的 error_inferences 随 evidence 键
// 落 trace，W6 拆键只增不改）.
func (s *AIRubricScorer) Score(ctx context.Context, answer string, params map[string]any) (registry.ScoreResult, error) {
	rubric, ok := params["rubric"].(map[string]any)
	if !ok || rubric == nil {
		return registry.ScoreResult{}, fmt.Errorf("%w: ai_rubric 缺 rubric（禁止静默判错）", ErrInvalidInput)
	}
	modelTier, _ := params["model_tier"].(string)
	gradeBand, _ := params["grade_band"].(string)
	// Go 面无 item_version 入参：学段经可选 params.grade_band 声明，缺省 M
	// （Python _extract_grade_band 兜底同值）.
	if gradeBand == "" {
		gradeBand = "M"
	}

	scored, err := s.ScoreRubric(ctx, responseTextOf(answer), rubric, modelTier, gradeBand)
	if err != nil {
		return registry.ScoreResult{}, err
	}

	dimEvidence := make([]any, 0, len(scored.Dimensions))
	for _, d := range scored.Dimensions {
		dimEvidence = append(dimEvidence, map[string]any{
			"name":       d.Name,
			"score":      d.Score,
			"max":        d.Max,
			"rationale":  d.Rationale,
			"confidence": d.Confidence,
		})
	}
	evidence := map[string]any{
		"dimensions":         dimEvidence,
		"total_score":        scored.TotalScore,
		"total_max":          scored.TotalMax,
		"overall_confidence": scored.OverallConfidence,
		"needs_human_review": scored.NeedsHumanReview,
	}
	if scored.NeedsHumanReview {
		// 低置信自动转人工复核队列（验收②；threshold 随行便于回放判读）.
		evidence["error_inferences"] = []any{map[string]any{
			"error_type_id": "low_confidence_needs_human_review",
			"confidence":    scored.OverallConfidence,
			"rule_version":  versionAIRubric,
			"evidence": map[string]any{
				"overall_confidence": scored.OverallConfidence,
				"threshold":          HumanReviewConfidenceThreshold,
			},
		}}
	}
	blob, err := json.Marshal(evidence)
	if err != nil {
		return registry.ScoreResult{}, fmt.Errorf("scoring: ai_rubric 证据序列化失败: %w", err)
	}
	return registry.ScoreResult{
		Correct:      !scored.NeedsHumanReview,
		Score:        scored.TotalScore,
		Confidence:   scored.OverallConfidence,
		Model:        s.model,
		ModelVersion: s.modelVersion,
		EvidenceJSON: string(blob),
	}, nil
}

// responseTextOf 从作答载荷提取文本：裸字符串直接用；{text} / {blanks} 形态
// 解串提取（Python _extract_response_text 同构；blanks 键排序拼接保确定性）.
func responseTextOf(answer string) string {
	switch v := decodeAnswer(answer).(type) {
	case string:
		return v
	case map[string]any:
		if t, ok := v["text"].(string); ok && t != "" {
			return t
		}
		if blanks, ok := v["blanks"].(map[string]any); ok && len(blanks) > 0 {
			keys := make([]string, 0, len(blanks))
			for k := range blanks {
				keys = append(keys, k)
			}
			sort.Strings(keys)
			parts := make([]string, 0, len(keys))
			for _, k := range keys {
				if vm, ok := blanks[k].(map[string]any); ok {
					if val, ok := vm["value"]; ok && val != nil {
						parts = append(parts, scalarString(val))
					}
				} else if blanks[k] != nil {
					parts = append(parts, scalarString(blanks[k]))
				}
			}
			return strings.Join(parts, " ")
		}
		return answer
	default:
		return answer
	}
}
