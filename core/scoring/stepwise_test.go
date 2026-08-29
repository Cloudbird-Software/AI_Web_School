package scoring

import (
	"context"
	"errors"
	"strings"
	"testing"

	"github.com/Cloudbird-Software/AI_Web_School/registry"
)

// stepwise_rubric 套件：逐步汇总、缺步推断、置信度取弱环、子评分器契约
// （只能来自注册表且须确定性）、子入参声明面校验.

// detTable 装配确定性四件套（注册顺序由 RegisterDeterministicScorers 保证）.
func detTable(t *testing.T) *registry.ScorerTable {
	t.Helper()
	tb := registry.NewScorerTable()
	if err := RegisterDeterministicScorers(tb); err != nil {
		t.Fatalf("确定性评分器注册失败: %v", err)
	}
	return tb
}

func stepwiseParams(steps ...map[string]any) map[string]any {
	return map[string]any{"steps": toAnySlice(steps)}
}

func stepDef(id, scorer string, maxScore float64, subParams map[string]any) map[string]any {
	return map[string]any{
		"step_id":       id,
		"scorer":        scorer,
		"max_score":     maxScore,
		"scorer_params": subParams,
	}
}

// TestStepwiseRubricScoring 逐步汇总正/负例.
func TestStepwiseRubricScoring(t *testing.T) {
	sw := mustStepwise(t, detTable(t))
	ctx := context.Background()

	params := stepwiseParams(
		stepDef("s1", "exact_match", 4, map[string]any{"answer": map[string]any{"b1": "3"}}),
		stepDef("s2", "math_equivalence", 6, map[string]any{"answer_expr": "1/2"}),
	)
	full := `{"steps":[{"step_id":"s1","response":{"blanks":{"b1":"3"}}},{"step_id":"s2","response":"0.5"}]}`
	partial := `{"steps":[{"step_id":"s1","response":{"blanks":{"b1":"3"}}},{"step_id":"s2","response":"0.3"}]}`

	t.Run("全对满分", func(t *testing.T) {
		res, err := sw.Score(ctx, full, params)
		if err != nil || !res.Correct || res.Score != 1 || res.Confidence != 1 {
			t.Fatalf("res=%+v err=%v", res, err)
		}
	})
	t.Run("部分得分记错（correct 口径 0.4）", func(t *testing.T) {
		res, err := sw.Score(ctx, partial, params)
		if err != nil || res.Correct || res.Score != 0.4 {
			t.Fatalf("res=%+v err=%v", res, err)
		}
		ev := evidenceMap(t, res)
		if ev["max_total"] != 10.0 {
			t.Fatalf("max_total=%v", ev["max_total"])
		}
		steps := ev["steps"].([]any)
		second := steps[1].(map[string]any)
		if second["points"] != 0.0 || second["sub_correct"] != 0.0 {
			t.Fatalf("步骤分应记零: %v", second)
		}
	})
	t.Run("缺步记零并推断 missing_step", func(t *testing.T) {
		res, err := sw.Score(ctx, `{"steps":[{"step_id":"s1","response":{"blanks":{"b1":"3"}}}]}`, params)
		if err != nil || res.Correct || res.Score != 0.4 {
			t.Fatalf("res=%+v err=%v", res, err)
		}
		// 缺步是确定缺失：scoring 置信度不降级.
		if res.Confidence != 1.0 {
			t.Fatalf("缺步置信度不应降级: %v", res.Confidence)
		}
		inf := evidenceMap(t, res)["error_inferences"].([]any)[0].(map[string]any)
		if inf["error_type_id"] != "missing_step" || inf["evidence"].(map[string]any)["step_id"] != "s2" {
			t.Fatalf("missing_step 推断不符: %v", inf)
		}
	})
	t.Run("裸数组作答形态", func(t *testing.T) {
		res, err := sw.Score(ctx,
			`[{"step_id":"s1","response":{"blanks":{"b1":"3"}}},{"step_id":"s2","response":"0.5"}]`,
			params)
		if err != nil || !res.Correct {
			t.Fatalf("res=%+v err=%v", res, err)
		}
	})
	t.Run("max_total 为零判错不除零", func(t *testing.T) {
		res, err := sw.Score(ctx, full, stepwiseParams(
			stepDef("s1", "exact_match", 0, map[string]any{"answer": map[string]any{"b1": "3"}}),
		))
		if err != nil || res.Correct || res.Score != 0 {
			t.Fatalf("res=%+v err=%v", res, err)
		}
	})
	t.Run("嵌套 stepwise 可复用（子评分器来自注册表）", func(t *testing.T) {
		nested := stepwiseParams(
			stepDef("inner", "stepwise_rubric", 10, stepwiseParams(
				stepDef("s1", "exact_match", 5, map[string]any{"answer": map[string]any{"b1": "3"}}),
			)),
		)
		res, err := sw.Score(ctx, `{"steps":[{"step_id":"inner","response":{"steps":[{"step_id":"s1","response":{"blanks":{"b1":"3"}}}]}}]}`, nested)
		if err != nil || !res.Correct || res.Score != 1 {
			t.Fatalf("res=%+v err=%v", res, err)
		}
	})
}

// TestStepwiseRubricContract 子评分器契约三面：未注册拒、AI 评分器拒、
// 子入参声明面校验拒绝.
func TestStepwiseRubricContract(t *testing.T) {
	ctx := context.Background()

	t.Run("未注册子评分器", func(t *testing.T) {
		sw := mustStepwise(t, detTable(t))
		_, err := sw.Score(ctx, `{}`, stepwiseParams(stepDef("s1", "nope", 1, nil)))
		if !errors.Is(err, ErrScorerNotFound) {
			t.Fatalf("err = %v, want ErrScorerNotFound", err)
		}
	})
	t.Run("AI 子评分器被拒（须确定性）", func(t *testing.T) {
		tb := detTable(t)
		if err := tb.Register("ai_rubric", aiStub()); err != nil {
			t.Fatal(err)
		}
		sw := mustStepwise(t, tb)
		_, err := sw.Score(ctx, `{}`, stepwiseParams(stepDef("s1", "ai_rubric", 1, nil)))
		if !errors.Is(err, ErrInvalidInput) || !strings.Contains(err.Error(), "确定性") {
			t.Fatalf("err = %v, want 确定性拒绝", err)
		}
	})
	t.Run("子入参缺必备键（与 Runner 同一声明面）", func(t *testing.T) {
		sw := mustStepwise(t, detTable(t))
		_, err := sw.Score(ctx, `{}`, stepwiseParams(stepDef("s1", "exact_match", 1, map[string]any{})))
		if !errors.Is(err, ErrInvalidInput) || !strings.Contains(err.Error(), "缺必备键") {
			t.Fatalf("err = %v, want 缺必备键", err)
		}
	})
	t.Run("步骤定义残缺 fail-loud", func(t *testing.T) {
		sw := mustStepwise(t, detTable(t))
		cases := []struct {
			name   string
			params map[string]any
		}{
			{"steps 为空", map[string]any{"steps": []any{}}},
			{"缺 step_id", stepwiseParams(map[string]any{"scorer": "exact_match", "max_score": 1.0})},
			{"缺 scorer", stepwiseParams(map[string]any{"step_id": "s1", "max_score": 1.0})},
			{"max_score 非数值", stepwiseParams(map[string]any{"step_id": "s1", "scorer": "exact_match", "max_score": "x"})},
		}
		for _, tc := range cases {
			t.Run(tc.name, func(t *testing.T) {
				if _, err := sw.Score(ctx, `{}`, tc.params); !errors.Is(err, ErrInvalidInput) {
					t.Fatalf("err = %v, want ErrInvalidInput", err)
				}
			})
		}
	})
	t.Run("构造期拒绝 nil 注册表", func(t *testing.T) {
		if _, err := NewStepwiseRubricScorer(nil); !errors.Is(err, ErrInvalidInput) {
			t.Fatalf("err = %v, want ErrInvalidInput", err)
		}
	})
}

// TestStepwiseConfidenceTakesWeakLink 置信度取各步最小值（串联取弱环）：
// 注册一个低置信确定性替身作为步骤级子评分器.
func TestStepwiseConfidenceTakesWeakLink(t *testing.T) {
	tb := detTable(t)
	weak := &stubScorer{
		entry: registry.Entry{ID: "weak_sub", Version: "1.0.0+stub"},
		spec: registry.ScorerSpec{
			Entry:         registry.Entry{ID: "weak_sub", Version: "1.0.0+stub"},
			InputSchema:   map[string]registry.ParamKind{"answer": registry.KindAny},
			Deterministic: true,
		},
		res: registry.ScoreResult{Correct: true, Score: 1, Confidence: 0.5},
	}
	if err := tb.Register("weak_sub", weak); err != nil {
		t.Fatal(err)
	}
	sw := mustStepwise(t, tb)
	res, err := sw.Score(context.Background(), `{"steps":[{"step_id":"s1","response":"x"}]}`,
		stepwiseParams(stepDef("s1", "weak_sub", 2, map[string]any{"answer": "x"})))
	if err != nil || !res.Correct || res.Score != 1 {
		t.Fatalf("res=%+v err=%v", res, err)
	}
	if res.Confidence != 0.5 {
		t.Fatalf("置信度应取弱环 0.5: %v", res.Confidence)
	}
}

// TestStepwiseSubInferenceCarriesStepID 子评分器自报推断随 step_id 归因并入.
func TestStepwiseSubInferenceCarriesStepID(t *testing.T) {
	tb := detTable(t)
	// keypoint 未命中会自报错误推断——经 stepwise 并入后须带 step_id.
	sw := mustStepwise(t, tb)
	res, err := sw.Score(context.Background(), `{"steps":[{"step_id":"s1","response":"答非所问"}]}`,
		stepwiseParams(stepDef("s1", "keypoint_hit", 2,
			kpParams(map[string]any{"id": "kp1", "patterns": []any{"关键词"}, "score": 1.0, "error_type_id": "err.miss"}))))
	if err != nil {
		t.Fatal(err)
	}
	inf := evidenceMap(t, res)["error_inferences"].([]any)[0].(map[string]any)
	if inf["error_type_id"] != "err.miss" {
		t.Fatalf("error_type_id=%v", inf["error_type_id"])
	}
	if inf["evidence"].(map[string]any)["step_id"] != "s1" {
		t.Fatalf("step_id 归因缺失: %v", inf)
	}
}

func mustStepwise(t *testing.T, tb *registry.ScorerTable) *StepwiseRubricScorer {
	t.Helper()
	sw, err := NewStepwiseRubricScorer(tb)
	if err != nil {
		t.Fatalf("NewStepwiseRubricScorer: %v", err)
	}
	return sw
}

// TestStepwiseThroughRunner 注册表面端到端.
func TestStepwiseThroughRunner(t *testing.T) {
	tb := detTable(t)
	r, err := NewRunner(tb)
	if err != nil {
		t.Fatal(err)
	}
	run, err := r.Run(context.Background(), RunInput{
		ScorerID: "stepwise_rubric",
		Answer:   `{"steps":[{"step_id":"s1","response":{"blanks":{"b1":"3"}}}]}`,
		Params: stepwiseParams(
			stepDef("s1", "exact_match", 4, map[string]any{"answer": map[string]any{"b1": "3"}}),
		),
	})
	if err != nil {
		t.Fatal(err)
	}
	if !run.Result.Correct || run.ScorerVersion != versionStepwiseRubric {
		t.Fatalf("run=%+v", run)
	}
	if _, ok := run.Trace["evidence"]; !ok {
		t.Fatalf("证据应随 trace 落账: %v", run.Trace)
	}
}
