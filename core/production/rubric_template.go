// rubric_template.go 承载量规模板数据化（Python 冻结基准
// src/core/production/rubric_template.py 的 Go 移植，T-W4-017 语义同构）。
//
// 落地架构 v2 §4.5「AI 维度量规评分器」的数据侧契约：**量规即数据**。
// 量规模板是可序列化为 JSON 的纯数据结构，被 AI 量规评分器（T-W4-019，
// core/scoring.ParseRubric）直接解析执行——评分器不感知量规语义，只按
// 量规维度/锚点/分值带让强模型打分。
//
// ToScorerParams 输出对齐 specs/contracts/registries/scorer.yaml 的
// ai_rubric.params_schema.rubric：
//
//	dimensions[*] = {id, name, anchors, score_bands, error_type_rules}
//
// 本文件只定义数据模型与构造期校验，不涉及 DB 读写（持久化由迁移 0018 +
// 调用方经写入服务承载），也不感知任何学科语义（宪法 A5/X6：核心域零学科
// 特判）。
package production

import (
	"errors"
	"fmt"
	"math"
	"sort"
)

// 学段覆盖标记：L=低段 / M=中段 / H=高段（Python GradeBand；与 GradeBandPack
// 三档对齐。本地常量化，不为两个字符串建立跨域编译耦合）.
const (
	GradebandL = "L"
	GradebandM = "M"
	GradebandH = "H"
)

// ErrInvalidRubric 是量规模板 schema 非法的哨兵（构造期校验失败面）.
var ErrInvalidRubric = errors.New("production: 量规模板 schema 非法")

// RubricLevel 是量规单等级（如 优秀/良好/合格/待改进）.
//
//   - Level：等级序号，1=最高档（约定，便于排序与一致率计算时取分）。
//   - Label：等级名（教研展示用）。
//   - Description：该等级的行为锚点描述（**非空**——AI 评分器据此判定该维
//     作答落在哪一档；空描述会让强模型无锚点可比，验收②「等级描述非空」）。
//   - Score：该等级对应分值（满分=最高档 score；低档可 <= 满分，通常递减）。
type RubricLevel struct {
	Level       int
	Label       string
	Description string
	Score       float64
}

func (l RubricLevel) validate() error {
	if l.Level < 1 {
		return fmt.Errorf("%w: level=%d 越域（等级序号，1=最高档）", ErrInvalidRubric, l.Level)
	}
	if l.Label == "" {
		return fmt.Errorf("%w: label 不能为空（min_length=1）", ErrInvalidRubric)
	}
	if l.Description == "" {
		return fmt.Errorf("%w: 等级描述不能为空（min_length=1；空描述让 AI 评分器无锚点可比）",
			ErrInvalidRubric)
	}
	return nil
}

// RubricDimension 是量规单维度（如 内容/结构/语言/书写）.
//
//   - ID：维度 id（snake_case，评分器按 id 落 dimension_scores 键）。
//   - Name：维度中文名（教研展示 + AI prompt 中呈现）。
//   - MaxScore：该维度满分（= max(levels.score)；验收③「分值合计正确」）。
//   - Levels：等级列表（≥2 档，否则无区分度）。
//   - ErrorTypeRules：维度得分模式 → 错误类型规则表（对齐 scorer.yaml），
//     评分器据此产 error_inferences（可空）。
type RubricDimension struct {
	ID             string
	Name           string
	MaxScore       float64
	Levels         []RubricLevel
	ErrorTypeRules []any
}

// validate 构造期校验（Python model_validator 等价面）：
//   - id/name 非空、max_score ≥ 0、levels ≥ 2；
//   - 等级 level 唯一（建议升序，不强制排序）；
//   - max_score 必须等于最高档 score（分值带一致性，验收③）。
//
// 为什么校验 max == max(levels.score) 而非 sum：单维度满分是该维度能拿的
// 最高分，即最高档的 score；levels 是「同一维度的不同档位」，不是「多个
// 子项」。sum 会把各档分值相加，语义错误.
func (d RubricDimension) validate() error {
	if d.ID == "" || d.Name == "" {
		return fmt.Errorf("%w: 维度 id/name 不能为空", ErrInvalidRubric)
	}
	if d.MaxScore < 0 || math.IsNaN(d.MaxScore) {
		return fmt.Errorf("%w: 维度 %s max_score=%v 越域（必须 ≥ 0）", ErrInvalidRubric, d.ID, d.MaxScore)
	}
	if len(d.Levels) < 2 {
		return fmt.Errorf("%w: 维度 %s levels=%d 越域（≥2 档，否则无区分度）", ErrInvalidRubric, d.ID, len(d.Levels))
	}
	levelSeen := make(map[int]bool, len(d.Levels))
	top := math.Inf(-1)
	for _, lvl := range d.Levels {
		if err := lvl.validate(); err != nil {
			return fmt.Errorf("维度 %s: %w", d.ID, err)
		}
		if levelSeen[lvl.Level] {
			return fmt.Errorf("%w: 维度 %q 等级 level 重复：%d", ErrInvalidRubric, d.ID, lvl.Level)
		}
		levelSeen[lvl.Level] = true
		if lvl.Score > top {
			top = lvl.Score
		}
	}
	if math.Abs(top-d.MaxScore) > 1e-9 {
		return fmt.Errorf("%w: 维度 %q max_score=%v 不等于最高档 score=%v（分值带不一致）",
			ErrInvalidRubric, d.ID, d.MaxScore, top)
	}
	return nil
}

// sortedLevels 返回按 level 升序的等级列表（ToScorerParams 锚点/分值带序）.
func (d RubricDimension) sortedLevels() []RubricLevel {
	out := make([]RubricLevel, len(d.Levels))
	copy(out, d.Levels)
	sort.SliceStable(out, func(i, j int) bool { return out[i].Level < out[j].Level })
	return out
}

// RubricTemplate 是量规模板（可序列化为 JSON 被评分器直接解析执行）.
//
//   - RubricID：量规 id（内容寻址，sha256 of payload；版本化时新 id）。
//   - GradeBand：学段覆盖标记（L/M/H）；同学科可有同学段不同主题的多套量规。
//   - Dimensions：维度列表（≥1）。
//   - TotalMaxScore：分值合计（= sum(dimensions.max_score)；验收③）。
//   - Version：量规版本串（随题版本化，重判时据此写平行账）。
type RubricTemplate struct {
	RubricID      string
	Name          string
	GradeBand     string
	Dimensions    []RubricDimension
	TotalMaxScore float64
	Version       string
}

// RubricTemplateInput 是 NewRubricTemplate 的请求参集.
type RubricTemplateInput struct {
	RubricID      string
	Name          string
	GradeBand     string
	Dimensions    []RubricDimension
	TotalMaxScore float64
	Version       string
}

// NewRubricTemplate 构造并施加构造期校验（Python model_validator 等价面）：
//   - 分值合计校验：total_max_score == sum(dimensions.max_score)（验收③）；
//   - 维度 id 唯一（评分器按 id 落 dimension_scores 键，重复会覆盖）；
//   - 学段覆盖标记限于 L/M/H。
func NewRubricTemplate(in RubricTemplateInput) (*RubricTemplate, error) {
	if in.RubricID == "" || in.Name == "" || in.Version == "" {
		return nil, fmt.Errorf("%w: rubric_id/name/version 不能为空", ErrInvalidRubric)
	}
	switch in.GradeBand {
	case GradebandL, GradebandM, GradebandH:
	default:
		return nil, fmt.Errorf("%w: grade_band %q 越域；合法域 [L, M, H]", ErrInvalidRubric, in.GradeBand)
	}
	if len(in.Dimensions) == 0 {
		return nil, fmt.Errorf("%w: dimensions 不能为空（≥1）", ErrInvalidRubric)
	}
	idsSeen := make(map[string]bool, len(in.Dimensions))
	actualTotal := 0.0
	for _, dim := range in.Dimensions {
		if err := dim.validate(); err != nil {
			return nil, err
		}
		if idsSeen[dim.ID] {
			return nil, fmt.Errorf("%w: 维度 id 重复：%s", ErrInvalidRubric, dim.ID)
		}
		idsSeen[dim.ID] = true
		actualTotal += dim.MaxScore
	}
	if math.Abs(actualTotal-in.TotalMaxScore) > 1e-9 {
		return nil, fmt.Errorf("%w: total_max_score=%v 不等于维度满分合计 %v（分值合计不一致）",
			ErrInvalidRubric, in.TotalMaxScore, actualTotal)
	}
	dims := make([]RubricDimension, len(in.Dimensions))
	copy(dims, in.Dimensions)
	return &RubricTemplate{
		RubricID:      in.RubricID,
		Name:          in.Name,
		GradeBand:     in.GradeBand,
		Dimensions:    dims,
		TotalMaxScore: in.TotalMaxScore,
		Version:       in.Version,
	}, nil
}

// ToScorerParams 序列化为 scorer.yaml ai_rubric.params_schema.rubric 结构
// （量规即数据：把内部强类型 levels[] 映射为评分器契约要求的
// {dimensions:[{id,name,anchors,score_bands,error_type_rules}],total_max_score}，
// 使评分器无需感知量规内部结构即可消费，验收③）。
//
// 映射约定：
//   - anchors ← levels[].description（各档行为锚点描述，按 level 升序）；
//   - score_bands ← levels[]（保留 level/label/score，供评分器落档）；
//   - error_type_rules 原样透传.
func (t *RubricTemplate) ToScorerParams() map[string]any {
	dimensions := make([]any, 0, len(t.Dimensions))
	for _, dim := range t.Dimensions {
		levels := dim.sortedLevels()
		anchors := make([]any, 0, len(levels))
		bands := make([]any, 0, len(levels))
		for _, lvl := range levels {
			anchors = append(anchors, lvl.Description)
			bands = append(bands, map[string]any{
				"level": lvl.Level,
				"label": lvl.Label,
				"score": lvl.Score,
			})
		}
		rules := dim.ErrorTypeRules
		if rules == nil {
			rules = []any{}
		}
		dimensions = append(dimensions, map[string]any{
			"id":               dim.ID,
			"name":             dim.Name,
			"anchors":          anchors,
			"score_bands":      bands,
			"error_type_rules": rules,
		})
	}
	return map[string]any{
		"dimensions":      dimensions,
		"total_max_score": t.TotalMaxScore,
	}
}
