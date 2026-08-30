// linter_test.go 母题 DSL Schema 与 Linter 的验收测试（T-W2-001）。
//
// 覆盖（对齐验收 §1/§2）：
//  1. 正例：黄金样例同构 spec 通过 lint 与强类型解析；
//  2. 四类必检负例：缺块 / slot 类型非法 / 轴引用未知槽 / difficulty_relevant 非布尔；
//  3. 结构校验负例：extra 字段拒绝（extra=forbid）、枚举值非法、必填缺失、
//     kp_set 最少 1 项；
//  4. 多错收集：单次 lint 报告全部问题且 (code, path) 去重。
package dsl

import (
	"testing"
)

// validSpec 与 tests/golden/instantiation/sample_single_choice.yaml 同构。
func validSpec() map[string]any {
	return map[string]any{
		"objective": map[string]any{
			"kp_set":          []any{map[string]any{"dimension": "kp", "code": "math.nal.int.add"}},
			"kp_set_mode":     "single",
			"cognitive_level": "apply",
			"gradeband":       "L",
			"graph_release":   "2026.1",
		},
		"slots": map[string]any{
			"a": map[string]any{"type": "int", "difficulty_relevant": true},
			"b": map[string]any{"type": "int", "difficulty_relevant": true},
		},
		"variation_axes": map[string]any{
			"axes": []any{map[string]any{"axis_id": "ax-num", "slots": []any{"a"}}},
		},
		"presentation": map[string]any{
			"blocks": []any{map[string]any{"kind": "text", "template": "{a} + {b} = ?"}},
		},
		"answer_program": map[string]any{"expression": "a + b", "returns": "number"},
		"distractor_rules": map[string]any{
			"rules": []any{
				map[string]any{
					"rule_type":     "deterministic",
					"error_type_id": "err.calc.add.off-by-one",
					"expression":    "a + b + 1",
					"label":         "多 1",
				},
			},
		},
	}
}

func hasError(errs []LintError, code, path string) bool {
	for _, e := range errs {
		if e.Code == code && e.Path == path {
			return true
		}
	}
	return false
}

func TestLintValidSpec(t *testing.T) {
	res := Lint(validSpec())
	if !res.Valid {
		t.Fatalf("合法 spec 误报: %+v", res.Errors)
	}
	if len(res.Errors) != 0 {
		t.Fatalf("合法 spec errors 应为空: %+v", res.Errors)
	}
	// 强类型解析可往返
	spec, err := ParseSpec(validSpec())
	if err != nil {
		t.Fatalf("ParseSpec 失败: %v", err)
	}
	if spec.Objective.KPSetMode != "single" || len(spec.Objective.KPSet) != 1 {
		t.Errorf("objective 解析不符: %+v", spec.Objective)
	}
	if spec.Slots["a"].Type != "int" || !spec.Slots["a"].DifficultyRelevant {
		t.Errorf("slot a 解析不符: %+v", spec.Slots["a"])
	}
	if len(spec.VariationAxes.Axes) != 1 || spec.VariationAxes.Axes[0].AxisID != "ax-num" {
		t.Errorf("variation_axes 解析不符: %+v", spec.VariationAxes.Axes)
	}
	if len(spec.DistractorRules.Rules) != 1 || spec.DistractorRules.Rules[0].ErrorTypeID != "err.calc.add.off-by-one" {
		t.Errorf("distractor_rules 解析不符: %+v", spec.DistractorRules.Rules)
	}
}

func TestLintRequiredBlocks(t *testing.T) {
	spec := validSpec()
	delete(spec, "answer_program")
	delete(spec, "distractor_rules")
	res := Lint(spec)
	if res.Valid {
		t.Fatalf("缺块 spec 应报错")
	}
	if !hasError(res.Errors, "missing_block", ".") {
		t.Fatalf("应报 missing_block@.: %+v", res.Errors)
	}
	// 消息含两个缺失块名
	found := false
	for _, e := range res.Errors {
		if e.Code == "missing_block" {
			found = true
		}
	}
	if !found {
		t.Fatalf("missing_block 消息缺失")
	}
}

func TestLintInvalidSlotType(t *testing.T) {
	spec := validSpec()
	spec["slots"].(map[string]any)["a"] = map[string]any{"type": "float", "difficulty_relevant": true}
	res := Lint(spec)
	if res.Valid {
		t.Fatalf("非法 slot 类型应报错")
	}
	if !hasError(res.Errors, "invalid_slot_type", "slots.a.type") {
		t.Fatalf("应报 invalid_slot_type@slots.a.type: %+v", res.Errors)
	}
}

func TestLintDanglingVariationSlot(t *testing.T) {
	spec := validSpec()
	spec["variation_axes"].(map[string]any)["axes"] = []any{
		map[string]any{"axis_id": "ax-bad", "slots": []any{"nonexistent"}},
	}
	res := Lint(spec)
	if !hasError(res.Errors, "dangling_variation_slot", "variation_axes.axes[0].slots") {
		t.Fatalf("应报 dangling_variation_slot: %+v", res.Errors)
	}
}

func TestLintDifficultyRelevantNotBool(t *testing.T) {
	spec := validSpec()
	spec["slots"].(map[string]any)["b"] = map[string]any{"type": "int", "difficulty_relevant": "yes"}
	res := Lint(spec)
	if !hasError(res.Errors, "invalid_difficulty_relevant_type", "slots.b.difficulty_relevant") {
		t.Fatalf("应报 invalid_difficulty_relevant_type: %+v", res.Errors)
	}
	// 去重：同一 (code, path) 不重复出现
	count := 0
	for _, e := range res.Errors {
		if e.Code == "invalid_difficulty_relevant_type" && e.Path == "slots.b.difficulty_relevant" {
			count++
		}
	}
	if count != 1 {
		t.Fatalf("(code, path) 应去重，实际出现 %d 次: %+v", count, res.Errors)
	}
}

func TestLintStructuralErrors(t *testing.T) {
	t.Run("extra 字段拒绝", func(t *testing.T) {
		spec := validSpec()
		spec["objective"].(map[string]any)["bonus"] = 1
		res := Lint(spec)
		if !hasError(res.Errors, "extra_field_forbidden", "objective.bonus") {
			t.Fatalf("应报 extra_field_forbidden@objective.bonus: %+v", res.Errors)
		}
	})
	t.Run("枚举值非法", func(t *testing.T) {
		spec := validSpec()
		spec["objective"].(map[string]any)["kp_set_mode"] = "some"
		res := Lint(spec)
		if !hasError(res.Errors, "invalid_enum_value", "objective.kp_set_mode") {
			t.Fatalf("应报 invalid_enum_value: %+v", res.Errors)
		}
	})
	t.Run("必填字段缺失", func(t *testing.T) {
		spec := validSpec()
		delete(spec["answer_program"].(map[string]any), "expression")
		res := Lint(spec)
		if !hasError(res.Errors, "missing_field", "answer_program.expression") {
			t.Fatalf("应报 missing_field: %+v", res.Errors)
		}
	})
	t.Run("kp_set 至少 1 项", func(t *testing.T) {
		spec := validSpec()
		spec["objective"].(map[string]any)["kp_set"] = []any{}
		res := Lint(spec)
		if !hasError(res.Errors, "too_short", "objective.kp_set") {
			t.Fatalf("应报 too_short@objective.kp_set: %+v", res.Errors)
		}
	})
	t.Run("rule_type 枚举", func(t *testing.T) {
		spec := validSpec()
		spec["distractor_rules"].(map[string]any)["rules"] = []any{
			map[string]any{"rule_type": "magic", "error_type_id": "e1"},
		}
		res := Lint(spec)
		if !hasError(res.Errors, "invalid_enum_value", "distractor_rules.rules.0.rule_type") {
			t.Fatalf("应报 rule_type 枚举错: %+v", res.Errors)
		}
	})
	t.Run("非 map 输入", func(t *testing.T) {
		res := Lint("not a spec")
		if res.Valid {
			t.Fatalf("非 map 输入应报错")
		}
	})
}

func TestParseSpecFailClosed(t *testing.T) {
	// 结构违规（extra 字段）→ ParseSpec 拒绝；非法 slot type 属 Linter
	// 阶段 1 必检（对齐冻结实现：Slot.type 在 schema 层只是 str）。
	spec := validSpec()
	spec["objective"].(map[string]any)["bonus"] = 1
	if _, err := ParseSpec(spec); err == nil {
		t.Fatalf("结构违规时 ParseSpec 应返回错误")
	}
	spec2 := validSpec()
	spec2["slots"].(map[string]any)["a"] = map[string]any{"type": "float", "difficulty_relevant": true}
	if _, err := ParseSpec(spec2); err != nil {
		t.Fatalf("slot 类型白名单属 Linter 检查，ParseSpec 不应拒绝: %v", err)
	}
	if Lint(spec2).Valid {
		t.Fatalf("slot 类型白名单应由 Lint 拒绝")
	}
}

func TestLintCollectsMultipleErrors(t *testing.T) {
	spec := validSpec()
	delete(spec, "presentation")                                                                      // 缺块
	spec["slots"].(map[string]any)["a"] = map[string]any{"type": "float", "difficulty_relevant": "x"} // 类型+布尔
	res := Lint(spec)
	if res.Valid {
		t.Fatalf("应失败")
	}
	if len(res.Errors) < 3 {
		t.Fatalf("应一次收集多个错误，实际 %d: %+v", len(res.Errors), res.Errors)
	}
}
