package scoring

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"sync"
	"testing"

	"github.com/Cloudbird-Software/AI_Web_School/core/ai"
)

// 影子模式套件（T-W4-020 语义同构）：
// - ShadowScore 账语义：派生 id 确定、作答摘要、append-only、不碰真实分数
//   （本包无 response_event 写路径——账独立性由结构保证）；
// - 一致性判定（逐维偏差 ≤ 容差；人工缺维度 → 不一致）；
// - 基准报告一致率与门槛；
// - MemoryShadowStore 并发安全（-race）。

func shaHex(t *testing.T, s string) string {
	t.Helper()
	sum := sha256.Sum256([]byte(s))
	return hex.EncodeToString(sum[:])
}

func shadowRunner(t *testing.T, store ShadowStore) *ShadowRunner {
	t.Helper()
	caller := &rubricCallerFake{resp: aiContentResult}
	return mustShadowRunner(t, mustAIRubric(t, caller), store)
}

// aiContentResult 是基准验证固定 AI 响应（content=5 满分、language=3 满分、
// 高置信——一致性差异由人工结论侧构造）.
var aiContentResult = ai.OutboundResult{Content: `{"dimensions":[{"id":"content","score":5,"rationale":"切题","confidence":0.95},{"id":"language","score":3,"rationale":"通顺","confidence":0.95}]}`}

func mustShadowRunner(t *testing.T, scorer *AIRubricScorer, store ShadowStore) *ShadowRunner {
	t.Helper()
	r, err := NewShadowRunner(scorer, store)
	if err != nil {
		t.Fatalf("NewShadowRunner: %v", err)
	}
	return r
}

// TestShadowScoreRecordShape 影子账记录的派生面：shadow_id/response_text_digest
// 的组成与确定性（用 stdlib 独立重算验证派生契约）.
func TestShadowScoreRecordShape(t *testing.T) {
	store := NewMemoryShadowStore()
	r := shadowRunner(t, store)

	text := "春天来了，小草绿了。"
	rec, err := r.Score(context.Background(), ShadowRequest{
		ResponseText: text,
		Rubric:       sampleRubric(),
	})
	if err != nil {
		t.Fatal(err)
	}
	// 作答摘要地面真值：stdlib sha256 独立重算.
	wantDigest := "sha256:" + shaHex(t, text)
	if rec.ResponseTextDigest != wantDigest {
		t.Fatalf("response_text_digest=%q want %q", rec.ResponseTextDigest, wantDigest)
	}
	// shadow_id 地面真值：dataset|case|rubric|摘要 的 sha256.
	wantID := "sha256:" + shaHex(t, "ad-hoc|ad-hoc|rub-test-1|"+wantDigest)
	if rec.ShadowID != wantID {
		t.Fatalf("shadow_id=%q want %q", rec.ShadowID, wantID)
	}
	if rec.ConsistencyStatus != StatusPending {
		t.Fatalf("无人结论文应 pending: %s", rec.ConsistencyStatus)
	}
	if rec.RubricID != "rub-test-1" || rec.WritingType != "composition" {
		t.Fatalf("缺省面不符: %+v", rec)
	}
	// 同输入同影子 id（重放定位），异输入异 id.
	if _, err := r.Score(context.Background(), ShadowRequest{ResponseText: "另一篇作答", Rubric: sampleRubric()}); err != nil {
		t.Fatal(err)
	}
	rec3, err := r.Score(context.Background(), ShadowRequest{ResponseText: text, Rubric: sampleRubric()})
	if !errors.Is(err, ErrDuplicateShadowID) {
		t.Fatalf("同 shadow_id 重放应触发 append-only 拒绝: %v", err)
	}
	_ = rec3
}

// TestShadowScoreAppendOnly 同 shadow_id 二次写账必须被拒（append-only；
// 重判应换新 shadow_id，历史影子评分不被覆盖）.
func TestShadowScoreAppendOnly(t *testing.T) {
	store := NewMemoryShadowStore()
	r := shadowRunner(t, store)
	req := ShadowRequest{ResponseText: "同一次作答", Rubric: sampleRubric()}
	if _, err := r.Score(context.Background(), req); err != nil {
		t.Fatal(err)
	}
	_, err := r.Score(context.Background(), req)
	if !errors.Is(err, ErrDuplicateShadowID) {
		t.Fatalf("err = %v, want ErrDuplicateShadowID", err)
	}
	if store.Len() != 1 {
		t.Fatalf("append-only 账不应增长: %d", store.Len())
	}
}

// TestShadowConsistencyStatus 一致性三态（pending/consistent/inconsistent）.
func TestShadowConsistencyStatus(t *testing.T) {
	r := shadowRunner(t, nil)
	human := func(content float64, language float64) map[string]any {
		return map[string]any{"dimensions": []any{
			map[string]any{"id": "content", "score": content},
			map[string]any{"id": "language", "score": language},
		}}
	}
	cases := []struct {
		name   string
		human  map[string]any
		status string
	}{
		{"逐维零偏差一致", human(5, 3), StatusConsistent},
		{"偏差恰等于容差一致（≤）", human(4, 3), StatusConsistent},
		{"偏差越容差不一致", human(1, 3), StatusInconsistent},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			rec, err := r.Score(context.Background(), ShadowRequest{
				ResponseText: tc.name, Rubric: sampleRubric(), HumanScore: tc.human,
			})
			if err != nil {
				t.Fatal(err)
			}
			if rec.ConsistencyStatus != tc.status {
				t.Fatalf("status=%s want %s", rec.ConsistencyStatus, tc.status)
			}
		})
	}
	t.Run("人工结论缺维度不一致", func(t *testing.T) {
		rec, err := r.Score(context.Background(), ShadowRequest{
			ResponseText: "缺维度",
			Rubric:       sampleRubric(),
			HumanScore:   map[string]any{"dimensions": []any{map[string]any{"id": "content", "score": 5.0}}},
		})
		if err != nil {
			t.Fatal(err)
		}
		if rec.ConsistencyStatus != StatusInconsistent {
			t.Fatalf("status=%s", rec.ConsistencyStatus)
		}
	})
}

// TestShadowBenchmarkReport 基准报告：一致率、门槛判定、纯报告不落影子账.
func TestShadowBenchmarkReport(t *testing.T) {
	store := NewMemoryShadowStore()
	r := shadowRunner(t, store)

	dataset := map[string]any{
		"dataset_id": "shadow-bench-test",
		"rubric":     sampleRubric(),
		"cases": []any{
			benchCase("case-ok", 5, 3),
			benchCase("case-bad", 1, 3),
			benchCase("case-boundary", 4, 3),
		},
	}
	report, err := r.Benchmark(context.Background(), dataset, BenchmarkOptions{})
	if err != nil {
		t.Fatal(err)
	}
	if report.TotalCases != 3 || report.ConsistentCases != 2 {
		t.Fatalf("report=%+v", report)
	}
	if report.ConsistencyRate != 2.0/3.0 || report.Passed {
		t.Fatalf("一致率 %v 应未过 0.70 门槛", report.ConsistencyRate)
	}
	if report.Threshold != DefaultConsistencyRateThreshold {
		t.Fatalf("threshold=%v", report.Threshold)
	}
	// 基准验证是纯报告（Python benchmark_against_dataset 纯函数同构）.
	if store.Len() != 0 {
		t.Fatalf("基准报告不得落影子账: %d", store.Len())
	}
	// 放宽门槛后通过.
	low, err := r.Benchmark(context.Background(), dataset, BenchmarkOptions{ConsistencyRateThreshold: 0.5})
	if err != nil || !low.Passed {
		t.Fatalf("放宽门槛应通过: %+v %v", low, err)
	}
	// 报告含逐维对比详情.
	first := report.PerCase[0]
	if len(first.Dimensions) != 2 || first.Dimensions[0].DimensionID != "content" {
		t.Fatalf("per_case 详情缺失: %+v", first)
	}
}

func benchCase(id string, content, language float64) map[string]any {
	return map[string]any{
		"case_id":       id,
		"grade_band":    "M",
		"writing_type":  "composition",
		"response_text": "作答-" + id,
		"human_score": map[string]any{"dimensions": []any{
			map[string]any{"id": "content", "score": content},
			map[string]any{"id": "language", "score": language},
		}},
	}
}

// TestShadowBenchmarkValidation 数据集结构非法 fail-loud.
func TestShadowBenchmarkValidation(t *testing.T) {
	r := shadowRunner(t, nil)
	cases := []map[string]any{
		{},
		{"rubric": sampleRubric()},
		{"rubric": sampleRubric(), "cases": []any{}},
	}
	for i, ds := range cases {
		if _, err := r.Benchmark(context.Background(), ds, BenchmarkOptions{}); !errors.Is(err, ErrInvalidInput) {
			t.Fatalf("case %d: err = %v, want ErrInvalidInput", i, err)
		}
	}
}

// TestShadowRunnerFailures 构造与落账失败面.
func TestShadowRunnerFailures(t *testing.T) {
	if _, err := NewShadowRunner(nil, nil); !errors.Is(err, ErrInvalidInput) {
		t.Fatalf("err = %v, want ErrInvalidInput", err)
	}
	// AI 评分失败 → 影子运行失败，不产生半截账.
	r := mustShadowRunner(t, mustAIRubric(t, &rubricCallerFake{err: errors.New("fake: 出站失败")}), NewMemoryShadowStore())
	if _, err := r.Score(context.Background(), ShadowRequest{ResponseText: "x", Rubric: sampleRubric()}); err == nil {
		t.Fatal("AI 失败必须上抛")
	}
}

// TestMemoryShadowStoreConcurrent 并发写账 -race 安全且无丢失.
func TestMemoryShadowStoreConcurrent(t *testing.T) {
	store := NewMemoryShadowStore()
	const n = 16
	var wg sync.WaitGroup
	errs := make(chan error, n)
	for i := 0; i < n; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			rec := ShadowScoreRecord{ShadowID: fmt.Sprintf("id-%d", i)}
			if err := store.Record(context.Background(), rec); err != nil {
				errs <- err
			}
		}(i)
	}
	wg.Wait()
	close(errs)
	for err := range errs {
		t.Fatalf("并发写账失败: %v", err)
	}
	if store.Len() != n {
		t.Fatalf("账应恰 %d 行: %d", n, store.Len())
	}
	snap := store.Snapshot()
	if len(snap) != n {
		t.Fatalf("快照应含全部记录: %d", len(snap))
	}
}
