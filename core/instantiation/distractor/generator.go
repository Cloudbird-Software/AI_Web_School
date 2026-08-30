// Package distractor 承载干扰项生成器（Python 冻结基准
// src/core/instantiation/distractor/generator.py 的 Go 移植；T-W2-003）。
//
// 设计要点（对齐冻结实现）：
//  1. Generate(rule, slotValues, answerValue, allowCollision) 返回
//     {Options: [...]}; 每条 rule 绑定一个 error_type_id，故 options 内
//     所有 option 共享同一 error_binding（验收 §1）。
//  2. deterministic：用安全表达式求值器（expr.Evaluate）求值 rule 表达式，
//     env=slot_values。表达式返回 list 时展开为多个 option；标量返回单
//     option。expression 缺失或求值抛错时拒绝（不静默吞错）。
//  3. corpus_sample：返回占位 option（Value=nil、label=rule.label 或
//     corpus_ref），等待 B 线语料库装配真实值。corpus_ref 缺失拒绝。
//  4. 碰撞检查（验收 §3）：option 值与正解值相等时——
//     allowCollision=false → 返回 CollisionError；true → 仍加入 options，
//     但 collision 字段置 true（B 线容差）。
//  5. 学科无关：本包只依赖 expr 求值器与 DSL schema，不引用任何学科包。
//
// 宪法 X6：本包不 import 任何学科/学段包。
package distractor

import (
	"fmt"

	"github.com/Cloudbird-Software/AI_Web_School/core/instantiation/dsl"
	"github.com/Cloudbird-Software/AI_Web_School/core/instantiation/expr"
)

// CollisionError 干扰项值与正解值碰撞（验收 §3）。
// 默认不允许（生成器拒绝）；抽样场景 allowCollision=true 时改为标记不抛。
type CollisionError struct {
	Distractor  string
	Answer      string
	ErrorTypeID string
}

func (e *CollisionError) Error() string {
	return fmt.Sprintf("干扰项值 %s 与正解值 %s 碰撞", e.Distractor, e.Answer)
}

// Option 单个干扰项选项。
//
// Value：选项取值（deterministic 表达式求值结果，或 corpus_sample 的 nil 占位）；
// Label：可读标签（rule.label 或求值结果字符串化）；
// ErrorBinding：错误类型 id（来自 rule.error_type_id，选项→错误类型确定映射，
// 架构 v2 §4.5「选某项是证据非因果」）；
// Collision：是否与正解碰撞（抽样容差场景标记用）；
// CorpusRef：corpus_sample 规则的语料库引用（仅该类型有效，便于 B 线回填）。
type Option struct {
	Value        expr.Value // corpus_sample 占位时为 nil
	Label        string
	HasLabel     bool
	ErrorBinding string
	Collision    bool
	CorpusRef    string
	HasCorpusRef bool
}

// Result 干扰项生成结果：Options 至少 1 项（无干扰项产出视为生成失败）。
type Result struct {
	Options []Option
}

// Generate 按规则生成干扰项选项列表。
//
// rule：来自母题 spec.distractor_rules.rules[*] 的单条规则；
// slotValues：槽值字典（注入 deterministic 求值器的 env）；
// answerValue：正解值，用于碰撞检查；nil 表示不做碰撞检查；
// allowCollision：抽样容差。true 时碰撞改为标记 collision=true 而非抛错；
// 默认 false（确定性场景必须严格不碰撞）。
//
// 错误：CollisionError（碰撞且不容差）、规则配置错误（缺 expression 或
// corpus_ref）、expr 求值失败（*expr.UnsafeError / *expr.SyntaxError）。
func Generate(rule dsl.DistractorRule, slotValues map[string]any, answerValue expr.Value, allowCollision bool) (*Result, error) {
	switch rule.RuleType {
	case "deterministic":
		options, err := generateDeterministic(rule, slotValues, answerValue, allowCollision)
		if err != nil {
			return nil, err
		}
		return &Result{Options: options}, nil
	case "corpus_sample":
		options, err := generateCorpusSample(rule, answerValue, allowCollision)
		if err != nil {
			return nil, err
		}
		return &Result{Options: options}, nil
	default:
		return nil, fmt.Errorf("未知 rule_type: %q", rule.RuleType)
	}
}

// generateDeterministic deterministic 规则：用安全求值器求 expression。
func generateDeterministic(rule dsl.DistractorRule, slotValues map[string]any, answerValue expr.Value, allowCollision bool) ([]Option, error) {
	if rule.Expression == nil || *rule.Expression == "" {
		return nil, fmt.Errorf("deterministic 规则缺少 expression（rule.expression 为空）")
	}
	result, err := expr.Evaluate(*rule.Expression, slotValues)
	if err != nil {
		return nil, fmt.Errorf(
			"deterministic 干扰项表达式求值失败 (error_type_id=%q): %w",
			rule.ErrorTypeID, err)
	}
	// 表达式可能返回 list（一次产生多个干扰项）
	var values []expr.Value
	if lst, ok := result.(expr.ListValue); ok {
		values = []expr.Value(lst)
	} else {
		values = []expr.Value{result}
	}
	if len(values) == 0 {
		return nil, fmt.Errorf(
			"deterministic 规则表达式返回空列表 (error_type_id=%q)", rule.ErrorTypeID)
	}
	options := make([]Option, 0, len(values))
	for _, v := range values {
		opt, err := makeOption(v, optionMeta{
			errorBinding:   rule.ErrorTypeID,
			label:          rule.Label,
			answerValue:    answerValue,
			allowCollision: allowCollision,
		})
		if err != nil {
			return nil, err
		}
		options = append(options, opt)
	}
	return options, nil
}

// generateCorpusSample corpus_sample 规则：返回占位（等待 B 线语料装配）。
func generateCorpusSample(rule dsl.DistractorRule, answerValue expr.Value, allowCollision bool) ([]Option, error) {
	if rule.CorpusRef == nil || *rule.CorpusRef == "" {
		return nil, fmt.Errorf(
			"corpus_sample 规则缺少 corpus_ref（rule.corpus_ref 为空，B 线未接入）")
	}
	label := rule.Label
	if label == nil {
		label = rule.CorpusRef
	}
	// 占位 Value=nil：B 线接入后由语料装配回填真实值（T-W2-017+）
	opt, err := makeOption(nil, optionMeta{
		errorBinding:   rule.ErrorTypeID,
		label:          label,
		answerValue:    answerValue,
		allowCollision: allowCollision,
		corpusRef:      rule.CorpusRef,
	})
	if err != nil {
		return nil, err
	}
	return []Option{opt}, nil
}

// optionMeta 选项构造参数。
type optionMeta struct {
	errorBinding   string
	label          *string
	answerValue    expr.Value
	allowCollision bool
	corpusRef      *string
}

// makeOption 构造单个选项并做碰撞检查。
func makeOption(value expr.Value, meta optionMeta) (Option, error) {
	collision := false
	// answerValue 为 nil 表示不做碰撞检查（对齐 Python answer_value is not None）。
	if meta.answerValue != nil && expr.ValuesEqual(value, meta.answerValue) {
		if !meta.allowCollision {
			return Option{}, &CollisionError{
				Distractor:  expr.String(value),
				Answer:      expr.String(meta.answerValue),
				ErrorTypeID: meta.errorBinding,
			}
		}
		collision = true
	}
	opt := Option{
		Value:        value,
		ErrorBinding: meta.errorBinding,
		Collision:    collision,
	}
	if meta.label != nil {
		opt.Label = *meta.label
		opt.HasLabel = true
	}
	if meta.corpusRef != nil {
		opt.CorpusRef = *meta.corpusRef
		opt.HasCorpusRef = true
	}
	return opt, nil
}

// DistinctValues 返回 options 中互不重复的取值描述（供上游审校报表）。
// 碰撞去重的口径：与 answer 相同的值在 allow_collision=false 下已被拒绝，
// 该函数只做生成后的只读审计，不改变 options。
func DistinctValues(options []Option) []string {
	seen := map[string]bool{}
	out := make([]string, 0, len(options))
	for _, o := range options {
		key := expr.String(o.Value)
		if !seen[key] {
			seen[key] = true
			out = append(out, key)
		}
	}
	return out
}
