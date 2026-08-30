// blueprint.go 承载命题蓝图库 schema（Python 冻结基准
// src/core/production/blueprint_schema.py 的 Go 移植，T-W4-017 语义同构）。
//
// 落地架构 v2 §4.1 D 线「命题蓝图库」：命题蓝图是 D 线命题的入口结构——
// 声明写作类型/学段/主题池/字数区间/时间限制/量规模板引用，被 d_pipeline.go
// 的流水线（T-W4-021）消费：选蓝图 → 按模板实例化 → 量规嵌入 → 校验门 →
// 签发入库。
//
// 学段参数化（验收③）：低段 50-100 字 / 中段 150-250 字 / 高段 300-400 字，
// 由 GradeBandSpec 承载；蓝图含三学段 specs，实例化时按学生学段取对应区间。
//
// 宪法 A5/X6：本文件不 import 任何学科包/学段包；PackID 是字符串字段，
// 核心域仅通过注册表 id 字符串引用学科，不感知学科语义。
package production

import (
	"errors"
	"fmt"
)

// ErrInvalidBlueprint 是蓝图 schema 非法的哨兵（构造期校验失败面）.
var ErrInvalidBlueprint = errors.New("production: 命题蓝图 schema 非法")

// 写作类型（与 interaction.yaml 的 writing 交互 + 学科包模板对齐；
// Python WritingType Literal）.
const (
	WritingComposition = "composition"
	WritingPicture     = "picture_writing"
)

// GradeBandSpec 是学段参数（字数区间/时间限制/评分宽松度）.
//
//   - GradeBand：学段 L/M/H（量规模板同域常量）。
//   - WordCountMin/Max：字数区间（验收③：低段50-100/中段150-250/高段300-400）。
//   - TimeLimitMinutes：建议作答时长（分钟）。
//   - RubricLeniency：评分宽松度 0-1（低段更宽松；透传给评分器作上下文提示，
//     不改变量规分值——量规是数据，宽松度是提示而非硬规则）。
//
// 约束：word_count_min < word_count_max（空区间无意义）.
type GradeBandSpec struct {
	GradeBand        string
	WordCountMin     int
	WordCountMax     int
	TimeLimitMinutes int
	RubricLeniency   float64
}

// validate 构造期校验（Python model_validator 等价面）：值域 + 字数下限
// 严格小于上限.
func (s GradeBandSpec) validate() error {
	switch s.GradeBand {
	case GradebandL, GradebandM, GradebandH:
	default:
		return fmt.Errorf("%w: 学段 %q 越域；合法域 [L, M, H]", ErrInvalidBlueprint, s.GradeBand)
	}
	if s.WordCountMin < 0 || s.WordCountMax < 0 {
		return fmt.Errorf("%w: 学段 %s word_count 越域（min/max 必须 ≥ 0）", ErrInvalidBlueprint, s.GradeBand)
	}
	if s.TimeLimitMinutes < 1 {
		return fmt.Errorf("%w: 学段 %s time_limit_minutes=%d 越域（必须 ≥ 1）",
			ErrInvalidBlueprint, s.GradeBand, s.TimeLimitMinutes)
	}
	if s.RubricLeniency < 0 || s.RubricLeniency > 1 {
		return fmt.Errorf("%w: 学段 %s rubric_leniency=%v 越域（0-1）", ErrInvalidBlueprint, s.GradeBand, s.RubricLeniency)
	}
	if s.WordCountMin >= s.WordCountMax {
		// 字数下限必须严格小于上限（空区间无意义）.
		return fmt.Errorf("%w: 学段 %q word_count_min=%d 须 < word_count_max=%d",
			ErrInvalidBlueprint, s.GradeBand, s.WordCountMin, s.WordCountMax)
	}
	return nil
}

// Blueprint 是命题蓝图（写作类型/学段/主题池/字数区间/时间限制/量规模板
// 引用）。验收①：写作类型/学段/主题池/字数区间/时间限制/量规模板引用齐全.
type Blueprint struct {
	// BlueprintID 蓝图 id（版本化时新 id）.
	BlueprintID string
	// WritingType 写作类型（composition=作文 / picture_writing=看图写话）.
	WritingType string
	// PackID 学科包 id（如 "subject-chinese"）；核心域仅字符串引用，不 import 包.
	PackID string
	// TemplateVersionID A 线母题模板版本引用（实例化时定位模板）.
	TemplateVersionID string
	// RubricTemplateID 量规模板引用（→ RubricTemplate.RubricID）.
	RubricTemplateID string
	// GradeBandSpecs 三学段参数化（须覆盖 L/M/H 三档）.
	GradeBandSpecs []GradeBandSpec
	// TopicPool 主题池（实例化时按主题注入，≥1）.
	TopicPool []string
	// TimeLimitMinutes 默认时间限制（学段 spec 未指定时回退）.
	TimeLimitMinutes int
	// Version 蓝图版本串.
	Version string
}

// BlueprintInput 是 NewBlueprint 的请求参集（Python Blueprint(**kw) 面孔）.
type BlueprintInput struct {
	BlueprintID       string
	WritingType       string
	PackID            string
	TemplateVersionID string
	RubricTemplateID  string
	GradeBandSpecs    []GradeBandSpec
	TopicPool         []string
	TimeLimitMinutes  int
	Version           string
}

// NewBlueprint 构造并施加构造期校验（Python model_validator 等价面）：
// 标识非空 + 写作类型值域 + 逐学段 spec 合法 + 学段 specs 须覆盖 L/M/H
// 三档且不重复。为什么要求三档齐全：命题蓝图是「可复用题目骨架」，应能在
// 三学段下产出合规实例；缺学段会让该学段学生无法消费。如某学段不适用，
// 应另起蓝图而非留空.
func NewBlueprint(in BlueprintInput) (*Blueprint, error) {
	if in.BlueprintID == "" || in.PackID == "" || in.TemplateVersionID == "" || in.RubricTemplateID == "" {
		return nil, fmt.Errorf("%w: blueprint_id/pack_id/template_version_id/rubric_template_id 均不能为空",
			ErrInvalidBlueprint)
	}
	switch in.WritingType {
	case WritingComposition, WritingPicture:
	default:
		return nil, fmt.Errorf("%w: writing_type %q 越域；合法域 [composition, picture_writing]",
			ErrInvalidBlueprint, in.WritingType)
	}
	if len(in.GradeBandSpecs) == 0 {
		return nil, fmt.Errorf("%w: grade_band_specs 不能为空（min_length=1）", ErrInvalidBlueprint)
	}
	bandSeen := make(map[string]bool, len(in.GradeBandSpecs))
	for _, spec := range in.GradeBandSpecs {
		if err := spec.validate(); err != nil {
			return nil, err
		}
		if bandSeen[spec.GradeBand] {
			return nil, fmt.Errorf("%w: 学段 specs 重复：%v", ErrInvalidBlueprint, bandsOf(in.GradeBandSpecs))
		}
		bandSeen[spec.GradeBand] = true
	}
	if missing := missingBands(bandSeen); len(missing) > 0 {
		return nil, fmt.Errorf("%w: 学段 specs 未覆盖：缺 %v", ErrInvalidBlueprint, missing)
	}
	if len(in.TopicPool) == 0 {
		return nil, fmt.Errorf("%w: topic_pool 不能为空（≥1）", ErrInvalidBlueprint)
	}
	for _, topic := range in.TopicPool {
		if topic == "" {
			return nil, fmt.Errorf("%w: topic_pool 含空主题", ErrInvalidBlueprint)
		}
	}
	if in.TimeLimitMinutes < 1 {
		return nil, fmt.Errorf("%w: time_limit_minutes=%d 越域（必须 ≥ 1）", ErrInvalidBlueprint, in.TimeLimitMinutes)
	}
	if in.Version == "" {
		return nil, fmt.Errorf("%w: version 不能为空", ErrInvalidBlueprint)
	}
	return &Blueprint{
		BlueprintID:       in.BlueprintID,
		WritingType:       in.WritingType,
		PackID:            in.PackID,
		TemplateVersionID: in.TemplateVersionID,
		RubricTemplateID:  in.RubricTemplateID,
		GradeBandSpecs:    copySpecs(in.GradeBandSpecs),
		TopicPool:         append([]string(nil), in.TopicPool...),
		TimeLimitMinutes:  in.TimeLimitMinutes,
		Version:           in.Version,
	}, nil
}

// MakeBlueprint 便捷构造：用默认三学段字数区间（验收③）建蓝图.
//
// 默认字数区间：低段50-100/中段150-250/高段300-400（任务卡验收③约定）。
// 默认宽松度：低段0.8/中段0.6/高段0.5（低段更宽容）。
// 默认时长：低段20/中段30/高段40 分钟。version 空串取 "1"（Python 缺省）.
func MakeBlueprint(
	blueprintID, writingType, packID, templateVersionID, rubricTemplateID string,
	topicPool []string,
	timeLimitMinutes int,
	version string,
) (*Blueprint, error) {
	if version == "" {
		version = "1"
	}
	defaults := []GradeBandSpec{
		{GradeBand: GradebandL, WordCountMin: 50, WordCountMax: 100, TimeLimitMinutes: 20, RubricLeniency: 0.8},
		{GradeBand: GradebandM, WordCountMin: 150, WordCountMax: 250, TimeLimitMinutes: 30, RubricLeniency: 0.6},
		{GradeBand: GradebandH, WordCountMin: 300, WordCountMax: 400, TimeLimitMinutes: 40, RubricLeniency: 0.5},
	}
	return NewBlueprint(BlueprintInput{
		BlueprintID:       blueprintID,
		WritingType:       writingType,
		PackID:            packID,
		TemplateVersionID: templateVersionID,
		RubricTemplateID:  rubricTemplateID,
		GradeBandSpecs:    defaults,
		TopicPool:         topicPool,
		TimeLimitMinutes:  timeLimitMinutes,
		Version:           version,
	})
}

// SpecFor 从蓝图取指定学段的 spec（须存在；Python _select_grade_band_spec）.
func (b *Blueprint) SpecFor(gradeBand string) (GradeBandSpec, error) {
	for _, spec := range b.GradeBandSpecs {
		if spec.GradeBand == gradeBand {
			return spec, nil
		}
	}
	return GradeBandSpec{}, fmt.Errorf("%w: 蓝图 %q 缺学段 %q 的 spec",
		ErrInvalidBlueprint, b.BlueprintID, gradeBand)
}

func bandsOf(specs []GradeBandSpec) []string {
	out := make([]string, 0, len(specs))
	for _, s := range specs {
		out = append(out, s.GradeBand)
	}
	return out
}

func missingBands(seen map[string]bool) []string {
	var missing []string
	for _, band := range []string{GradebandL, GradebandM, GradebandH} {
		if !seen[band] {
			missing = append(missing, band)
		}
	}
	return missing
}

func copySpecs(src []GradeBandSpec) []GradeBandSpec {
	out := make([]GradeBandSpec, len(src))
	copy(out, src)
	return out
}
