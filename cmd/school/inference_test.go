package main

// inference_test.go 钉死错误推断生产面的行为（弱项报告/复习队列的数据源头）。
// 覆盖三种 binding 方言：Go packs 选项位次锚定 / Python 冻结引擎选项值
// 锚定 / answer 级规则，以及判对不推断、缺 selected 不推断的负向路径。
import (
	"encoding/json"
	"testing"
)

func infer(t *testing.T, interactionID string, bindingsJSON, contentJSON string, response map[string]any, wrong bool) []map[string]any {
	t.Helper()
	ref := map[string]any{"interaction_id": interactionID}
	refJSON, _ := json.Marshal(ref)
	return inferErrorBindings(refJSON, []byte(contentJSON), []byte(bindingsJSON), response, "sha256:test-iv", wrong)
}

func TestInferOptionSubjectDialect(t *testing.T) {
	// Go packs 方言：subject 锚定选项位次（真实 subjectmath 落库形态）
	bindings := `[
		{"subject":"option:A","error_type_id":"err.cmp.dec.reverse-order","confidence_rule":"selected-option-equals-subject"},
		{"subject":"option:C","error_type_id":"err.cmp.dec.digit-slip","confidence_rule":"selected-option-equals-subject"}
	]`
	content := `{"blocks":[
		{"kind":"text","template":"A. {A}","rendered":"A. 0.5"},
		{"kind":"text","template":"B. {B}","rendered":"B. 0.6"},
		{"kind":"text","template":"C. {C}","rendered":"C. 5"}
	]}`
	inf := infer(t, "single_choice", bindings, content, map[string]any{"selected": "5"}, true)
	if len(inf) != 1 {
		t.Fatalf("选中 C 位干扰项应产 1 条推断，得到 %d: %v", len(inf), inf)
	}
	if inf[0]["error_type_id"] != "err.cmp.dec.digit-slip" {
		t.Fatalf("error_type_id = %v, want err.cmp.dec.digit-slip", inf[0]["error_type_id"])
	}
	if inf[0]["confidence"] != 0.9 {
		t.Fatalf("confidence = %v, want 0.9", inf[0]["confidence"])
	}
	if inf[0]["rule_version"] != "sha256:test-iv" {
		t.Fatalf("rule_version 应为 item_version_id: %v", inf[0]["rule_version"])
	}
}

func TestInferOptionValueDialect(t *testing.T) {
	// 冻结引擎方言：option_value 锚定
	bindings := `[{"option_value":"0.5","label":"位数多的小数更大","error_type_id":"err.dec.more-digits"}]`
	inf := infer(t, "single_choice", bindings, `{}`, map[string]any{"selected": "0.5"}, true)
	if len(inf) != 1 || inf[0]["error_type_id"] != "err.dec.more-digits" {
		t.Fatalf("选项值锚定推断失败: %v", inf)
	}
}

func TestInferAnswerLevelRule(t *testing.T) {
	// answer 级规则（unit_convert 真实形态）：显式判错才推断
	bindings := `[{"subject":"blank:b1","error_type_id":"err.conv.unit.mismatch","confidence_rule":"answer-value-neq-implies-error"}]`
	inf := infer(t, "numeric_blank", bindings, `{}`, map[string]any{"answer": "999"}, true)
	if len(inf) != 1 || inf[0]["error_type_id"] != "err.conv.unit.mismatch" {
		t.Fatalf("answer 级判错推断失败: %v", inf)
	}
	// 判对不推断
	inf = infer(t, "numeric_blank", bindings, `{}`, map[string]any{"answer": "9.12"}, false)
	if len(inf) != 0 {
		t.Fatalf("判对不应推断: %v", inf)
	}
	// 未显式判定（wrong=false）不推断——不猜
	inf = infer(t, "numeric_blank", bindings, `{}`, map[string]any{"answer": "999"}, false)
	if len(inf) != 0 {
		t.Fatalf("无显式判定不应推断: %v", inf)
	}
}

func TestInferNoInferenceOnCorrectOption(t *testing.T) {
	// 选中正确项（无绑定命中）→ 空推断（非 nil）
	bindings := `[{"subject":"option:A","error_type_id":"err.x","confidence_rule":"selected-option-equals-subject"}]`
	content := `{"blocks":[{"kind":"text","rendered":"A. 9"},{"kind":"text","rendered":"B. 8"}]}`
	inf := infer(t, "single_choice", bindings, content, map[string]any{"selected": "8"}, false)
	if len(inf) != 0 {
		t.Fatalf("无命中应空推断: %v", inf)
	}
}

func TestInferMultiChoiceSelectedList(t *testing.T) {
	bindings := `[
		{"subject":"option:A","error_type_id":"err.a","confidence_rule":"selected-option-equals-subject"},
		{"subject":"option:B","error_type_id":"err.b","confidence_rule":"selected-option-equals-subject"}
	]`
	content := `{"blocks":[{"kind":"text","rendered":"A. 1"},{"kind":"text","rendered":"B. 2"}]}`
	inf := infer(t, "multi_choice", bindings, content, map[string]any{"selected": []any{"1", "2"}}, true)
	if len(inf) != 2 {
		t.Fatalf("多选两条命中应产 2 条推断，得到 %d: %v", len(inf), inf)
	}
}

func TestInferGarbageBindingsSkipped(t *testing.T) {
	// 脏 binding（缺 error_type_id / 非法 JSON）跳过不炸
	inf := infer(t, "single_choice", `[{"subject":"option:A"}]`, `{}`, map[string]any{"selected": "x"}, true)
	if len(inf) != 0 {
		t.Fatalf("缺 error_type_id 应跳过: %v", inf)
	}
	refJSON := []byte(`{"interaction_id":"single_choice"}`)
	got := inferErrorBindings(refJSON, []byte(`{}`), []byte(`not-json`), map[string]any{"selected": "x"}, "sha256:test-iv", true)
	if len(got) != 0 {
		t.Fatalf("非法 bindings JSON 应返回空: %v", got)
	}
}
