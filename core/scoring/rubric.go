package scoring

// 量规解析器（Python 冻结实现 src/core/scoring/rubric_parser.py 的 Go 移植，
// T-W4-019 语义同构）：量规即数据 → 评分 prompt + AI 响应解析。
//
// 对齐契约：
//   - 输入侧 specs/contracts/registries/scorer.yaml 的
//     ai_rubric.params_schema.rubric（{dimensions:[{id,name,anchors,
//     score_bands,error_type_rules}]}）；
//   - 输出侧 T-W4-019 验收①：{dimensions:[{name,score,max,rationale,
//     confidence}], total_score, total_max, overall_confidence}。
//
// Python 侧的 RubricTemplate（Pydantic）入参不移植：Go 面量规一律经 JSON
// 通道以 map 形态进入（Pydantic 实例的序列化产物即本形态）.

import (
	"encoding/json"
	"fmt"
	"regexp"
	"strconv"
	"strings"
)

// 量规纪律常量（Python rubric_parser.py 同值）.
const (
	// HumanReviewConfidenceThreshold AI 响应置信度阈值（< 此值标记待人工复核，
	// T-W4-019 验收②；低置信自动转 human_confirm 队列——架构 v2 §4.5 上线
	// 四步的收口纪律）.
	HumanReviewConfidenceThreshold = 0.6

	// MaxRubricDimensions 量规维度数量上限（防 prompt 膨胀与解析歧义）.
	MaxRubricDimensions = 16
)

// gradeBandLabel 学段中文名（prompt 上下文提示，量规分值不变）.
var gradeBandLabel = map[string]string{
	"L": "低段（小学 1-2 年级）",
	"M": "中段（小学 3-4 年级）",
	"H": "高段（小学 5-6 年级）",
}

// ParsedDimension 是已解析的单维度（中性结构，无学科语义——A5/X6）.
type ParsedDimension struct {
	// ID 维度 id（snake_case，落 dimension_scores 键）.
	ID string
	// Name 维度中文名（prompt 与结果展示用）.
	Name string
	// Anchors 等级行为锚点描述列表（按等级序）.
	Anchors []string
	// ScoreBands 分值带原始条目（[{level,label,score}]，prompt 呈现用）.
	ScoreBands []map[string]any
	// MaxScore 该维度满分（= max(score_bands.score)）.
	MaxScore float64
	// ErrorTypeRules 维度得分→错误类型规则表（透传，可空）.
	ErrorTypeRules []any
}

// ParsedRubric 是已解析的量规（评分器消费的中性结构）.
type ParsedRubric struct {
	// Dimensions 已解析维度列表（按输入顺序）.
	Dimensions []*ParsedDimension
	// TotalMaxScore 分值合计（显式 total_max_score 优先，否则按维度合计）.
	TotalMaxScore float64
}

// ParseRubric 解析量规（map 形态，JSON 通道常态）为评分器消费的中性结构。
// 量规结构非法（缺 dimensions / 维度缺字段 / 等级为空）返回
// ErrInvalidInput wrap 错误.
func ParseRubric(rubric map[string]any) (*ParsedRubric, error) {
	if rubric == nil {
		return nil, fmt.Errorf("%w: 量规缺 dimensions", ErrInvalidInput)
	}
	rawDims, ok := rubric["dimensions"].([]any)
	if !ok || len(rawDims) == 0 {
		return nil, fmt.Errorf("%w: 量规缺 dimensions 或非数组", ErrInvalidInput)
	}
	if len(rawDims) > MaxRubricDimensions {
		return nil, fmt.Errorf("%w: 量规维度数 %d 超过上限 %d（防 prompt 膨胀）", ErrInvalidInput, len(rawDims), MaxRubricDimensions)
	}

	parsed := make([]*ParsedDimension, 0, len(rawDims))
	for i, raw := range rawDims {
		dim, ok := raw.(map[string]any)
		if !ok {
			return nil, fmt.Errorf("%w: 量规维度 #%d 非 object", ErrInvalidInput, i)
		}
		dimID, _ := dim["id"].(string)
		if dimID == "" {
			return nil, fmt.Errorf("%w: 量规维度 #%d 缺 id 或非 string", ErrInvalidInput, i)
		}
		dimName, _ := dim["name"].(string)
		if dimName == "" {
			return nil, fmt.Errorf("%w: 量规维度 #%d（%s）缺 name 或非 string", ErrInvalidInput, i, dimID)
		}
		anchorList, ok := dim["anchors"].([]any)
		if !ok || len(anchorList) == 0 {
			return nil, fmt.Errorf("%w: 量规维度 #%d（%s）anchors 必须为非空数组", ErrInvalidInput, i, dimID)
		}
		bandList, ok := dim["score_bands"].([]any)
		if !ok || len(bandList) == 0 {
			return nil, fmt.Errorf("%w: 量规维度 #%d（%s）score_bands 必须为非空数组", ErrInvalidInput, i, dimID)
		}
		bands := make([]map[string]any, 0, len(bandList))
		maxScore := 0.0
		for _, sb := range bandList {
			band, ok := sb.(map[string]any)
			if !ok {
				return nil, fmt.Errorf("%w: 量规维度 #%d（%s）score_bands 元素非 object", ErrInvalidInput, i, dimID)
			}
			score, ok := paramFloat(band["score"])
			if !ok {
				return nil, fmt.Errorf("%w: 量规维度 #%d（%s）score_bands.score 非数值", ErrInvalidInput, i, dimID)
			}
			if score > maxScore {
				maxScore = score
			}
			bands = append(bands, band)
		}
		anchors := make([]string, 0, len(anchorList))
		for _, a := range anchorList {
			anchors = append(anchors, scalarString(a))
		}
		etRules, _ := dim["error_type_rules"].([]any)
		parsed = append(parsed, &ParsedDimension{
			ID:             dimID,
			Name:           dimName,
			Anchors:        anchors,
			ScoreBands:     bands,
			MaxScore:       maxScore,
			ErrorTypeRules: etRules,
		})
	}

	totalMax := 0.0
	for _, d := range parsed {
		totalMax += d.MaxScore
	}
	if v, ok := rubric["total_max_score"]; ok && v != nil {
		f, ok := paramFloat(v)
		if !ok {
			return nil, fmt.Errorf("%w: 量规 total_max_score 非数值", ErrInvalidInput)
		}
		totalMax = f
	}
	return &ParsedRubric{Dimensions: parsed, TotalMaxScore: totalMax}, nil
}

// RubricID 取量规 id（缺省 ad-hoc-rubric——影子账派生 shadow_id 用）.
func RubricID(rubric map[string]any) string {
	if id, ok := rubric["rubric_id"].(string); ok && id != "" {
		return id
	}
	return "ad-hoc-rubric"
}

// BuildScoringPrompt 构建评分 prompt（强模型按量规打分+逐维理由；Python
// build_scoring_prompt 语义同构）。行文与冻结实现一致，供回放比对.
func BuildScoringPrompt(responseText string, parsed *ParsedRubric, gradeBand string) string {
	bandLabel, ok := gradeBandLabel[gradeBand]
	if !ok {
		bandLabel = gradeBand
	}

	lines := []string{
		"你是小学语文作文/看图写话评分器。请严格按下列量规对学生作答逐维度评分。",
		"【学段】" + bandLabel,
		"",
		"【量规】",
	}
	for _, dim := range parsed.Dimensions {
		lines = append(lines, fmt.Sprintf("- 维度「%s」（id=%s，满分 %s）", dim.Name, dim.ID, formatScore(dim.MaxScore)))
		for j, anchor := range dim.Anchors {
			label, score := "?", "?"
			if j < len(dim.ScoreBands) {
				if l, ok := dim.ScoreBands[j]["label"]; ok && l != nil {
					label = scalarString(l)
				}
				if s, ok := dim.ScoreBands[j]["score"]; ok && s != nil {
					score = scalarString(s)
				}
			}
			lines = append(lines, fmt.Sprintf("    等级%d（%s，%s分）：%s", j+1, label, score, anchor))
		}
	}
	lines = append(lines,
		"",
		"【学生作答】",
		responseText,
		"",
		"【输出要求】",
		"请输出严格 JSON（无注释、无 markdown 围栏），结构如下：",
		"{",
		`  "dimensions": [`,
		`    {"id": "<维度id>", "score": <分数>, "rationale": "<理由>", "confidence": <0-1>}`,
		"  ]",
		"}",
		"约束：score 必须在该维度分值带内；rationale 必须非空且引用具体锚点；",
		"confidence 为对该维度评分的置信度，0.0=完全不确定 / 1.0=完全确定。",
		"只输出 JSON，不要任何其他文字。",
	)
	return strings.Join(lines, "\n")
}

// formatScore 分值最短表示（整数不带小数点——Python f-string 的 5.0 →
// "5.0" 与 Go 的 "5" 差异为 prompt 装饰性差异，量规分值语义不变）.
func formatScore(f float64) string {
	return strconv.FormatFloat(f, 'f', -1, 64)
}

// AIRubricScoreDimension 是 AI 评分结果的单维度（T-W4-019 验收①字段）.
type AIRubricScoreDimension struct {
	Name       string  `json:"name"`
	Score      float64 `json:"score"`
	Max        float64 `json:"max"`
	Rationale  string  `json:"rationale"`
	Confidence float64 `json:"confidence"`
}

// AIRubricScore 是 AI 量规评分结果（T-W4-019 验收①字段）.
type AIRubricScore struct {
	// Dimensions 逐维评分（按量规维度顺序对齐——返回结构稳定）.
	Dimensions []*AIRubricScoreDimension `json:"dimensions"`
	// TotalScore 总分（= Σ dimensions.score）.
	TotalScore float64 `json:"total_score"`
	// TotalMax 总满分（= rubric.total_max_score）.
	TotalMax float64 `json:"total_max"`
	// OverallConfidence 整体置信度（= min(dimensions.confidence)）.
	OverallConfidence float64 `json:"overall_confidence"`
	// NeedsHumanReview 是否需要人工复核（overall < 阈值 或解析失败）.
	NeedsHumanReview bool `json:"needs_human_review"`
}

// json_object_re 容错提取首个 JSON 对象（Python \{.*\} DOTALL 贪婪同款：
// 首个 { 到末个 }）.
var jsonObjectRE = regexp.MustCompile(`(?s)\{.*\}`)

// ParseAIResponse 解析 AI 返回的 JSON 为 AIRubricScore。
//
// 容错策略（Python parse_ai_response 同构）：
//   - 优先整段解码；失败则正则提取首个 {...} 再解码（兼容 markdown 围栏）；
//   - 仍失败 → 零分、overall_confidence=0、needs_human_review=true；
//   - 缺维度 → 该维度 score=0/confidence=0/占位理由；
//   - score 超出分值带 → clamp 到 [min(score_bands), max(score_bands)]；
//   - confidence 超出 [0,1] → clamp；rationale 为空 → 占位理由并降置信度.
//
// 解析失败原因只落分类短语、不落底层错误原文（X3：AI 输出可能夹带作答
// 原文，错误文本是泄漏面）.
func ParseAIResponse(content string, parsed *ParsedRubric) *AIRubricScore {
	data, ok := decodeJSONObject(content)
	if !ok {
		return zeroAIRubricScore(parsed, "AI 响应解析失败")
	}

	rawDims, _ := data["dimensions"].([]any)
	scoreByID := make(map[string]map[string]any, len(rawDims))
	for _, rd := range rawDims {
		m, ok := rd.(map[string]any)
		if !ok {
			continue
		}
		if id, ok := m["id"].(string); ok {
			if _, dup := scoreByID[id]; !dup {
				scoreByID[id] = m
			}
		}
	}

	out := make([]*AIRubricScoreDimension, 0, len(parsed.Dimensions))
	totalScore := 0.0
	minConf := 1.0
	for _, dim := range parsed.Dimensions {
		rd, present := scoreByID[dim.ID]
		if !present {
			// AI 漏评该维度：零分 + 零置信（min_conf 拉低）.
			out = append(out, &AIRubricScoreDimension{
				Name: dim.Name, Score: 0, Max: dim.MaxScore,
				Rationale: "AI 未返回该维度评分", Confidence: 0,
			})
			minConf = 0
			continue
		}

		bandMin, bandMax := bandRange(dim)
		scoreVal, ok := paramFloat(rd["score"])
		if !ok {
			scoreVal = 0
		}
		if scoreVal < bandMin {
			scoreVal = bandMin
		}
		if scoreVal > bandMax {
			scoreVal = bandMax
		}

		rationale := strings.TrimSpace(scalarString(rd["rationale"]))
		if rationale == "" {
			rationale = "AI 未给出理由（rationale 为空）"
		}

		confidence, ok := paramFloat(rd["confidence"])
		if !ok {
			confidence = 0
		}
		confidence = clampUnit(confidence)
		if rationale == "AI 未给出理由（rationale 为空）" {
			// 理由缺失降低置信度（Python 同构 0.3 上限）.
			if confidence > 0.3 {
				confidence = 0.3
			}
		}

		out = append(out, &AIRubricScoreDimension{
			Name: dim.Name, Score: scoreVal, Max: dim.MaxScore,
			Rationale: rationale, Confidence: confidence,
		})
		totalScore += scoreVal
		if confidence < minConf {
			minConf = confidence
		}
	}

	return &AIRubricScore{
		Dimensions:        out,
		TotalScore:        totalScore,
		TotalMax:          parsed.TotalMaxScore,
		OverallConfidence: minConf,
		NeedsHumanReview:  minConf < HumanReviewConfidenceThreshold,
	}
}

// decodeJSONObject 解码 AI 输出为 JSON object：整段优先，失败正则提取首个
// {...}（兼容 markdown 围栏）；顶层非 object 与 Python 同构地不重试提取.
func decodeJSONObject(content string) (map[string]any, bool) {
	var v any
	if err := json.Unmarshal([]byte(content), &v); err == nil {
		if m, ok := v.(map[string]any); ok {
			return m, true
		}
		return nil, false
	}
	m := jsonObjectRE.FindString(content)
	if m == "" {
		return nil, false
	}
	if err := json.Unmarshal([]byte(m), &v); err != nil {
		return nil, false
	}
	if m2, ok := v.(map[string]any); ok {
		return m2, true
	}
	return nil, false
}

// zeroAIRubricScore 解析完全失败：零分 + 低置信 + 人工复核标记（验收②）.
func zeroAIRubricScore(parsed *ParsedRubric, note string) *AIRubricScore {
	dims := make([]*AIRubricScoreDimension, 0, len(parsed.Dimensions))
	for _, d := range parsed.Dimensions {
		dims = append(dims, &AIRubricScoreDimension{
			Name: d.Name, Score: 0, Max: d.MaxScore,
			Rationale: note, Confidence: 0,
		})
	}
	return &AIRubricScore{
		Dimensions:        dims,
		TotalScore:        0,
		TotalMax:          parsed.TotalMaxScore,
		OverallConfidence: 0,
		NeedsHumanReview:  true,
	}
}

// bandRange 维度分值带下界/上界（score_bands.score 的 min/max；解析期已保证
// 非空）.
func bandRange(dim *ParsedDimension) (float64, float64) {
	minV, maxV := 0.0, 0.0
	first := true
	for _, band := range dim.ScoreBands {
		s, ok := paramFloat(band["score"])
		if !ok {
			continue
		}
		if first || s < minV {
			minV = s
		}
		if first || s > maxV {
			maxV = s
		}
		first = false
	}
	return minV, maxV
}

// clampUnit 夹取到 [0,1].
func clampUnit(v float64) float64 {
	if v < 0 {
		return 0
	}
	if v > 1 {
		return 1
	}
	return v
}
