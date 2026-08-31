// inference.go 承载作答错误推断的生产面（评分证据加工）：
// item_version.error_bindings × 作答载荷 → error_inferences（契约 §4 结构）。
//
// 这是弱项报告/复习队列/诊断归因的数据源头（北极星「知道孩子哪里弱」）。
// 镜像冻结 Python 的 infer_option_errors（src/core/scoring/service.py:112，
// 架构 §4.5：模板 distractor_rules 确定映射）并兼容 Go 学科包 binding 方言：
//
//	dialect A（Go packs，subject 锚定）：{subject: "option:A", error_type_id,
//	  confidence_rule: "selected-option-equals-subject"}——选项位次锚定，
//	  选中该位干扰项即推断该错误类型；
//	dialect B（Python 冻结引擎）：{option_value, label, error_type_id}——
//	  选项值锚定，选中值等于 option_value 即推断；
//	answer 级规则（confidence_rule: "answer-value-neq-implies-error"）：
//	  填空/数值题整题级绑定——显式判错即推断（如「位数多的小数更大」）。
//
// rule_version = item_version_id（映射规则随内容寻址版本化——同内容必同
// 映射，重放可复现，R-D-05）。推断只在显式判错时发生：判对永不推断。
package main

import (
	"encoding/json"
	"fmt"
	"strings"
)

// defaultOptionInferConfidence 对齐冻结实现 DEFAULT_OPTION_INFER_CONFIDENCE
// （src/core/scoring/service.py:37）——确定性干扰项映射的高置信但不封顶，
// 给贝叶斯聚合留收敛空间.
const defaultOptionInferConfidence = 0.9

// choiceInteractions 是选项类交互集合（推断的选项映射只对它们有意义）.
var choiceInteractions = map[string]bool{
	"single_choice": true,
	"multi_choice":  true,
}

// inferErrorBindings 从 error_bindings 产出错误推断（评分证据加工的纯函数面）。
//
// 入参均为 item_version 行的 JSONB 原文；correctExplicit 为评分轨迹的显式
// 对错（轨迹不含显式判定时调用方应传 false 且不产推断——不猜）。
// 返回契约 §4 结构数组；无命中返回空切片（非 nil——契约 required 数组）.
func inferErrorBindings(interactionRef, content, errorBindings []byte, response map[string]any, itemVersionID string, wrongExplicit bool) []map[string]any {
	out := []map[string]any{}
	if len(errorBindings) == 0 {
		return out
	}
	var ref struct {
		InteractionID string `json:"interaction_id"`
	}
	if err := json.Unmarshal(interactionRef, &ref); err != nil || ref.InteractionID == "" {
		return out
	}
	var bindings []map[string]any
	if err := json.Unmarshal(errorBindings, &bindings); err != nil {
		return out
	}
	selected := selectedValues(response)

	// 选项位次→选项值（dialect A 的映射源）：题干块 "A. 168" 解析
	optionValues := map[string]string{}
	if choiceInteractions[ref.InteractionID] {
		optionValues = parseOptionValues(content)
	}

	seen := map[string]bool{}
	for _, b := range bindings {
		errorTypeID, _ := b["error_type_id"].(string)
		if errorTypeID == "" {
			continue
		}
		rule, _ := b["confidence_rule"].(string)
		subject, _ := b["subject"].(string)

		hit := false
		var evidence map[string]any
		switch {
		case strings.HasPrefix(subject, "option:"):
			// dialect A：选项位次锚定——选中该位次即命中
			letter := strings.TrimPrefix(subject, "option:")
			val, ok := optionValues[letter]
			if !ok {
				continue
			}
			for _, s := range selected {
				if s == val {
					hit = true
					evidence = map[string]any{"selected_option": letter, "selected_value": s}
					break
				}
			}
		case rule == "answer-value-neq-implies-error":
			// answer 级规则：显式判错即命中（整题级绑定）
			if !wrongExplicit {
				continue
			}
			hit = true
			evidence = map[string]any{"rule": rule}
		default:
			// dialect B：选项值锚定（冻结引擎形态）
			ov, _ := b["option_value"].(string)
			if ov == "" {
				continue
			}
			for _, s := range selected {
				if ov != "" && s == ov {
					hit = true
					label, _ := b["label"].(string)
					evidence = map[string]any{"selected_option": s, "label": label}
					break
				}
			}
		}
		if !hit {
			continue
		}
		key := subject + "|" + errorTypeID + "|" + fmt.Sprint(evidence)
		if seen[key] {
			continue
		}
		seen[key] = true
		out = append(out, map[string]any{
			"error_type_id": errorTypeID,
			"confidence":    defaultOptionInferConfidence,
			"rule_version":  itemVersionID,
			"evidence":      evidence,
		})
	}
	return out
}

// selectedValues 抽取作答载荷的选中值面（response.selected：字符串或数组）.
func selectedValues(response map[string]any) []string {
	raw, ok := response["selected"]
	if !ok || raw == nil {
		return nil
	}
	switch v := raw.(type) {
	case string:
		if v == "" {
			return nil
		}
		return []string{v}
	case []any:
		out := make([]string, 0, len(v))
		for _, x := range v {
			if s, ok := x.(string); ok && s != "" {
				out = append(out, s)
			}
		}
		return out
	default:
		return nil
	}
}

// parseOptionValues 从 content.blocks 解析选项位次→选项值（"A. 168" 形态的
// kind 方言文本块；解析失败的块跳过——推断缺失不炸评分）.
func parseOptionValues(content []byte) map[string]string {
	out := map[string]string{}
	if len(content) == 0 {
		return out
	}
	var c struct {
		Blocks []map[string]any `json:"blocks"`
	}
	if err := json.Unmarshal(content, &c); err != nil {
		return out
	}
	for _, b := range c.Blocks {
		text := ""
		for _, key := range []string{"rendered", "value", "template"} {
			if s, ok := b[key].(string); ok && s != "" {
				text = s
				break
			}
		}
		if text == "" {
			continue
		}
		// "A. 168" / "A．168"：单字母 + 分隔 + 值
		if len(text) >= 3 && text[1] == '.' && text[0] >= 'A' && text[0] <= 'Z' {
			out[string(text[0])] = strings.TrimSpace(text[2:])
		}
	}
	return out
}
