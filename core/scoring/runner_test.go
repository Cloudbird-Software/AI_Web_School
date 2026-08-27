package scoring

import (
	"context"
	"encoding/json"
	"errors"
	"strings"
	"testing"
	"time"

	"github.com/Cloudbird-Software/AI_Web_School/registry"
)

// 本套件承载 T-W5-016 的执行期语义（注册表侧契约校验见 registry/scorer_test.go）：
// - 铁律 3（D4）：评分器只能从注册表取，未注册 id 结构不可达；
// - 验收 #2：入参按注册表 input_contract 校验，缺字段/类型不符明确失败且
//   评分器不被调用（禁止静默转换）；
// - 验收 #1：scoring_trace 固定记录 scorer_id/version、（AI 类）model/model_version/
//   prompt_version、判定依据键（process.correct）与输入摘要、耗时。

// stubScorer 是可编程的最小评分器替身（记录调用次数；返回值测试注入）.
type stubScorer struct {
	entry registry.Entry
	spec  registry.ScorerSpec
	res   registry.ScoreResult
	err   error
	calls int
}

func (s *stubScorer) Entry() registry.Entry { return s.entry }
func (s *stubScorer) ScorerContract() registry.ScorerSpec {
	return s.spec
}
func (s *stubScorer) Score(_ context.Context, _ string, _ map[string]any) (registry.ScoreResult, error) {
	s.calls++
	return s.res, s.err
}

// newTable 装配一个含确定性与 AI 评分器各一的注册表（fail-loud 注册路径本身
// 在 registry 套件验证，这里只走成功路径）.
func newTable(t *testing.T, stubs ...*stubScorer) *registry.ScorerTable {
	t.Helper()
	tb := registry.NewScorerTable()
	for _, s := range stubs {
		if err := tb.Register(s.entry.ID, s); err != nil {
			t.Fatalf("注册 %s 失败: %v", s.entry.ID, err)
		}
	}
	return tb
}

func exactStub() *stubScorer {
	return &stubScorer{
		entry: registry.Entry{ID: "exact_match", Version: "1.0.0+platform"},
		spec: registry.ScorerSpec{
			Entry:         registry.Entry{ID: "exact_match", Version: "1.0.0+platform"},
			InputSchema:   map[string]registry.ParamKind{"answer": registry.KindObject},
			Deterministic: true,
		},
		res: registry.ScoreResult{Correct: true, Score: 1, Confidence: 1},
	}
}

func aiStub() *stubScorer {
	return &stubScorer{
		entry: registry.Entry{ID: "ai_rubric", Version: "1.0.0+ai-rubric"},
		spec: registry.ScorerSpec{
			Entry:           registry.Entry{ID: "ai_rubric", Version: "1.0.0+ai-rubric"},
			AcceptsAnyInput: true,
			PromptVersion:   "v3",
		},
		res: registry.ScoreResult{Correct: true, Score: 8, Confidence: 0.82, Model: "glm", ModelVersion: "2026-06"},
	}
}

const fixedNow = "2026-08-27T10:00:00Z"

func fixedClock(t *testing.T) func() time.Time {
	t.Helper()
	base, err := time.Parse(time.RFC3339, fixedNow)
	if err != nil {
		t.Fatal(err)
	}
	return func() time.Time { return base }
}

func mustRunner(t *testing.T, tb *registry.ScorerTable, clock func() time.Time) *Runner {
	t.Helper()
	r, err := NewRunner(tb)
	if err != nil {
		t.Fatalf("NewRunner: %v", err)
	}
	r.SetClock(clock)
	return r
}

func mustRun(t *testing.T, r *Runner, in RunInput) *Run {
	t.Helper()
	out, err := r.Run(context.Background(), in)
	if err != nil {
		t.Fatalf("Run 意外失败: %v", err)
	}
	return out
}

// TestNewRunnerRequiresRegistry：无注册表的评分执行器是违宪产物（D4），构造期拒绝.
func TestNewRunnerRequiresRegistry(t *testing.T) {
	if _, err := NewRunner(nil); !errors.Is(err, ErrInvalidInput) {
		t.Fatalf("nil 注册表必须构造失败，得到 %v", err)
	}
}

// TestRunScorerOnlyFromRegistry：未注册 id 与空 id 的失败面（铁律 3：
// 未注册的评分器在 Runner 上结构不可达，不存在临时 substitute 通道）.
func TestRunScorerOnlyFromRegistry(t *testing.T) {
	r := mustRunner(t, newTable(t, exactStub()), fixedClock(t))
	if _, err := r.Run(context.Background(), RunInput{ScorerID: "math_equivalence"}); !errors.Is(err, ErrScorerNotFound) {
		t.Fatalf("未注册评分器必须 ErrScorerNotFound，得到 %v", err)
	}
	if _, err := r.Run(context.Background(), RunInput{ScorerID: ""}); !errors.Is(err, ErrInvalidInput) {
		t.Fatalf("空 scorer_id 必须 ErrInvalidInput，得到 %v", err)
	}
}

// TestRunValidatesParamsAgainstContract 是验收 #2 的主断言：入参按注册表
// input_contract 校验，缺字段/类型不符明确报错且评分器不被调用——静默转换
// （Python 鸭子类型面）在 Go 侧不可达.
func TestRunValidatesParamsAgainstContract(t *testing.T) {
	stub := exactStub()
	r := mustRunner(t, newTable(t, stub), fixedClock(t))

	cases := []struct {
		name    string
		params  map[string]any
		wantSub string
	}{
		{
			name:    "缺必备键",
			params:  map[string]any{},
			wantSub: "缺必备键",
		},
		{
			name:    "形态不符（answer 应为 object）",
			params:  map[string]any{"answer": "42"},
			wantSub: "形态应为 object",
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			_, err := r.Run(context.Background(), RunInput{ScorerID: "exact_match", Answer: "B", Params: tc.params})
			if !errors.Is(err, ErrInvalidInput) || !strings.Contains(err.Error(), tc.wantSub) {
				t.Fatalf("err = %v, want ErrInvalidInput 含 %q", err, tc.wantSub)
			}
		})
	}
	if stub.calls != 0 {
		t.Fatalf("契约违例的入参不得触达评分器: calls=%d", stub.calls)
	}

	// number 形态接受 JSON 解码数字（float64）与进程内字面量（int）.
	stub2 := exactStub()
	stub2.spec.InputSchema["tolerance"] = registry.KindNumber
	r2 := mustRunner(t, newTable(t, stub2), fixedClock(t))
	for _, v := range []any{float64(0.5), 1} {
		in := RunInput{ScorerID: "exact_match", Answer: "B", Params: map[string]any{"answer": map[string]any{}, "tolerance": v}}
		if _, err := r2.Run(context.Background(), in); err != nil {
			t.Fatalf("number 形态应接受 %T: %v", v, err)
		}
	}
	// 「任意作答」类条目跳过键表校验.
	ai := aiStub()
	r3 := mustRunner(t, newTable(t, ai), fixedClock(t))
	mustRun(t, r3, RunInput{ScorerID: "ai_rubric", Answer: "作文原文"})
	if ai.calls != 1 {
		t.Fatalf("任意作答条目应直接执行: calls=%d", ai.calls)
	}
}

// TestTraceRecordsReplayabilityFields 是验收 #1 的主断言：trace 固定记录
// scorer 身份、判定依据键（process.correct，复习排程取数位）、置信度、输入
// 摘要与耗时；AI 类评分额外固化 model/model_version/prompt_version（D10），
// 确定性评分器则不得出现模型键（口径不混淆）.
func TestTraceRecordsReplayabilityFields(t *testing.T) {
	base, err := time.Parse(time.RFC3339, fixedNow)
	if err != nil {
		t.Fatal(err)
	}
	n := 0
	step := 250 * time.Millisecond
	stepping := func() time.Time { n++; return base.Add(time.Duration(n) * step) }

	t.Run("确定性评分器", func(t *testing.T) {
		run := mustRun(t, mustRunner(t, newTable(t, exactStub()), stepping),
			RunInput{ScorerID: "exact_match", Answer: "B", Params: map[string]any{"answer": map[string]any{"q1": "B"}}})
		if run.ScorerID != "exact_match" || run.ScorerVersion != "1.0.0+platform" {
			t.Fatalf("Run 身份不符: %+v", run)
		}
		if run.DurationMS != 250 {
			t.Fatalf("duration 应为步进时钟差 250ms: %v", run.DurationMS)
		}
		trace := run.Trace
		if trace["scorer_id"] != "exact_match" || trace["scorer_version"] != "1.0.0+platform" {
			t.Fatalf("trace 评分器身份不符: %+v", trace)
		}
		dims, ok := trace["dimension_scores"].(map[string]any)
		if !ok || dims["correct"] != 1.0 {
			t.Fatalf("dimension_scores.correct 应为客观题 0|1 口径: %+v", trace["dimension_scores"])
		}
		proc, ok := trace["process"].(map[string]any)
		if !ok || proc["correct"] != true {
			t.Fatalf("process.correct（复习排程判定依据键）缺失: %+v", trace["process"])
		}
		conf, ok := trace["confidence"].(map[string]any)
		if !ok || conf["scoring"] != 1.0 {
			t.Fatalf("confidence.scoring 缺失: %+v", trace["confidence"])
		}
		if d, ok := trace["input_digest"].(string); !ok || len(d) != 16 {
			t.Fatalf("input_digest 应为 16 hex: %#v", trace["input_digest"])
		}
		if trace["duration_ms"] != 250.0 {
			t.Fatalf("trace 耗时应固化: %#v", trace["duration_ms"])
		}
		for _, k := range []string{"model", "model_version", "prompt_version"} {
			if _, exists := trace[k]; exists {
				t.Fatalf("确定性评分器不得出现模型键 %q", k)
			}
		}
	})

	t.Run("AI 评分器", func(t *testing.T) {
		run := mustRun(t, mustRunner(t, newTable(t, aiStub()), stepping), RunInput{ScorerID: "ai_rubric", Answer: "作文"})
		trace := run.Trace
		if trace["model"] != "glm" || trace["model_version"] != "2026-06" || trace["prompt_version"] != "v3" {
			t.Fatalf("AI trace 必须固化模型身份与 prompt 版本（D10）: %+v", trace)
		}
		proc := trace["process"].(map[string]any)
		if proc["correct"] != true {
			t.Fatalf("process.correct 缺失: %+v", trace["process"])
		}
	})
}

// TestInputDigestCoversInputAndIsDeterministic：输入摘要覆盖 scorer+作答+参数，
// 同输入同摘要、异输入异摘要——回放定位键的最低性质.
func TestInputDigestCoversInputAndIsDeterministic(t *testing.T) {
	clock := fixedClock(t)
	r := mustRunner(t, newTable(t, exactStub()), clock)
	in := RunInput{ScorerID: "exact_match", Answer: "B", Params: map[string]any{"answer": map[string]any{"q1": "B"}}}
	d1 := mustRun(t, r, in).Trace["input_digest"]
	d2 := mustRun(t, r, in).Trace["input_digest"]
	if d1 != d2 {
		t.Fatalf("同输入摘要必须相同: %v vs %v", d1, d2)
	}
	in.Params = map[string]any{"answer": map[string]any{"q1": "C"}}
	if mustRun(t, r, in).Trace["input_digest"] == d1 {
		t.Fatal("异输入摘要必须不同")
	}
	// 不可 JSON 化入参在出分前显式失败（禁止静默豁免）.
	bad := RunInput{ScorerID: "exact_match", Answer: "B", Params: map[string]any{"answer": map[string]any{"fn": func() {}}}}
	if _, err := r.Run(context.Background(), bad); !errors.Is(err, ErrInvalidInput) {
		t.Fatalf("不可序列化入参必须 ErrInvalidInput，得到 %v", err)
	}
}

// traceJSON 序列化 trace（确定性断言与落账内容比对共用）.
func traceJSON(t *testing.T, trace map[string]any) string {
	t.Helper()
	blob, err := json.Marshal(trace)
	if err != nil {
		t.Fatalf("trace 序列化失败: %v", err)
	}
	return string(blob)
}
