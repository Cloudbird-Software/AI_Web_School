package scoring

// keypoint_hit 评分器（scorer.yaml §111；Python 冻结实现
// src/core/scoring/platform_scorers.py KeypointHitScorer 的 Go 移植）。
//
// 关键词/要点 + 规则判定（简答/句式转换/阅读要点题）。patterns 元素：
// 普通字符串=子串命中（规范化后）；"re:" 前缀=正则命中。正则方言锁定：
// 契约禁用后向引用/原子组/条件断言等实现相关特性——Go RE2 结构性拒绝
// 后向引用与前向断言，方言越界在编译期显式失败（不会静默误判）.

import (
	"context"
	"encoding/json"
	"fmt"
	"regexp"
	"sort"
	"strings"

	"github.com/Cloudbird-Software/AI_Web_School/registry"
)

const (
	// versionKeypointHit 评分器版本（Python 冻结实现同值）.
	versionKeypointHit = "1.0.0+platform"

	// DefaultKeypointInferConfidence 未命中关键点→错误类型推断的默认置信度。
	// 为什么 <1.0：规则判定是确定性的（scoring 层置信度 1.0），但「未命中某
	// 关键点→某种错误理解」是证据非因果的推断（架构 v2 §4.5），推断层置信度
	// 须如实 <1.
	DefaultKeypointInferConfidence = 0.8
)

// KeypointHitScorer 是 keypoint_hit 评分器（确定性；params.min_pass 为通过
// 分数线——缺省全部关键点命中才算对）.
type KeypointHitScorer struct{}

// NewKeypointHitScorer 构造 keypoint_hit 评分器.
func NewKeypointHitScorer() *KeypointHitScorer { return &KeypointHitScorer{} }

// Entry 实现 registry.Scorer.
func (s *KeypointHitScorer) Entry() registry.Entry {
	return registry.Entry{ID: "keypoint_hit", Version: versionKeypointHit}
}

// ScorerContract 实现 registry.Contracted（scorer.yaml §119 required=[keypoints]）.
func (s *KeypointHitScorer) ScorerContract() registry.ScorerSpec {
	return registry.ScorerSpec{
		Entry:         s.Entry(),
		InputSchema:   map[string]registry.ParamKind{"keypoints": registry.KindArray},
		Deterministic: true,
	}
}

// Score 执行关键点命中判定。keypoints 为空是配置错误：显式失败
// （Python 返回置信度 0 结果，Go 按 fail-loud 纪律收紧为错误）.
func (s *KeypointHitScorer) Score(_ context.Context, answer string, params map[string]any) (registry.ScoreResult, error) {
	rawKps, ok := params["keypoints"].([]any)
	if !ok || len(rawKps) == 0 {
		return registry.ScoreResult{}, fmt.Errorf("%w: keypoint_hit keypoints 为空（无法判定≠判错）", ErrInvalidInput)
	}
	norm := parseNormalization(params)
	text := keypointText(decodeAnswer(answer), norm)

	total := 0.0
	allHit := true
	detail := []any{}
	inferences := []any{}
	for i, raw := range rawKps {
		kp, ok := raw.(map[string]any)
		if !ok {
			return registry.ScoreResult{}, fmt.Errorf("%w: keypoint_hit keypoints[%d] 非 object", ErrInvalidInput, i)
		}
		id, _ := kp["id"].(string)
		if id == "" {
			return registry.ScoreResult{}, fmt.Errorf("%w: keypoint_hit keypoints[%d] 缺 id", ErrInvalidInput, i)
		}
		patterns := stringSlice(kp["patterns"])
		matched, err := matchAnyPattern(text, patterns, norm)
		if err != nil {
			return registry.ScoreResult{}, err
		}
		hit := matched != ""
		scoreVal := 0.0
		if hit {
			scoreVal, ok = paramFloat(kp["score"])
			if !ok {
				return registry.ScoreResult{}, fmt.Errorf("%w: keypoint_hit 关键点 %q score 非数值", ErrInvalidInput, id)
			}
		}
		total += scoreVal
		if !hit {
			allHit = false
		}
		detail = append(detail, map[string]any{
			"id":              id,
			"hit":             hit,
			"matched_pattern": matched,
			"score":           scoreVal,
		})
		if !hit {
			if et, _ := kp["error_type_id"].(string); et != "" {
				conf := float64(DefaultKeypointInferConfidence)
				if c, ok := paramFloat(kp["confidence"]); ok {
					conf = c
				}
				inferences = append(inferences, map[string]any{
					"error_type_id": et,
					"confidence":    conf,
					"rule_version":  versionKeypointHit,
					"evidence":      map[string]any{"missed_keypoint": id},
				})
			}
		}
	}

	// correct 口径（Python 同构）：min_pass 缺省 = 全部命中；声明 = 总分达线.
	var correct float64
	if mp, ok := params["min_pass"]; ok && mp != nil {
		minPass, ok := paramFloat(mp)
		if !ok {
			return registry.ScoreResult{}, fmt.Errorf("%w: keypoint_hit min_pass 非数值", ErrInvalidInput)
		}
		if total >= minPass {
			correct = 1.0
		}
	} else if allHit {
		correct = 1.0
	}

	blob, err := json.Marshal(map[string]any{
		"keypoints":        detail,
		"error_inferences": inferences,
	})
	if err != nil {
		return registry.ScoreResult{}, fmt.Errorf("scoring: keypoint_hit 证据序列化失败: %w", err)
	}
	return registry.ScoreResult{
		Correct:      correct >= 1.0,
		Score:        correct, // 判分口径分 ∈ {0,1}（Python dimension_scores["correct"] 同源）
		Confidence:   1.0,     // 确定性评分器；逐维得分明细随 evidence
		EvidenceJSON: string(blob),
	}, nil
}

// keypointText 提取作答文本：short_answer.text / text_blank.blanks 拼接 /
// 裸字符串（Python _extract_text 同构；blanks 键排序拼接——Go map 迭代乱序，
// 可回放要求拼接序确定）.
func keypointText(resp any, n textNormalization) string {
	if s, ok := resp.(string); ok {
		return NormalizeText(s, n)
	}
	m, ok := resp.(map[string]any)
	if !ok {
		return NormalizeText(scalarString(resp), n)
	}
	if t, ok := m["text"]; ok && t != nil {
		return NormalizeText(scalarString(t), n)
	}
	blanks, ok := m["blanks"].(map[string]any)
	if !ok {
		return NormalizeText(scalarString(resp), n)
	}
	keys := make([]string, 0, len(blanks))
	for k := range blanks {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	parts := make([]string, 0, len(keys))
	for _, k := range keys {
		if vm, ok := blanks[k].(map[string]any); ok {
			parts = append(parts, scalarString(vm["value"]))
		} else {
			parts = append(parts, scalarString(blanks[k]))
		}
	}
	return NormalizeText(strings.Join(parts, " "), n)
}

// matchAnyPattern 逐模式命中判定；返回首个命中的 pattern（未命中空串）.
func matchAnyPattern(text string, patterns []string, n textNormalization) (string, error) {
	for _, pat := range patterns {
		if strings.HasPrefix(pat, "re:") {
			re, err := regexp.Compile(pat[3:])
			if err != nil {
				return "", fmt.Errorf("%w: keypoint_hit 正则模式编译失败（方言锁定 Python re 子集；Go RE2 拒绝后向引用/前向断言等实现相关特性）: %v", ErrInvalidInput, err)
			}
			if re.MatchString(text) {
				return pat, nil
			}
		} else if strings.Contains(text, NormalizeText(pat, n)) {
			return pat, nil
		}
	}
	return "", nil
}
