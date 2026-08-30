// assembly_test.go：生产装配面的纯函数测试（评分桥 scoreAgainstRef——
// 无 DB 面：scoring_ref 解析 × 评分器注册表执行 × 落账形态 trace）。
// pgxpool 连接面不在此宣称覆盖（真库行为归 CI 集成面，本机如实声明）.
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

// TestScoreAgainstRef_ExactMatch 评分桥主链：scoring_ref → exact_match →
// trace 含契约 §3 的 process.correct 与 dimension_scores（Feedback 投影源）.
func TestScoreAgainstRef_ExactMatch(t *testing.T) {
	runner := testScoringRunner(t)
	ref := []byte(`{"scorer_id":"exact_match","scorer_params":{"answer":"A"}}`)

	trace, inferences, err := scoreAgainstRef(context.Background(), ref,
		map[string]any{"selected": "A"}, runner)
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
		map[string]any{"selected": "B"}, runner)
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
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if _, _, err := scoreAgainstRef(context.Background(), []byte(tc.ref),
				map[string]any{"selected": "A"}, runner); err == nil {
				t.Fatalf("err = nil, want 显式失败")
			}
		})
	}
}
