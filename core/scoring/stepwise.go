package scoring

// stepwise_rubric 评分器（scorer.yaml §88；Python 冻结实现
// src/core/scoring/platform_scorers.py StepwiseRubricScorer 的 Go 移植）。
//
// 结构化步骤 rubric：综合题拆有序步骤，每步独立判分、独立知识点归因
// （R-Q-15）。步骤分 = 子评分器判分口径分（Score ∈ [0,1]，Python
// dimension_scores["correct"] 同源）× max_score；总分与总满分按步累加，
// correct 口径 = total/max_total。
//
// 子评分器查找：只能来自注册表（D4）且必须是确定性评分器（scorer.yaml
// notes：「步骤级子评分器 id（本注册表现役确定性评分器）」）——AI 评分器
// （ai_rubric）作步骤级子评分在装配与执行两侧都被拒绝.

import (
	"context"
	"encoding/json"
	"fmt"

	"github.com/Cloudbird-Software/AI_Web_School/registry"
)

// versionStepwiseRubric 评分器版本（Python 冻结实现同值）.
const versionStepwiseRubric = "1.0.0+platform"

// StepwiseRubricScorer 是 stepwise_rubric 评分器（确定性；持注册表引用做
// 步骤级子评分器查找——Score 期查表，构造期不依赖表内容）.
type StepwiseRubricScorer struct {
	table *registry.ScorerTable
}

// NewStepwiseRubricScorer 构造分步评分器；注册表缺失直接报错——步骤级子
// 评分器只能来自注册表（D4），从构造期堵死.
func NewStepwiseRubricScorer(table *registry.ScorerTable) (*StepwiseRubricScorer, error) {
	if table == nil {
		return nil, fmt.Errorf("%w: stepwise_rubric 需注入评分器注册表（步骤级子评分器只能来自注册表，D4）", ErrInvalidInput)
	}
	return &StepwiseRubricScorer{table: table}, nil
}

// Entry 实现 registry.Scorer.
func (s *StepwiseRubricScorer) Entry() registry.Entry {
	return registry.Entry{ID: "stepwise_rubric", Version: versionStepwiseRubric}
}

// ScorerContract 实现 registry.Contracted（scorer.yaml §96 required=[steps]）.
func (s *StepwiseRubricScorer) ScorerContract() registry.ScorerSpec {
	return registry.ScorerSpec{
		Entry:         s.Entry(),
		InputSchema:   map[string]registry.ParamKind{"steps": registry.KindArray},
		Deterministic: true,
	}
}

// Score 执行分步判定与汇总。steps 为空是配置错误：显式失败
// （Python 返回置信度 0 结果，Go 按 fail-loud 纪律收紧为错误）.
func (s *StepwiseRubricScorer) Score(ctx context.Context, answer string, params map[string]any) (registry.ScoreResult, error) {
	rawSteps, ok := params["steps"].([]any)
	if !ok || len(rawSteps) == 0 {
		return registry.ScoreResult{}, fmt.Errorf("%w: stepwise_rubric steps 为空（无法判定≠判错）", ErrInvalidInput)
	}

	// 作答形态：{steps: [{step_id, response}]}（interaction.yaml
	// stepwise_process 标准形态）或裸数组 [{step_id, response}]（JSON 通道）.
	answers := map[string]any{}
	switch m := decodeAnswer(answer).(type) {
	case map[string]any:
		if steps, ok := m["steps"].([]any); ok {
			answers = collectStepAnswers(steps)
		}
	case []any:
		answers = collectStepAnswers(m)
	}

	total, maxTotal := 0.0, 0.0
	minConf := 1.0
	detail := []any{}
	inferences := []any{}
	for i, raw := range rawSteps {
		step, ok := raw.(map[string]any)
		if !ok {
			return registry.ScoreResult{}, fmt.Errorf("%w: stepwise_rubric steps[%d] 非 object", ErrInvalidInput, i)
		}
		stepID, _ := step["step_id"].(string)
		if stepID == "" {
			return registry.ScoreResult{}, fmt.Errorf("%w: stepwise_rubric steps[%d] 缺 step_id", ErrInvalidInput, i)
		}
		scorerID, _ := step["scorer"].(string)
		if scorerID == "" {
			return registry.ScoreResult{}, fmt.Errorf("%w: stepwise_rubric 步骤 %q 缺 scorer", ErrInvalidInput, stepID)
		}
		maxScore := 0.0
		if v, ok := step["max_score"]; ok && v != nil {
			f, ok := paramFloat(v)
			if !ok {
				return registry.ScoreResult{}, fmt.Errorf("%w: stepwise_rubric 步骤 %q max_score 非数值", ErrInvalidInput, stepID)
			}
			maxScore = f
		}
		maxTotal += maxScore

		sub, spec, ok := s.table.Get(scorerID)
		if !ok {
			return registry.ScoreResult{}, fmt.Errorf("%w: 步骤级子评分器 %q（D4：只能来自注册表）", ErrScorerNotFound, scorerID)
		}
		if !spec.Deterministic {
			return registry.ScoreResult{}, fmt.Errorf("%w: 步骤级子评分器 %q 须为确定性评分器（scorer.yaml stepwise_rubric notes）", ErrInvalidInput, scorerID)
		}
		subParams, ok := step["scorer_params"].(map[string]any)
		if !ok {
			subParams = map[string]any{}
		}
		// 子评分入参按其注册表声明面校验（与 Runner 同一声明面，口径不漂移）.
		if err := validateParams(spec, subParams); err != nil {
			return registry.ScoreResult{}, err
		}

		var subScore, subConf float64
		if subResp, answered := answers[stepID]; !answered {
			// 缺步作答：0 分，scoring 置信度不降级（确定缺失——Python 同语义）.
			subScore, subConf = 0.0, 1.0
			inferences = append(inferences, map[string]any{
				"error_type_id": "missing_step",
				"confidence":    1.0,
				"rule_version":  versionStepwiseRubric,
				"evidence":      map[string]any{"step_id": stepID},
			})
		} else {
			res, err := sub.Score(ctx, encodeSubResponse(subResp), subParams)
			if err != nil {
				return registry.ScoreResult{}, fmt.Errorf("scoring: stepwise_rubric 步骤 %q 子评分失败: %w", stepID, err)
			}
			if err := registry.ValidateResult(spec, res); err != nil {
				return registry.ScoreResult{}, fmt.Errorf("scoring: stepwise_rubric 步骤 %q: %w", stepID, err)
			}
			subScore, subConf = res.Score, res.Confidence
			inferences = appendSubInferences(inferences, stepID, res.EvidenceJSON)
		}

		points := subScore * maxScore
		total += points
		if subConf < minConf {
			minConf = subConf
		}
		detail = append(detail, map[string]any{
			"step_id":     stepID,
			"scorer":      scorerID,
			"max_score":   maxScore,
			"sub_correct": subScore,
			"points":      points,
			"kp":          step["kp"],
		})
	}

	ratio := 0.0
	if maxTotal > 0 {
		ratio = total / maxTotal
	}
	blob, err := json.Marshal(map[string]any{
		"steps":            detail,
		"max_total":        maxTotal,
		"error_inferences": inferences,
	})
	if err != nil {
		return registry.ScoreResult{}, fmt.Errorf("scoring: stepwise_rubric 证据序列化失败: %w", err)
	}
	return registry.ScoreResult{
		Correct:      maxTotal > 0 && ratio >= 1.0,
		Score:        ratio,   // 判分口径分 = total/max_total（Python dimension_scores["correct"] 同源）
		Confidence:   minConf, // 分步评分置信度 = 各步子评分置信度的最小值（串联取弱环）
		EvidenceJSON: string(blob),
	}, nil
}

// collectStepAnswers 把 [{step_id, response}] 收敛为 {step_id: response}
// （缺 step_id 的条目不可能与任何步骤对位，跳过——Python str(None) 键的
// 怪癖在 Go 面不移植）.
func collectStepAnswers(rawSteps []any) map[string]any {
	answers := make(map[string]any, len(rawSteps))
	for _, rs := range rawSteps {
		sm, ok := rs.(map[string]any)
		if !ok {
			continue
		}
		if sid, ok := sm["step_id"].(string); ok && sid != "" {
			answers[sid] = sm["response"]
		}
	}
	return answers
}

// encodeSubResponse 把步骤作答编码回子评分器的 string 载荷面（字符串原样，
// 其余 JSON 序列化——子评分器经 decodeAnswer 解回同构值）.
func encodeSubResponse(resp any) string {
	if s, ok := resp.(string); ok {
		return s
	}
	blob, err := json.Marshal(resp)
	if err != nil {
		return ""
	}
	return string(blob)
}

// appendSubInferences 从子评分器 evidence 中取出自报推断（error_inferences），
// 补 step_id 归因后并入总推断（Python ev.setdefault("step_id", ...) 同构）.
func appendSubInferences(dst []any, stepID, evidenceJSON string) []any {
	if evidenceJSON == "" {
		return dst
	}
	var ev map[string]any
	if err := json.Unmarshal([]byte(evidenceJSON), &ev); err != nil {
		return dst
	}
	infs, ok := ev["error_inferences"].([]any)
	if !ok {
		return dst
	}
	for _, inf := range infs {
		m, ok := inf.(map[string]any)
		if !ok {
			continue
		}
		entry := make(map[string]any, len(m)+1)
		for k, v := range m {
			entry[k] = v
		}
		e, ok := entry["evidence"].(map[string]any)
		if !ok || e == nil {
			e = map[string]any{}
		}
		if _, exists := e["step_id"]; !exists {
			e["step_id"] = stepID
		}
		entry["evidence"] = e
		dst = append(dst, entry)
	}
	return dst
}
