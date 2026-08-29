package scoring

import (
	"context"
	"errors"
	"testing"

	"github.com/Cloudbird-Software/AI_Web_School/core/ai"
	"github.com/Cloudbird-Software/AI_Web_School/registry"
)

// 平台装配套件：scorer.yaml 冻结清单的 Go 实现注册面——id/契约面/确定性
// 与注册表逐条对齐，端到端经 Runner 契约校验出分.

// TestPlatformScorerTableContract 注册清单与契约声明面对齐 scorer.yaml.
func TestPlatformScorerTableContract(t *testing.T) {
	tb, err := NewPlatformScorerTable(AIRubricConfig{
		Caller: &rubricCallerFake{}, Target: "ai_rubric", Model: "glm-5", ModelVersion: "2026-06",
	})
	if err != nil {
		t.Fatal(err)
	}
	if tb.Len() != 5 {
		t.Fatalf("应注册 5 个现役评分器: %d", tb.Len())
	}
	cases := []struct {
		id            string
		version       string
		inputSchema   map[string]registry.ParamKind
		deterministic bool
	}{
		{"exact_match", "1.0.0+platform", map[string]registry.ParamKind{"answer": registry.KindAny}, true},
		{"math_equivalence", "1.0.0+subject-math", map[string]registry.ParamKind{"answer_expr": registry.KindString}, true},
		{"keypoint_hit", "1.0.0+platform", map[string]registry.ParamKind{"keypoints": registry.KindArray}, true},
		{"stepwise_rubric", "1.0.0+platform", map[string]registry.ParamKind{"steps": registry.KindArray}, true},
		{"ai_rubric", "1.0.0+ai-rubric", map[string]registry.ParamKind{"rubric": registry.KindObject}, false},
	}
	for _, tc := range cases {
		t.Run(tc.id, func(t *testing.T) {
			_, spec, ok := tb.Get(tc.id)
			if !ok {
				t.Fatal("条目未注册")
			}
			if spec.Entry.Version != tc.version {
				t.Fatalf("version=%q want %q", spec.Entry.Version, tc.version)
			}
			if len(spec.InputSchema) != len(tc.inputSchema) {
				t.Fatalf("InputSchema=%v", spec.InputSchema)
			}
			for k, want := range tc.inputSchema {
				if got := spec.InputSchema[k]; got != want {
					t.Fatalf("键 %q 形态=%q want %q", k, got, want)
				}
			}
			if spec.Deterministic != tc.deterministic {
				t.Fatalf("deterministic=%v", spec.Deterministic)
			}
		})
	}
	// 非现役/废弃条目不注册（结构对齐，非遗漏）.
	for _, absent := range []string{"human_confirm", "asr_oral"} {
		if _, _, ok := tb.Get(absent); ok {
			t.Fatalf("%s 不应注册（ADR-0005/reserved）", absent)
		}
	}
}

// TestPlatformScorerTableEndToEnd 五个现役评分器经 Runner 出分（含 AI 面
// fake Caller；ValidateResult 契约校验全通过）.
func TestPlatformScorerTableEndToEnd(t *testing.T) {
	tb, err := NewPlatformScorerTable(AIRubricConfig{
		Caller:       &rubricCallerFake{resp: ai.OutboundResult{Content: aiRubricJSON("4", "3")}},
		Target:       "ai_rubric",
		Model:        "glm-5",
		ModelVersion: "2026-06",
	})
	if err != nil {
		t.Fatal(err)
	}
	r, err := NewRunner(tb)
	if err != nil {
		t.Fatal(err)
	}
	cases := []RunInput{
		{ScorerID: "exact_match", Answer: `{"selected":"B"}`, Params: map[string]any{"answer": "B"}},
		{ScorerID: "math_equivalence", Answer: "0.5", Params: map[string]any{"answer_expr": "1/2"}},
		{ScorerID: "keypoint_hit", Answer: "包含光合作用",
			Params: kpParams(map[string]any{"id": "kp1", "patterns": []any{"光合作用"}, "score": 2.0})},
		{ScorerID: "stepwise_rubric",
			Answer: `{"steps":[{"step_id":"s1","response":{"blanks":{"b1":"3"}}}]}`,
			Params: stepwiseParams(stepDef("s1", "exact_match", 4, map[string]any{"answer": map[string]any{"b1": "3"}}))},
		{ScorerID: "ai_rubric", Answer: "春天来了。", Params: map[string]any{"rubric": sampleRubric()}},
	}
	for _, in := range cases {
		t.Run(in.ScorerID, func(t *testing.T) {
			run, err := r.Run(context.Background(), in)
			if err != nil {
				t.Fatalf("Run 失败: %v", err)
			}
			if !run.Result.Correct || run.Result.Confidence <= 0 {
				t.Fatalf("res=%+v", run.Result)
			}
			if run.Trace["scorer_id"] != in.ScorerID {
				t.Fatalf("trace 身份不符: %v", run.Trace["scorer_id"])
			}
			if _, ok := run.Trace["evidence"]; !ok {
				t.Fatalf("评分证据应随 trace 落账: %v", run.Trace)
			}
		})
	}
}

// TestRegisterDeterministicScorersOnly 确定性子集装配（AI 面显式补注册）.
func TestRegisterDeterministicScorersOnly(t *testing.T) {
	tb := registry.NewScorerTable()
	if err := RegisterDeterministicScorers(tb); err != nil {
		t.Fatal(err)
	}
	if tb.Len() != 4 {
		t.Fatalf("应注册 4 个确定性评分器: %d", tb.Len())
	}
	if _, _, ok := tb.Get("ai_rubric"); ok {
		t.Fatal("AI 面未经显式装配不得入库")
	}
	// 重复注册整体失败（ErrDuplicate 上抛，无静默残表）.
	if err := RegisterDeterministicScorers(tb); !errors.Is(err, registry.ErrDuplicate) {
		t.Fatalf("err = %v, want ErrDuplicate", err)
	}
}
