package scoring

import (
	"testing"
)

// TestDefaultErrorTypeRegistrySeeds 规范种子集装配：无重复、无空串、条目数
// 与源码清单一致（装配期 fail-loud 的前提）.
func TestDefaultErrorTypeRegistrySeeds(t *testing.T) {
	r := DefaultErrorTypeRegistry()
	if r.Len() == 0 {
		t.Fatal("默认注册中心不应为空")
	}
	if r.Len() != len(defaultErrorTypeIDs) {
		t.Fatalf("默认注册中心条目数 = %d, want %d", r.Len(), len(defaultErrorTypeIDs))
	}
	// 种子集全覆盖断言（任一 id 漏登记即失败）.
	for _, id := range []string{
		"off_by_one", "empty_response", "missing_step",
		"lang.chr.confusable", "eng.vocab.misspell",
		"low_confidence_needs_human_review",
	} {
		if !r.Valid(id) {
			t.Fatalf("种子集缺少 %q", id)
		}
	}
}

// TestErrorTypeRegistryRejectsInvalid 注册中心纪律：空串与重复登记均拒绝.
func TestErrorTypeRegistryRejectsInvalid(t *testing.T) {
	r := NewErrorTypeRegistry()
	if err := r.Register(""); err == nil {
		t.Fatal("空串登记应失败")
	}
	if err := r.Register("  "); err == nil {
		t.Fatal("空白串登记应失败")
	}
	if err := r.Register("err.test.sample"); err != nil {
		t.Fatalf("首次登记应成功: %v", err)
	}
	if err := r.Register("err.test.sample"); err == nil {
		t.Fatal("重复登记应失败（只增不改）")
	}
}

// TestExtractErrorInferences_OffByOne 核心提取路径：math_equivalence 判错产
// 出的 error_inferences 经注册中心校验后保留 off_by_one.
func TestExtractErrorInferences_OffByOne(t *testing.T) {
	r := DefaultErrorTypeRegistry()
	trace := map[string]any{
		"evidence": map[string]any{
			"match": false,
			"error_inferences": []any{
				map[string]any{
					"error_type_id": "off_by_one",
					"confidence":    1.0,
					"rule_version":  "1.0.0+subject-math",
				},
			},
		},
	}
	got := ExtractErrorInferences(trace, r)
	if len(got) != 1 {
		t.Fatalf("提取数 = %d, want 1, got %+v", len(got), got)
	}
	if got[0]["error_type_id"] != "off_by_one" {
		t.Fatalf("error_type_id = %q, want off_by_one", got[0]["error_type_id"])
	}
	if got[0]["confidence"] != 1.0 {
		t.Fatalf("confidence 应保留: %+v", got[0])
	}
	if got[0]["rule_version"] != "1.0.0+subject-math" {
		t.Fatalf("rule_version 应保留: %+v", got[0])
	}
}

// TestExtractErrorInferences_DropsUnknown 未登记 error_type_id 整条丢弃.
func TestExtractErrorInferences_DropsUnknown(t *testing.T) {
	r := DefaultErrorTypeRegistry()
	trace := map[string]any{
		"evidence": map[string]any{
			"error_inferences": []any{
				map[string]any{"error_type_id": "err.unknown.not-registered", "confidence": 1.0},
			},
		},
	}
	if got := ExtractErrorInferences(trace, r); got != nil {
		t.Fatalf("未登记 id 应丢弃（返回 nil），得到 %+v", got)
	}
}

// TestExtractErrorInferences_FiltersMixed 混合批次：合法保留、非法丢弃，
// 保留序次不乱.
func TestExtractErrorInferences_FiltersMixed(t *testing.T) {
	r := DefaultErrorTypeRegistry()
	trace := map[string]any{
		"evidence": map[string]any{
			"error_inferences": []any{
				map[string]any{"error_type_id": "err.calc.addsub.mismatch", "confidence": 0.9},
				map[string]any{"error_type_id": "totally-made-up", "confidence": 1.0},
				map[string]any{"error_type_id": "empty_response", "confidence": 1.0},
				map[string]any{"confidence": 0.5}, // 缺 error_type_id
				"not-a-map",                       // 形态不符
			},
		},
	}
	got := ExtractErrorInferences(trace, r)
	if len(got) != 2 {
		t.Fatalf("应保留 2 条合法，得到 %d 条: %+v", len(got), got)
	}
	if got[0]["error_type_id"] != "err.calc.addsub.mismatch" || got[1]["error_type_id"] != "empty_response" {
		t.Fatalf("保留序次/内容不符: %+v", got)
	}
}

// TestExtractErrorInferences_NilInputs 防御面：nil trace / nil registry /
// 无 evidence / 无 error_inferences 一律返回 nil 不 panic.
func TestExtractErrorInferences_NilInputs(t *testing.T) {
	r := DefaultErrorTypeRegistry()
	if got := ExtractErrorInferences(nil, r); got != nil {
		t.Fatalf("nil trace 应返回 nil, got %+v", got)
	}
	if got := ExtractErrorInferences(map[string]any{}, nil); got != nil {
		t.Fatalf("nil registry 应返回 nil, got %+v", got)
	}
	if got := ExtractErrorInferences(map[string]any{"evidence": map[string]any{}}, r); got != nil {
		t.Fatalf("空 evidence 应返回 nil, got %+v", got)
	}
	// evidence.error_inferences 类型为非数组时静默返回 nil.
	if got := ExtractErrorInferences(map[string]any{"evidence": map[string]any{"error_inferences": "not-a-slice"}}, r); got != nil {
		t.Fatalf("非数组 error_inferences 应返回 nil, got %+v", got)
	}
}

// TestExtractErrorInferences_CorrectAnswerNoInference 判对作答：evidence 无
// error_inferences 键 → 返回 nil（不伪造归因）.
func TestExtractErrorInferences_CorrectAnswerNoInference(t *testing.T) {
	r := DefaultErrorTypeRegistry()
	trace := map[string]any{
		"evidence": map[string]any{"match": true, "method": "exact"},
	}
	if got := ExtractErrorInferences(trace, r); got != nil {
		t.Fatalf("判对作答不应产出推断, got %+v", got)
	}
}
