package datastat

import (
	"encoding/json"
	"errors"
	"testing"
	"time"
)

// ────────────────────────────────────────────────────────────────────
// trace 提取助手（冻结实现 replay._safe_*_from_trace）
// ────────────────────────────────────────────────────────────────────

func TestSafeScorerVersionFromTrace(t *testing.T) {
	cases := []struct {
		name  string
		trace map[string]any
		want  string
	}{
		{"正常取版本", map[string]any{"scorer_version": "v2"}, "v2"},
		{"缺键兜底空串", map[string]any{}, ""},
		{"空串兜底空串", map[string]any{"scorer_version": ""}, ""},
		{"非字符串兜底空串", map[string]any{"scorer_version": 123}, ""},
		{"nil trace", nil, ""},
	}
	for _, c := range cases {
		if got := SafeScorerVersionFromTrace(c.trace); got != c.want {
			t.Errorf("%s = %q，期望 %q", c.name, got, c.want)
		}
	}
}

func TestSafeCorrectFromTrace(t *testing.T) {
	cases := []struct {
		name  string
		trace map[string]any
		want  *bool
	}{
		{"process.correct 为真", map[string]any{"process": map[string]any{"correct": true}}, wantBool(true)},
		{"process.correct 为假", map[string]any{"process": map[string]any{"correct": false}}, wantBool(false)},
		// bool() 语义：非空字符串为真、0 为假（冻结实现逐行对齐）
		{"process.correct 非空串为真", map[string]any{"process": map[string]any{"correct": "yes"}}, wantBool(true)},
		{"process.correct 数字 0 为假", map[string]any{"process": map[string]any{"correct": 0}}, wantBool(false)},
		{"process 无 correct 键→落到 dims", map[string]any{"process": map[string]any{"other": 1}}, nil},
		{"process 优先于 dims", map[string]any{
			"process":          map[string]any{"correct": true},
			"dimension_scores": map[string]any{"correct": 0.0},
		}, wantBool(true)},
		{"dims.correct=1.0", map[string]any{"dimension_scores": map[string]any{"correct": 1.0}}, wantBool(true)},
		{"dims.correct=0.5", map[string]any{"dimension_scores": map[string]any{"correct": 0.5}}, wantBool(false)},
		{"dims.correct 字符串 1.0", map[string]any{"dimension_scores": map[string]any{"correct": "1.0"}}, wantBool(true)},
		{"dims.correct 字符串 0", map[string]any{"dimension_scores": map[string]any{"correct": "0"}}, wantBool(false)},
		{"dims 无 correct 键", map[string]any{"dimension_scores": map[string]any{"other": 1}}, nil},
		{"全缺", map[string]any{}, nil},
		{"nil trace", nil, nil},
	}
	for _, c := range cases {
		got, err := SafeCorrectFromTrace(c.trace)
		if err != nil {
			t.Errorf("%s：不应报错，得到 %v", c.name, err)
			continue
		}
		if c.want == nil {
			if got != nil {
				t.Errorf("%s = %v，期望 nil", c.name, *got)
			}
			continue
		}
		if got == nil || *got != *c.want {
			t.Errorf("%s = %v，期望 %v", c.name, got, *c.want)
		}
	}
}

func wantBool(b bool) *bool { return &b }

func TestSafeCorrectFromTrace_Unparsable(t *testing.T) {
	// dims.correct 存在但不可解析 → 报错（冻结实现 float() 异常穿透，fail-closed）
	_, err := SafeCorrectFromTrace(map[string]any{
		"dimension_scores": map[string]any{"correct": "abc"},
	})
	if !errors.Is(err, ErrTraceCorrectUnparsable) {
		t.Errorf("不可解析 correct 应报 ErrTraceCorrectUnparsable，得到 %v", err)
	}
}

// ────────────────────────────────────────────────────────────────────
// DefaultInputSnapshotID（冻结实现 _default_input_snapshot_id）
// ────────────────────────────────────────────────────────────────────

func TestDefaultInputSnapshotID(t *testing.T) {
	if got := DefaultInputSnapshotID(0, ""); got != "snapshot:all:0" {
		t.Errorf("= %q，期望 snapshot:all:0", got)
	}
	if got := DefaultInputSnapshotID(5, ScopePractice); got != "snapshot:practice:5" {
		t.Errorf("= %q，期望 snapshot:practice:5", got)
	}
}

// ────────────────────────────────────────────────────────────────────
// ComputeSummaryHash（冻结实现 _compute_summary_hash——固定哈希地面真值，
// 用冻结 Python 实现交叉计算验证，见波次报告）
// ────────────────────────────────────────────────────────────────────

func TestComputeSummaryHash_PythonParity(t *testing.T) {
	events := []ReplayEvent{
		{EventID: "e3", ItemVersionID: "iv2", Scene: "practice"},
		{EventID: "e1", ItemVersionID: "iv1", Scene: "practice"},
		{EventID: "e2", ItemVersionID: "iv1", Scene: "diagnosis"},
	}
	rescored := []RescoreFingerprint{
		{EventID: "e1", ScorerVersion: "sv-b", Correct: true},
		{EventID: "e2", ScorerVersion: "sv-a", Correct: false},
		{EventID: "e3", ScorerVersion: "sv-b", Correct: true},
	}
	// 冻结实现输出：63f8e1aa…（乱序输入、混合 scene）
	want := "63f8e1aa5b6ab3f96cc5ca4aa9ea852737f82036ce387c69d575cddc317108be"
	if got := ComputeSummaryHash("snapshot:practice:3", events, rescored); got != want {
		t.Errorf("hash = %s，期望 %s", got, want)
	}
	// 空集（冻结实现输出：3b10ac17…）
	wantEmpty := "3b10ac176077843efc5684d127ff5a298ac38cfb442d187e1e643c5068c6276c"
	if got := ComputeSummaryHash("snapshot:all:0", nil, nil); got != wantEmpty {
		t.Errorf("空集 hash = %s，期望 %s", got, wantEmpty)
	}
	// 确定性：乱序输入同哈希
	shuffled := []ReplayEvent{events[2], events[0], events[1]}
	rs := []RescoreFingerprint{rescored[2], rescored[0], rescored[1]}
	if got := ComputeSummaryHash("snapshot:practice:3", shuffled, rs); got != want {
		t.Errorf("乱序同输入应同哈希，得到 %s", got)
	}
	// 输入不同 → 哈希不同
	if got := ComputeSummaryHash("snapshot:practice:4", events, rescored); got == want {
		t.Error("不同快照 id 不应同哈希")
	}
}

// ────────────────────────────────────────────────────────────────────
// CanonicalJSON（冻结实现 parquet_export._canonical_json——固定输出地面真值）
// ────────────────────────────────────────────────────────────────────

func TestCanonicalJSON_PythonParity(t *testing.T) {
	// 冻结实现输出（Python json.dumps(sort_keys=True, ensure_ascii=False,
	// separators=(",",":"))）：{"a":"中文<x>&","arr":[1,"x"],"b":1,"flag":true,"n":2.0,"nil":null}
	v := map[string]any{
		"b":    int64(1),
		"a":    "中文<x>&",
		"n":    float64(2.0),
		"flag": true,
		"nil":  nil,
		"arr":  []any{int64(1), "x"},
	}
	want := `{"a":"中文<x>&","arr":[1,"x"],"b":1,"flag":true,"n":2.0,"nil":null}`
	if got := CanonicalJSON(v); got != want {
		t.Errorf("CanonicalJSON = %s，期望 %s", got, want)
	}
	// json.Number 整/浮形态保真（与 Python json.loads 的 int/float 区分一致）
	if got := CanonicalJSON(map[string]any{"x": json.Number("1")}); got != `{"x":1}` {
		t.Errorf("Number 整形态 = %s", got)
	}
	if got := CanonicalJSON(map[string]any{"x": json.Number("1.50")}); got != `{"x":1.5}` {
		t.Errorf("Number 浮形态 = %s（Python repr 最短往返）", got)
	}
}

// ────────────────────────────────────────────────────────────────────
// RunReplay（冻结实现 replay_all 的聚合核）
// ────────────────────────────────────────────────────────────────────

type fakeRescorer struct {
	results map[string]fakeResult
}

type fakeResult struct {
	version string
	correct bool
	err     error
}

func (f *fakeRescorer) Rescore(ev ReplayEvent) (string, bool, error) {
	r, ok := f.results[ev.EventID]
	if !ok {
		return "", false, errors.New("scorer failed: NotRegistered: no scorer")
	}
	if r.err != nil {
		return "", false, r.err
	}
	return r.version, r.correct, nil
}

func TestRunReplay_HandExample(t *testing.T) {
	// 手算：3 事件（全 practice），旧 correct [true, false, 未知]；
	// 重判 [true, false, 失败] → rescored=2 / failed=1 / comparable=2 /
	// consistent=2 → 一致率 1.0；新旧难度近似均 0.5 → delta 0
	in := ReplayInput{
		PurposeScope: ScopePractice,
		Events: []ReplayEvent{
			{EventID: "e1", ItemVersionID: "iv1", Scene: ScopePractice,
				OriginalTrace: map[string]any{"process": map[string]any{"correct": true}}},
			{EventID: "e2", ItemVersionID: "iv1", Scene: ScopePractice,
				OriginalTrace: map[string]any{"dimension_scores": map[string]any{"correct": 0.0}}},
			{EventID: "e3", ItemVersionID: "iv2", Scene: ScopePractice,
				OriginalTrace: map[string]any{}},
		},
		ScorerVersion: "rescore-2026",
		RunLabel:      "annual-replay-2026",
	}
	rescorer := &fakeRescorer{results: map[string]fakeResult{
		"e1": {version: "sv-b", correct: true},
		"e2": {version: "sv-a", correct: false},
		// e3 无结果 → 失败
	}}
	rep, err := RunReplay(in, rescorer)
	if err != nil {
		t.Fatalf("RunReplay 失败：%v", err)
	}
	if rep.RescoredCount != 2 || rep.FailedCount != 1 || rep.SkippedCount != 0 {
		t.Errorf("rescored/skipped/failed = %d/%d/%d，期望 2/0/1", rep.RescoredCount, rep.SkippedCount, rep.FailedCount)
	}
	if rep.Consistency != 1.0 {
		t.Errorf("一致性率 = %v，期望 1.0", rep.Consistency)
	}
	if rep.ScorerVersion != "sv-b" {
		t.Errorf("实际评分器版本 = %q，期望 sv-b（首个成功自报）", rep.ScorerVersion)
	}
	if rep.RunLabel != "annual-replay-2026" {
		t.Errorf("run_label = %q", rep.RunLabel)
	}
	if rep.InputSnapshotID != "snapshot:practice:3" {
		t.Errorf("input_snapshot_id = %q，期望 snapshot:practice:3", rep.InputSnapshotID)
	}
	// 摘要哈希与冻结 Python 实现逐字节一致（固定地面真值）
	wantHash := "0bea3c2a9466d70259d807a09225885d4ea33dc7362b02ca06c8b9cc348ff5b3"
	if rep.SummaryHash != wantHash {
		t.Errorf("summary_hash = %s，期望 %s", rep.SummaryHash, wantHash)
	}
	// 旧统计：trace 均无 scorer_version 键 → {"" : 3}；correct [T,F] → 1/1
	if rep.OldParamSummary.ScorerVersions[""] != 3 {
		t.Errorf("旧版本分布 = %v，期望 {\"\":3}", rep.OldParamSummary.ScorerVersions)
	}
	if rep.OldParamSummary.CorrectTrue != 1 || rep.OldParamSummary.CorrectFalse != 1 {
		t.Errorf("旧 correct 分布 = %d/%d，期望 1/1", rep.OldParamSummary.CorrectTrue, rep.OldParamSummary.CorrectFalse)
	}
	assertValueF(t, "旧难度近似", rep.OldParamSummary.DifficultyApprox, 0.5, 1e-12)
	// 新统计：仅成功重判者 [true, false] → 0.5；差分 delta = 0
	if rep.NewParamSummary.CorrectTrue != 1 || rep.NewParamSummary.CorrectFalse != 1 {
		t.Errorf("新 correct 分布 = %d/%d，期望 1/1", rep.NewParamSummary.CorrectTrue, rep.NewParamSummary.CorrectFalse)
	}
	assertValueF(t, "新难度近似", rep.NewParamSummary.DifficultyApprox, 0.5, 1e-12)
	if rep.ParamDiff == nil {
		t.Fatal("两侧可估计时 ParamDiff 不应为 nil")
	}
	assertValueF(t, "diff old", rep.ParamDiff.DifficultyOld, 0.5, 1e-12)
	assertValueF(t, "diff new", rep.ParamDiff.DifficultyNew, 0.5, 1e-12)
	assertValueF(t, "diff delta", rep.ParamDiff.DifficultyDelta, 0.0, 1e-12)
	// 失败详情
	if len(rep.Failures) != 1 {
		t.Fatalf("failures = %d 条，期望 1", len(rep.Failures))
	}
	f := rep.Failures[0]
	if f.EventID != "e3" || f.ItemVersionID != "iv2" || f.Reason == "" {
		t.Errorf("失败详情 = %+v", f)
	}
}

func TestRunReplay_PartialConsistency(t *testing.T) {
	// 手算：旧 [true, false]，新 [false, false] → 一致 1/2 = 0.5；
	// 旧难度 0.5，新难度 0.0 → delta = -0.5
	in := ReplayInput{
		PurposeScope: ScopePractice,
		Events: []ReplayEvent{
			{EventID: "e1", ItemVersionID: "iv1", Scene: ScopePractice,
				OriginalTrace: map[string]any{"process": map[string]any{"correct": true}}},
			{EventID: "e2", ItemVersionID: "iv1", Scene: ScopePractice,
				OriginalTrace: map[string]any{"process": map[string]any{"correct": false}}},
		},
		ScorerVersion: "v2",
	}
	rescorer := &fakeRescorer{results: map[string]fakeResult{
		"e1": {version: "v2", correct: false},
		"e2": {version: "v2", correct: false},
	}}
	rep, err := RunReplay(in, rescorer)
	if err != nil {
		t.Fatalf("RunReplay 失败：%v", err)
	}
	if rep.Consistency != 0.5 {
		t.Errorf("一致性率 = %v，期望 0.5", rep.Consistency)
	}
	assertValueF(t, "旧难度", rep.ParamDiff.DifficultyOld, 0.5, 1e-12)
	assertValueF(t, "新难度", rep.ParamDiff.DifficultyNew, 0.0, 1e-12)
	assertValueF(t, "delta", rep.ParamDiff.DifficultyDelta, -0.5, 1e-12)
}

func TestRunReplay_EmptyEvents(t *testing.T) {
	rep, err := RunReplay(ReplayInput{PurposeScope: ScopePractice, ScorerVersion: "v2"}, &fakeRescorer{})
	if err != nil {
		t.Fatalf("RunReplay 失败：%v", err)
	}
	if rep.RescoredCount != 0 || rep.FailedCount != 0 {
		t.Errorf("空快照应全零计数，得到 %+v", rep.RescoreReport)
	}
	if rep.SummaryHash != "" {
		t.Errorf("空快照 hash 应为空串（冻结实现早退），得到 %q", rep.SummaryHash)
	}
	if rep.InputSnapshotID != "snapshot:practice:0" {
		t.Errorf("input_snapshot_id = %q", rep.InputSnapshotID)
	}
	if rep.ParamDiff != nil {
		t.Error("空快照不应有参数差分")
	}
}

func TestRunReplay_Errors(t *testing.T) {
	_, err := RunReplay(ReplayInput{PurposeScope: "exam", ScorerVersion: "v2"}, &fakeRescorer{})
	if !errors.Is(err, ErrInvalidPurposeScope) {
		t.Errorf("越域 scope 应报 ErrInvalidPurposeScope，得到 %v", err)
	}
	_, err = RunReplay(ReplayInput{PurposeScope: ScopePractice}, &fakeRescorer{})
	if !errors.Is(err, ErrScorerVersionRequired) {
		t.Errorf("缺评分器版本应报 ErrScorerVersionRequired，得到 %v", err)
	}
	// trace correct 不可解析 → 整体失败（fail-closed，冻结实现异常穿透）
	in := ReplayInput{
		PurposeScope: ScopePractice,
		Events: []ReplayEvent{
			{EventID: "e1", ItemVersionID: "iv1", Scene: ScopePractice,
				OriginalTrace: map[string]any{"dimension_scores": map[string]any{"correct": "oops"}}},
		},
		ScorerVersion: "v2",
	}
	if _, err = RunReplay(in, &fakeRescorer{}); !errors.Is(err, ErrTraceCorrectUnparsable) {
		t.Errorf("不可解析 correct 应整体失败，得到 %v", err)
	}
}

func TestRunReplay_CustomSnapshotID(t *testing.T) {
	rep, err := RunReplay(ReplayInput{
		PurposeScope:    ScopeDiagnosis,
		Events:          []ReplayEvent{{EventID: "e1", ItemVersionID: "iv1", Scene: ScopeDiagnosis, OriginalTrace: nil}},
		ScorerVersion:   "v2",
		InputSnapshotID: "snapshot-2026-08-30",
	}, &fakeRescorer{results: map[string]fakeResult{
		"e1": {version: "v2", correct: true},
	}})
	if err != nil {
		t.Fatalf("RunReplay 失败：%v", err)
	}
	if rep.InputSnapshotID != "snapshot-2026-08-30" {
		t.Errorf("自定义快照 id = %q", rep.InputSnapshotID)
	}
	if rep.RescoredCount != 1 || rep.Consistency != 0.0 {
		t.Errorf("nil trace 不可比对：rescored=%d consistency=%v，期望 1/0.0", rep.RescoredCount, rep.Consistency)
	}
}

// ────────────────────────────────────────────────────────────────────
// Parquet 归档面纯助手（冻结实现 parquet_export.py 的命名/区间/去重）
// ────────────────────────────────────────────────────────────────────

func TestBuildParquetOutputPath(t *testing.T) {
	day := time.Date(2026, 8, 30, 15, 0, 0, 0, time.UTC)
	// {base}/date=YYYY-MM-DD/scene={scene}/events-YYYYMMDD-{scene}.parquet
	want := "archive/date=2026-08-30/scene=practice/events-20260830-practice.parquet"
	if got := BuildParquetOutputPath("archive", day, ScopePractice); got != want {
		t.Errorf("输出路径 = %q，期望 %q", got, want)
	}
}

func TestParquetDateRangeUTC(t *testing.T) {
	day := time.Date(2026, 8, 30, 23, 59, 0, 0, time.UTC)
	start, end := ParquetDateRangeUTC(day)
	if !start.Equal(time.Date(2026, 8, 30, 0, 0, 0, 0, time.UTC)) {
		t.Errorf("区间左端 = %v", start)
	}
	if !end.Equal(time.Date(2026, 8, 31, 0, 0, 0, 0, time.UTC)) {
		t.Errorf("区间右端 = %v（UTC 半开区间）", end)
	}
}

func TestDedupByEventID(t *testing.T) {
	rows := []struct {
		EventID string
		Seq     int
	}{
		{"e1", 1}, {"e2", 2}, {"e1", 3}, {"e3", 4}, {"e1", 5},
	}
	deduped := DedupByEventID(rows, func(r struct {
		EventID string
		Seq     int
	}) string {
		return r.EventID
	})
	if len(deduped) != 3 {
		t.Fatalf("去重后行数 = %d，期望 3", len(deduped))
	}
	// 保留首条（行已按 created_at,event_id 排序）
	if deduped[0].Seq != 1 || deduped[1].EventID != "e2" || deduped[2].EventID != "e3" {
		t.Errorf("去重保留首条违约：%+v", deduped)
	}
	if got := DedupByEventID(nil, func(r struct{ X int }) string { return "" }); len(got) != 0 {
		t.Error("空输入应返回空")
	}
}

func TestSummaryHashSingleElement(t *testing.T) {
	// 单元素数组序列化（无 ", " 拼接路径）输出仍为 64 位 SHA256 hex
	got := ComputeSummaryHash("x", []ReplayEvent{{EventID: "a", ItemVersionID: "b", Scene: "c"}},
		[]RescoreFingerprint{{EventID: "d", ScorerVersion: "e", Correct: true}})
	if len(got) != 64 {
		t.Errorf("SHA256 hex 长度 = %d，期望 64", len(got))
	}
}
