package datastat

import (
	"errors"
	"testing"
)

// ────────────────────────────────────────────────────────────────────
// Median（冻结实现 health._median → statistics.median）
// ────────────────────────────────────────────────────────────────────

func TestMedian(t *testing.T) {
	assertNilF(t, "空列表", Median(nil))
	assertValueF(t, "单元素", Median([]float64{7}), 7.0, 1e-15)
	assertValueF(t, "奇数个", Median([]float64{5, 1, 3}), 3.0, 1e-15)
	assertValueF(t, "偶数个取中点", Median([]float64{4, 1, 3, 2}), 2.5, 1e-15)
	assertValueF(t, "乱序", Median([]float64{100, 1, 50}), 50.0, 1e-15)
}

// ────────────────────────────────────────────────────────────────────
// DetectAnomalies（冻结实现 health._detect_anomalies）
// ────────────────────────────────────────────────────────────────────

func TestDetectAnomalies_InsufficientSample(t *testing.T) {
	// n < 30 不判定（样本不足不伪造异常——外层标 insufficient_sample）
	anomalies := DetectAnomalies(29, 1.0, wantFloat(-1.0), wantFloat(100), map[string]float64{"A": 0.5, "B": 0.0}, "A")
	if len(anomalies) != 0 {
		t.Errorf("样本不足应无异常标签，得到 %v", anomalies)
	}
}

func TestDetectAnomalies_RateBoundaries(t *testing.T) {
	cases := []struct {
		rate float64
		want string // "" = 无标签
	}{
		{0.96, "correct_rate_too_high"},
		{0.95, ""}, // 严格大于：恰在阈值不算
		{0.94, ""}, // 正常区间
		{0.06, ""}, // 正常区间
		{0.05, ""}, // 严格小于：恰在阈值不算
		{0.04, "correct_rate_too_low"},
	}
	for _, c := range cases {
		got := DetectAnomalies(30, c.rate, nil, nil, nil, "")
		if c.want == "" {
			if len(got) != 0 {
				t.Errorf("rate=%v 应无异常，得到 %v", c.rate, got)
			}
			continue
		}
		if len(got) != 1 || got[0] != c.want {
			t.Errorf("rate=%v 应得 [%s]，得到 %v", c.rate, c.want, got)
		}
	}
}

func TestDetectAnomalies_Discrimination(t *testing.T) {
	got := DetectAnomalies(30, 0.5, wantFloat(0.19), nil, nil, "")
	if len(got) != 1 || got[0] != "low_discrimination" {
		t.Errorf("区分度 0.19 应报 low_discrimination，得到 %v", got)
	}
	// 恰在阈值不算（严格小于）
	if got := DetectAnomalies(30, 0.5, wantFloat(0.2), nil, nil, ""); len(got) != 0 {
		t.Errorf("区分度 0.2 应无异常，得到 %v", got)
	}
	// nil（不可计算）不判定
	if got := DetectAnomalies(30, 0.5, nil, nil, nil, ""); len(got) != 0 {
		t.Errorf("区分度 nil 不应判定，得到 %v", got)
	}
}

func TestDetectAnomalies_Distractor(t *testing.T) {
	rates := map[string]float64{"A": 0.5, "B": 0.0, "C": 0.3}
	// 正解 A，干扰项 B 无人选 → 标记一次
	got := DetectAnomalies(30, 0.5, nil, nil, rates, "A")
	if len(got) != 1 || got[0] != "no_distractor_selected" {
		t.Errorf("应报 no_distractor_selected，得到 %v", got)
	}
	// 正解 B：无人选的是正确选项本身 → 不标记
	if got := DetectAnomalies(30, 0.5, nil, nil, rates, "B"); len(got) != 0 {
		t.Errorf("correct 选项无人选不应标记，得到 %v", got)
	}
	// rates nil（非单选）不判定
	if got := DetectAnomalies(30, 0.5, nil, nil, nil, "A"); len(got) != 0 {
		t.Errorf("非单选题不应判定干扰项，得到 %v", got)
	}
	// correctOption 空（无正解结构）不判定
	if got := DetectAnomalies(30, 0.5, nil, nil, rates, ""); len(got) != 0 {
		t.Errorf("无正解不应判定干扰项，得到 %v", got)
	}
}

func TestDetectAnomalies_Time(t *testing.T) {
	got := DetectAnomalies(30, 0.5, nil, wantFloat(1500), nil, "")
	if len(got) != 1 || got[0] != "time_too_fast" {
		t.Errorf("中位耗时 1500ms 应报 time_too_fast，得到 %v", got)
	}
	// 恰在阈值不算（严格比较）
	if got := DetectAnomalies(30, 0.5, nil, wantFloat(2000), nil, ""); len(got) != 0 {
		t.Errorf("中位耗时 2000ms 应无异常，得到 %v", got)
	}
	if got := DetectAnomalies(30, 0.5, nil, wantFloat(30000), nil, ""); len(got) != 0 {
		t.Errorf("中位耗时 30000ms 应无异常，得到 %v", got)
	}
	got = DetectAnomalies(30, 0.5, nil, wantFloat(35000), nil, "")
	if len(got) != 1 || got[0] != "time_too_slow" {
		t.Errorf("中位耗时 35000ms 应报 time_too_slow，得到 %v", got)
	}
}

func TestDetectAnomalies_MultipleOrdering(t *testing.T) {
	// 四类异常齐发，标签按判定序：正确率 → 区分度 → 干扰项 → 耗时
	got := DetectAnomalies(30, 0.96, wantFloat(0.1), wantFloat(100), map[string]float64{"A": 0.9, "B": 0.0}, "A")
	want := []string{"correct_rate_too_high", "low_discrimination", "no_distractor_selected", "time_too_fast"}
	if len(got) != len(want) {
		t.Fatalf("异常数 = %d，期望 %d：%v", len(got), len(want), got)
	}
	for i := range want {
		if got[i] != want[i] {
			t.Errorf("第 %d 个标签 = %s，期望 %s（判定序确定）", i, got[i], want[i])
		}
	}
}

// ────────────────────────────────────────────────────────────────────
// HealthScoreOf（冻结实现内联公式 max(0, 1 - 0.2·n)）
// ────────────────────────────────────────────────────────────────────

func TestHealthScoreOf(t *testing.T) {
	cases := []struct {
		n    int
		want float64
	}{
		{0, 1.0},
		{1, 0.8},
		{2, 0.6},
		{3, 0.4},
		{4, 0.2},
		{5, 0.0}, // 下限 0
		{6, 0.0}, // 不为负
	}
	for _, c := range cases {
		got := HealthScoreOf(c.n)
		if c.want == 0 || c.want == 1.0 {
			if got != c.want {
				t.Errorf("HealthScoreOf(%d) = %v，期望精确 %v", c.n, got, c.want)
			}
			continue
		}
		assertApproxF(t, "health_score", got, c.want, 1e-15)
	}
}

// ────────────────────────────────────────────────────────────────────
// EvaluateHealth（冻结实现 evaluate_health 的纯聚合面）
// ────────────────────────────────────────────────────────────────────

func healthEvents(n int, correct float64, durationMs float64, selected string) []HealthEventView {
	events := make([]HealthEventView, 0, n)
	for i := 0; i < n; i++ {
		d := durationMs
		events = append(events, HealthEventView{Correct: correct, DurationMs: &d, Selected: selected})
	}
	return events
}

func TestEvaluateHealth_NoEvents(t *testing.T) {
	rep := EvaluateHealth("item-1", ScopePractice, nil, HealthOptions{})
	if rep.SampleSize != 0 || rep.HealthScore != 0.0 {
		t.Errorf("空事件报告 = %+v", rep)
	}
	if !rep.InsufficientSample {
		t.Error("空事件应标 insufficient_sample")
	}
	if rep.Metrics.Note != "no response_event in scope" {
		t.Errorf("metrics.Note = %q", rep.Metrics.Note)
	}
	if len(rep.Anomalies) != 0 {
		t.Errorf("空事件不应有异常标签，得到 %v", rep.Anomalies)
	}
}

func TestEvaluateHealth_HealthyItem(t *testing.T) {
	// 30 事件 27 对 → rate=0.9（正常区间）；中位耗时 10000ms；区分度 0.5
	events := healthEvents(30, 1.0, 10000, "")
	for i := 0; i < 3; i++ {
		events[i].Correct = 0
	}
	rep := EvaluateHealth("item-ok", ScopeDiagnosis, events, HealthOptions{Discrimination: wantFloat(0.5)})
	if rep.SampleSize != 30 {
		t.Errorf("sample_size = %d", rep.SampleSize)
	}
	assertApproxF(t, "correct_rate", rep.Metrics.CorrectRate, 0.9, 1e-15)
	assertValueF(t, "discrimination", rep.Metrics.Discrimination, 0.5, 1e-15)
	assertValueF(t, "duration_median", rep.Metrics.DurationMedianMs, 10000, 1e-9)
	if len(rep.Anomalies) != 0 {
		t.Errorf("健康题不应有异常，得到 %v", rep.Anomalies)
	}
	if rep.HealthScore != 1.0 {
		t.Errorf("health_score = %v，期望精确 1.0", rep.HealthScore)
	}
	if rep.InsufficientSample {
		t.Error("n=30 不应标 insufficient_sample")
	}
	if rep.Metrics.PurposeScope != ScopeDiagnosis {
		t.Errorf("purpose_scope = %q", rep.Metrics.PurposeScope)
	}
}

func TestEvaluateHealth_UnhealthyItem(t *testing.T) {
	// 30 事件全对（rate=1.0 → too_high）+ 区分度 0.1（low）+ 干扰项 B 无人选
	// + 中位耗时 100ms（fast）→ 4 异常 → score = 1-0.8 = 0.2（手算）
	events := healthEvents(30, 1.0, 100, "A")
	events[29].DurationMs = wantFloat(5000) // 中位数仍为 100（29×100 + 1×5000）
	rep := EvaluateHealth("item-bad", ScopePractice, events, HealthOptions{
		Discrimination:    wantFloat(0.1),
		CorrectOption:     "A",
		DistractorOptions: []string{"B", "C"},
	})
	want := []string{"correct_rate_too_high", "low_discrimination", "no_distractor_selected", "time_too_fast"}
	if len(rep.Anomalies) != len(want) {
		t.Fatalf("异常 = %v，期望 %v", rep.Anomalies, want)
	}
	for i := range want {
		if rep.Anomalies[i] != want[i] {
			t.Errorf("第 %d 个标签 = %s，期望 %s", i, rep.Anomalies[i], want[i])
		}
	}
	assertApproxF(t, "health_score", rep.HealthScore, 0.2, 1e-12)
	// 干扰项选择率含正确选项自身（rates = 计数/样本量）：A=30/30=1.0，B=C=0
	if rep.Metrics.DistractorRates["A"] != 1.0 {
		t.Errorf("A 选择率 = %v，期望 1.0", rep.Metrics.DistractorRates["A"])
	}
	if rep.Metrics.DistractorRates["B"] != 0.0 || rep.Metrics.DistractorRates["C"] != 0.0 {
		t.Errorf("B/C 选择率 = %v", rep.Metrics.DistractorRates)
	}
	if rep.Metrics.CorrectOption != "A" {
		t.Errorf("correct_option = %q", rep.Metrics.CorrectOption)
	}
}

func TestEvaluateHealth_MedianEvenCount(t *testing.T) {
	// 30 个耗时：15×100 + 15×200 → 偶数个取中间两数均值 = 150（手算）
	events := healthEvents(30, 0.5, 100, "")
	for i := 15; i < 30; i++ {
		events[i].DurationMs = wantFloat(200)
	}
	rep := EvaluateHealth("item-m", ScopePractice, events, HealthOptions{})
	assertValueF(t, "偶数中位", rep.Metrics.DurationMedianMs, 150, 1e-9)
}

func TestEvaluateHealth_InsufficientSampleNoAnomalies(t *testing.T) {
	// n=29 全对 + 快耗时：不判定异常（样本不足不伪造），但 score 恒 1.0
	events := healthEvents(29, 1.0, 100, "")
	rep := EvaluateHealth("item-s", ScopePractice, events, HealthOptions{Discrimination: wantFloat(-0.9)})
	if len(rep.Anomalies) != 0 {
		t.Errorf("样本不足应无异常标签，得到 %v", rep.Anomalies)
	}
	if !rep.InsufficientSample {
		t.Error("n=29 应标 insufficient_sample")
	}
	if rep.HealthScore != 1.0 {
		t.Errorf("无异常 score 应为 1.0，得到 %v", rep.HealthScore)
	}
}

// ────────────────────────────────────────────────────────────────────
// ValidateTransition（冻结实现 transition_lifecycle 的规则面）
// ────────────────────────────────────────────────────────────────────

func TestValidateTransition(t *testing.T) {
	cases := []struct {
		name    string
		from    string
		to      string
		hasCert bool
		wantErr error // nil = 合法
	}{
		{"初始→ACTIVE", "", LifecycleActive, false, nil},
		{"初始→WATCH 非法", "", LifecycleWatch, false, ErrIllegalTransition},
		{"ACTIVE→WATCH 自动", LifecycleActive, LifecycleWatch, false, nil},
		{"ACTIVE→ACTIVE 非法", LifecycleActive, LifecycleActive, false, ErrIllegalTransition},
		{"ACTIVE→RETIRED 需证书", LifecycleActive, LifecycleRetired, true, nil},
		{"ACTIVE→RETIRED 缺证书", LifecycleActive, LifecycleRetired, false, ErrGateCertRequired},
		{"ACTIVE→QUARANTINED 非法", LifecycleActive, LifecycleQuarantined, true, ErrIllegalTransition},
		{"WATCH→ACTIVE 自动", LifecycleWatch, LifecycleActive, false, nil},
		{"WATCH→QUARANTINED 缺证书", LifecycleWatch, LifecycleQuarantined, false, ErrGateCertRequired},
		{"WATCH→QUARANTINED 带证书", LifecycleWatch, LifecycleQuarantined, true, nil},
		{"WATCH→RETIRED 带证书", LifecycleWatch, LifecycleRetired, true, nil},
		{"QUARANTINED→WATCH 释放", LifecycleQuarantined, LifecycleWatch, false, nil},
		{"QUARANTINED→ACTIVE 非法", LifecycleQuarantined, LifecycleActive, false, ErrIllegalTransition},
		{"QUARANTINED→RETIRED 带证书", LifecycleQuarantined, LifecycleRetired, true, nil},
		{"RETIRED→ACTIVE 终态回边", LifecycleRetired, LifecycleActive, true, ErrTerminalTransition},
		{"RETIRED→RETIRED 终态", LifecycleRetired, LifecycleRetired, true, ErrTerminalTransition},
		{"非法 to_state", LifecycleActive, "BOGUS", false, ErrUnknownLifecycleState},
	}
	for _, c := range cases {
		err := ValidateTransition(c.from, c.to, c.hasCert)
		if c.wantErr == nil {
			if err != nil {
				t.Errorf("%s：不应报错，得到 %v", c.name, err)
			}
			continue
		}
		if !errors.Is(err, c.wantErr) {
			t.Errorf("%s：期望 %v，得到 %v", c.name, c.wantErr, err)
		}
	}
}

func TestLifecycleConstants(t *testing.T) {
	// 状态值与 PG 枚举一致（UPPER_CASE）
	if LifecycleActive != "ACTIVE" || LifecycleWatch != "WATCH" ||
		LifecycleQuarantined != "QUARANTINED" || LifecycleRetired != "RETIRED" {
		t.Error("生命周期状态值漂移")
	}
	if HealthMinSample != 30 || AnomalyPenalty != 0.2 ||
		CorrectRateTooHigh != 0.95 || CorrectRateTooLow != 0.05 ||
		LowDiscrimination != 0.2 || TimeTooFastMs != 2000 || TimeTooSlowMs != 30000 {
		t.Error("健康度常量漂移")
	}
	if len(ActivePoolStates) != 2 {
		t.Errorf("活跃池状态数 = %d，期望 2（ACTIVE/WATCH）", len(ActivePoolStates))
	}
}
