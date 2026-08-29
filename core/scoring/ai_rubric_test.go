package scoring

import (
	"context"
	"errors"
	"strings"
	"testing"

	"github.com/Cloudbird-Software/AI_Web_School/core/ai"
	"github.com/Cloudbird-Software/AI_Web_School/registry"
)

// ai_rubric 套件：Caller 注入（fake）+ D10 模型身份 + 低置信复核 + 构造期
// fail-loud + 注册表端到端（trace 固化 model/model_version/prompt_version）。

// rubricCallerFake 是可编程 Caller 替身（捕获请求供 prompt 断言）.
type rubricCallerFake struct {
	resp ai.OutboundResult
	err  error
	got  ai.OutboundRequest
}

func (f *rubricCallerFake) Call(_ context.Context, req ai.OutboundRequest) (ai.OutboundResult, error) {
	f.got = req
	return f.resp, f.err
}

func aiRubricJSON(content, language string) string {
	return `{"dimensions":[{"id":"content","score":` + content + `,"rationale":"切题有细节","confidence":0.9},` +
		`{"id":"language","score":` + language + `,"rationale":"语句通顺","confidence":0.8}]}`
}

func mustAIRubric(t *testing.T, caller ai.Caller) *AIRubricScorer {
	t.Helper()
	s, err := NewAIRubricScorer(AIRubricConfig{
		Caller: caller, Target: "ai_rubric", Model: "glm-5", ModelVersion: "2026-06",
	})
	if err != nil {
		t.Fatalf("NewAIRubricScorer: %v", err)
	}
	return s
}

// TestAIRubricScorerScore 主链路：prompt 组装 → Caller → 解析 → ScoreResult.
func TestAIRubricScorerScore(t *testing.T) {
	caller := &rubricCallerFake{resp: ai.OutboundResult{Content: aiRubricJSON("4", "3")}}
	s := mustAIRubric(t, caller)

	res, err := s.Score(context.Background(), "春天来了。", map[string]any{"rubric": sampleRubric()})
	if err != nil {
		t.Fatal(err)
	}
	if res.Score != 7 || res.Confidence != 0.8 {
		t.Fatalf("res=%+v", res)
	}
	if !res.Correct {
		t.Fatal("高置信结果不应转人工复核")
	}
	if res.Model != "glm-5" || res.ModelVersion != "2026-06" {
		t.Fatalf("D10 模型身份缺失: %+v", res)
	}
	// prompt 组装面：量规与作答原文都进入出站文本.
	prompt := caller.got.Prompt
	for _, want := range []string{"维度「内容」", "春天来了。", "【学段】中段（小学 3-4 年级）", "只输出 JSON"} {
		if !strings.Contains(prompt, want) {
			t.Fatalf("prompt 缺 %q:\n%s", want, prompt)
		}
	}
	// evidence 随行逐维理由与总分.
	ev := evidenceMap(t, res)
	if ev["total_score"] != 7.0 || ev["total_max"] != 8.0 {
		t.Fatalf("总分证据不符: %v", ev)
	}
	dims := ev["dimensions"].([]any)
	if len(dims) != 2 {
		t.Fatalf("逐维证据缺失: %v", ev)
	}
}

// TestAIRubricScorerGradeBand 学段经可选 params.grade_band 声明（缺省 M）.
func TestAIRubricScorerGradeBand(t *testing.T) {
	caller := &rubricCallerFake{resp: ai.OutboundResult{Content: aiRubricJSON("4", "3")}}
	s := mustAIRubric(t, caller)
	if _, err := s.Score(context.Background(), "作文", map[string]any{
		"rubric": sampleRubric(), "grade_band": "H",
	}); err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(caller.got.Prompt, "【学段】高段（小学 5-6 年级）") {
		t.Fatal("grade_band 未进入 prompt")
	}
}

// TestAIRubricScorerLowConfidence 低置信转人工复核（验收②）：Correct=false、
// 低置信推断随 evidence.
func TestAIRubricScorerLowConfidence(t *testing.T) {
	caller := &rubricCallerFake{resp: ai.OutboundResult{Content: `{"dimensions":[{"id":"content","score":3,"rationale":"r","confidence":0.4},{"id":"language","score":2,"rationale":"r","confidence":0.4}]}`}}
	s := mustAIRubric(t, caller)
	res, err := s.Score(context.Background(), "作文", map[string]any{"rubric": sampleRubric()})
	if err != nil {
		t.Fatal(err)
	}
	if res.Correct || res.Confidence != 0.4 {
		t.Fatalf("res=%+v", res)
	}
	if got := firstInferenceType(t, evidenceMap(t, res)); got != "low_confidence_needs_human_review" {
		t.Fatalf("error_type_id=%v", got)
	}
}

// TestAIRubricScorerFailures 失败面：Caller 错误上抛、tier 越域、rubric 缺失、
// 构造期缺 D10 要素.
func TestAIRubricScorerFailures(t *testing.T) {
	t.Run("Caller 错误上抛", func(t *testing.T) {
		caller := &rubricCallerFake{err: errors.New("fake: 出站失败")}
		s := mustAIRubric(t, caller)
		if _, err := s.Score(context.Background(), "作文", map[string]any{"rubric": sampleRubric()}); err == nil {
			t.Fatal("Caller 错误必须上抛")
		}
	})
	t.Run("model_tier 越域", func(t *testing.T) {
		s := mustAIRubric(t, &rubricCallerFake{})
		if _, err := s.Score(context.Background(), "作文", map[string]any{
			"rubric": sampleRubric(), "model_tier": "L1",
		}); !errors.Is(err, ErrInvalidInput) {
			t.Fatalf("err = %v, want ErrInvalidInput", err)
		}
	})
	t.Run("L3 合法", func(t *testing.T) {
		caller := &rubricCallerFake{resp: ai.OutboundResult{Content: aiRubricJSON("4", "3")}}
		s := mustAIRubric(t, caller)
		if _, err := s.Score(context.Background(), "作文", map[string]any{
			"rubric": sampleRubric(), "model_tier": "L3",
		}); err != nil {
			t.Fatalf("L3 应合法: %v", err)
		}
	})
	t.Run("缺 rubric 显式失败", func(t *testing.T) {
		s := mustAIRubric(t, &rubricCallerFake{})
		if _, err := s.Score(context.Background(), "作文", map[string]any{}); !errors.Is(err, ErrInvalidInput) {
			t.Fatalf("err = %v, want ErrInvalidInput", err)
		}
	})
	t.Run("量规结构非法上抛", func(t *testing.T) {
		s := mustAIRubric(t, &rubricCallerFake{})
		if _, err := s.Score(context.Background(), "作文", map[string]any{"rubric": map[string]any{}}); !errors.Is(err, ErrInvalidInput) {
			t.Fatalf("err = %v, want ErrInvalidInput", err)
		}
	})
	t.Run("构造期缺 Caller/模型身份", func(t *testing.T) {
		if _, err := NewAIRubricScorer(AIRubricConfig{}); !errors.Is(err, ErrInvalidInput) {
			t.Fatalf("err = %v, want ErrInvalidInput", err)
		}
		if _, err := NewAIRubricScorer(AIRubricConfig{Caller: &rubricCallerFake{}}); !errors.Is(err, ErrInvalidInput) {
			t.Fatalf("err = %v, want ErrInvalidInput", err)
		}
	})
}

// TestAIRubricThroughRunner 注册表端到端：AI 评分 trace 固化模型身份与
// prompt 版本（D10），证据随行.
func TestAIRubricThroughRunner(t *testing.T) {
	tb := registry.NewScorerTable()
	if err := RegisterAIRubricScorer(tb, mustAIRubric(t, &rubricCallerFake{resp: ai.OutboundResult{Content: aiRubricJSON("4", "3")}})); err != nil {
		t.Fatal(err)
	}
	r, err := NewRunner(tb)
	if err != nil {
		t.Fatal(err)
	}
	run, err := r.Run(context.Background(), RunInput{
		ScorerID: "ai_rubric",
		Answer:   "春天来了。",
		Params:   map[string]any{"rubric": sampleRubric()},
	})
	if err != nil {
		t.Fatal(err)
	}
	trace := run.Trace
	if trace["model"] != "glm-5" || trace["model_version"] != "2026-06" || trace["prompt_version"] != ai.DefaultPromptVersion {
		t.Fatalf("AI trace 必须固化模型身份与 prompt 版本（D10）: %+v", trace)
	}
	ev, ok := trace["evidence"].(map[string]any)
	if !ok || ev["total_score"] != 7.0 {
		t.Fatalf("证据应随 trace 落账: %v", trace)
	}
}
