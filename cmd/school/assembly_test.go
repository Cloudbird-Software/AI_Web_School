// assembly_test.go：生产装配面的纯函数测试（评分桥 scoreAgainstRef——
// 无 DB 面：scoring_ref 解析 × 评分器注册表执行 × 落账形态 trace ×
// error_inferences 提取）。pgxpool 连接面不在此宣称覆盖（真库行为归 CI 集成面）.
package main

import (
	"context"
	"testing"

	"github.com/Cloudbird-Software/AI_Web_School/core/scoring"
)

func testScoringRunner(t *testing.T) *scoring.Runner {
	t.Helper()
	tb, err := newDeterministicScorerTable()
	if err != nil {
		t.Fatalf("装配评分表: %v", err)
	}
	runner, err := scoring.NewRunner(tb)
	if err != nil {
		t.Fatalf("构造评分执行器: %v", err)
	}
	return runner
}

// testErrorTypes 返回预填充规范种子的 error_type 注册中心（测试用）.
func testErrorTypes(t *testing.T) *scoring.ErrorTypeRegistry {
	t.Helper()
	return scoring.DefaultErrorTypeRegistry()
}

// TestScoreAgainstRef_ExactMatch 评分桥主链：scoring_ref → exact_match →
// trace 含契约 §3 的 process.correct 与 dimension_scores（Feedback 投影源）.
func TestScoreAgainstRef_ExactMatch(t *testing.T) {
	runner := testScoringRunner(t)
	et := testErrorTypes(t)
	ref := []byte(`{"scorer_id":"exact_match","scorer_params":{"answer":"A"}}`)

	trace, inferences, err := scoreAgainstRef(context.Background(), ref,
		map[string]any{"selected": "A"}, runner, et)
	if err != nil {
		t.Fatalf("判对作答: %v", err)
	}
	process, _ := trace["process"].(map[string]any)
	if process == nil || process["correct"] != true {
		t.Fatalf("trace.process = %v, want correct=true", trace["process"])
	}
	scores, _ := trace["dimension_scores"].(map[string]any)
	if scores == nil || scores["correct"] != float64(1) {
		t.Fatalf("trace.dimension_scores = %v", trace["dimension_scores"])
	}
	if inferences == nil {
		t.Fatal("inferences 必须为空集而非 nil（契约「可为空数组」形态）")
	}

	_, _, err = scoreAgainstRef(context.Background(), ref,
		map[string]any{"selected": "B"}, runner, et)
	if err != nil {
		t.Fatalf("判错作答不应是桥错误: %v", err)
	}
}

// TestScoreAgainstRef_FailClosed 残缺 ref / 未注册评分器一律显式失败
// （评分失败不落账的桥侧前提）.
func TestScoreAgainstRef_FailClosed(t *testing.T) {
	runner := testScoringRunner(t)
	cases := []struct {
		name string
		ref  string
	}{
		{"ref非JSON", `{invalid`},
		{"ref缺scorer_id", `{"scorer_params":{"answer":"A"}}`},
		{"评分器未注册", `{"scorer_id":"no-such-scorer","scorer_params":{"answer":"A"}}`},
		{"评分参数缺answer", `{"scorer_id":"exact_match","scorer_params":{}}`},
	}
	et := testErrorTypes(t)
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if _, _, err := scoreAgainstRef(context.Background(), []byte(tc.ref),
				map[string]any{"selected": "A"}, runner, et); err == nil {
				t.Fatalf("err = nil, want 显式失败")
			}
		})
	}
}

// TestScoreAgainstRef_ErrorInferencesExtracted 是卡 #185 验收核心：评分桥从
// math_equivalence 评分器的 evidence 面提取 error_inferences 并回填返回数组
// （修复 assembly.go:100 恒空断点）。判错作答（off_by_one）应产出非空推断且
// error_type_id 命中注册中心合法值.
func TestScoreAgainstRef_ErrorInferencesExtracted(t *testing.T) {
	runner := testScoringRunner(t)
	et := testErrorTypes(t)
	ref := []byte(`{"scorer_id":"math_equivalence","scorer_params":{"answer_expr":"72"}}`)

	// 23+49 的正确答案是 72；学生答 73 → off_by_one（差恰为 +1）.
	trace, inferences, err := scoreAgainstRef(context.Background(), ref,
		map[string]any{"value": "73"}, runner, et)
	if err != nil {
		t.Fatalf("评分桥不应报错: %v", err)
	}
	proc, _ := trace["process"].(map[string]any)
	if proc["correct"] == true {
		t.Fatalf("4/5 应判错，得到 correct=%v", proc["correct"])
	}
	if len(inferences) == 0 {
		t.Fatal("卡 #185 验收失败：error_inferences 仍为空（assembly.go:100 断点未修复）")
	}
	inf := inferences[0]
	if id, _ := inf["error_type_id"].(string); id != "off_by_one" {
		t.Fatalf("error_type_id = %q, want off_by_one", id)
	}
	if !et.Valid("off_by_one") {
		t.Fatal("off_by_one 应在注册中心登记")
	}
}

// TestScoreAgainstRef_UnknownErrorTypeDropped 注册中心纪律：评分器产出未登记
// error_type_id 时整条推断丢弃（不伪造归因、不污染 response_error_type）.
func TestScoreAgainstRef_UnknownErrorTypeDropped(t *testing.T) {
	runner := testScoringRunner(t)
	// 空注册中心 → 任何 error_type_id 都不合法 → inferences 归空.
	emptyReg := scoring.NewErrorTypeRegistry()
	ref := []byte(`{"scorer_id":"math_equivalence","scorer_params":{"answer_expr":"3/5"}}`)

	_, inferences, err := scoreAgainstRef(context.Background(), ref,
		map[string]any{"value": "4/5"}, runner, emptyReg)
	if err != nil {
		t.Fatalf("评分桥不应报错: %v", err)
	}
	if len(inferences) != 0 {
		t.Fatalf("未登记 error_type_id 应被丢弃，得到 %d 条: %+v", len(inferences), inferences)
	}
}
