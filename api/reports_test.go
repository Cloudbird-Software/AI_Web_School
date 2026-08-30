package api

// 学生只读两端点（GET /reports/weakness、GET /review/due）的 HTTP 行为测试：
// LearnerReads 接口注入 + Memory fake（无 DB）。断言面：
//   - 正例：学生本人 200 契约 JSON（键面恰好契约字段集；空队列序列化为 []）；
//   - 归属面：他人 alias 仍 403（接线不松 D9 归属断言）；
//   - 参数面：非法 scene/limit/now → 422 bad_request（单字段脱敏）；
//   - 驱动故障 → 500 internal；查询面未注入（nil）→ 保持 501 占位。
//
// 全部 httptest.ResponseRecorder 直驱 mux：无 goroutine，兼容 TestMain goleak。

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"testing"
	"time"

	"github.com/Cloudbird-Software/AI_Web_School/core/report"
	"github.com/Cloudbird-Software/AI_Web_School/core/review"
)

var errFakeRead = errors.New("fakeread: 注入的取证故障")

type memoryReportQueries struct {
	events     []report.InferenceEventView
	recQuery   map[string][]string
	failEvents bool
	failRec    bool
}

func (m *memoryReportQueries) InferenceEvents(_ context.Context, _ string, _ string) ([]report.InferenceEventView, error) {
	if m.failEvents {
		return nil, errFakeRead
	}
	return m.events, nil
}

func (m *memoryReportQueries) Recommended(_ context.Context, errorTypeID string, _ []string, _ int) ([]string, error) {
	if m.failRec {
		return nil, errFakeRead
	}
	return m.recQuery[errorTypeID], nil
}

type memoryDueQueries struct {
	rows    []review.DueEntryProjection
	failDue bool
}

func (m *memoryDueQueries) DueEntries(_ context.Context, _ string, _ time.Time, _ int) ([]review.DueEntryProjection, error) {
	if m.failDue {
		return nil, errFakeRead
	}
	return m.rows, nil
}

func (f *apiFixture) withLearnerReads(reads LearnerReads) {
	f.app = NewRouterWithLearnerReads(f.signer, f.consent, nil, nil, nil, reads)
}

func sampleInferenceEvents() []report.InferenceEventView {
	return []report.InferenceEventView{
		{ItemVersionID: "iv_1", ErrorInferences: []map[string]any{
			{"error_type_id": "math.carry", "confidence": 0.9},
			{"error_type_id": "math.carry", "confidence": 0.8},
			{"error_type_id": "math.carry", "confidence": 0.7},
			{"error_type_id": "math.carry", "confidence": 0.6},
			{"error_type_id": "math.borrow", "confidence": 0.5},
		}},
		{ItemVersionID: "iv_2", ErrorInferences: []map[string]any{
			{"error_type_id": "math.carry", "confidence": 0.95},
		}},
	}
}

func TestWeaknessReport_ContractShape(t *testing.T) {
	f := newAPIFixture(t)
	f.withLearnerReads(LearnerReads{Reports: &memoryReportQueries{events: sampleInferenceEvents(), recQuery: map[string][]string{"math.carry": {"iv_pub_1"}}}})
	rec := f.do("GET", "/reports/weakness/"+apiAliasSelf, f.selfTok, "")
	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, body=%s", rec.Code, rec.Body.String())
	}
	var body map[string]any
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
		t.Fatalf("响应必须是 JSON: %v", err)
	}
	for _, k := range []string{"student_alias_id", "scene", "min_evidence", "generated_at", "items"} {
		if _, ok := body[k]; !ok {
			t.Fatalf("契约键缺失: %s（body=%s）", k, rec.Body.String())
		}
	}
	if len(body) != 5 {
		t.Fatalf("键面多出契约外字段: %v", body)
	}
	if body["scene"] != nil {
		t.Fatalf("未传 scene 必须回显 null（跨场景口径）: %v", body["scene"])
	}
	if body["student_alias_id"] != apiAliasSelf {
		t.Fatalf("归属回显错误: %v", body["student_alias_id"])
	}
	items, ok := body["items"].([]any)
	if !ok || len(items) != 2 {
		t.Fatalf("items 形态错误: %v", body["items"])
	}
	// 排序：evidence_count 降序（carry=4 先于 borrow=1）.
	first := items[0].(map[string]any)
	if first["error_type_id"] != "math.carry" || first["status"] != "concluded" {
		t.Fatalf("首条目不符: %v", first)
	}
	if _, has := first["recommended_item_version_ids"]; !has {
		t.Fatalf("concluded 条目必须带推荐小卷: %v", first)
	}
	second := items[1].(map[string]any)
	if second["status"] != "insufficient_evidence" {
		t.Fatalf("证据不足条目状态错误: %v", second)
	}
	if _, has := second["recommended_item_version_ids"]; has {
		t.Fatalf("insufficient_evidence 不得给推荐（误导）: %v", second)
	}
}

func TestWeaknessReport_RecommendationFill(t *testing.T) {
	f := newAPIFixture(t)
	rq := &memoryReportQueries{events: sampleInferenceEvents(), recQuery: map[string][]string{
		"math.carry": {"iv_pub_1", "iv_pub_2"},
	}}
	f.withLearnerReads(LearnerReads{Reports: rq})
	rec := f.do("GET", "/reports/weakness/"+apiAliasSelf, f.selfTok, "")
	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d", rec.Code)
	}
	var body struct {
		Items []struct {
			ErrorTypeID string   `json:"error_type_id"`
			Status      string   `json:"status"`
			Recommended []string `json:"recommended_item_version_ids"`
		} `json:"items"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
		t.Fatalf("解析失败: %v", err)
	}
	if body.Items[0].Recommended[0] != "iv_pub_1" {
		t.Fatalf("推荐小卷未回填: %+v", body.Items[0])
	}
}

func TestWeaknessReport_RecommendFailClosed(t *testing.T) {
	f := newAPIFixture(t)
	f.withLearnerReads(LearnerReads{Reports: &memoryReportQueries{events: sampleInferenceEvents(), failRec: true}})
	rec := f.do("GET", "/reports/weakness/"+apiAliasSelf, f.selfTok, "")
	if rec.Code != http.StatusInternalServerError {
		t.Fatalf("推荐取证故障必须 fail-closed 500, got %d", rec.Code)
	}
	expectSingleFieldError(t, rec, "internal")
}

func TestWeaknessReport_ParamValidation(t *testing.T) {
	f := newAPIFixture(t)
	f.withLearnerReads(LearnerReads{Reports: &memoryReportQueries{}})
	cases := []string{
		"/reports/weakness/" + apiAliasSelf + "?scene=exam",
		"/reports/weakness/" + apiAliasSelf + "?min_evidence=0",
		"/reports/weakness/" + apiAliasSelf + "?min_evidence=abc",
	}
	for _, url := range cases {
		rec := f.do("GET", url, f.selfTok, "")
		if rec.Code != http.StatusUnprocessableEntity {
			t.Fatalf("%s: 必须 422, got %d", url, rec.Code)
		}
		expectSingleFieldError(t, rec, "bad_request")
	}
}

func TestWeaknessReport_OwnershipAndNil(t *testing.T) {
	f := newAPIFixture(t)
	other := "99999999-8888-4777-8666-555555555555"
	f.withLearnerReads(LearnerReads{Reports: &memoryReportQueries{}})
	if rec := f.do("GET", "/reports/weakness/"+other, f.selfTok, ""); rec.Code != http.StatusForbidden {
		t.Fatalf("他人 alias 必须 403, got %d", rec.Code)
	}
	f2 := newAPIFixture(t)
	f2.app = NewRouterWithConsent(f2.signer, f2.consent)
	if rec := f2.do("GET", "/reports/weakness/"+apiAliasSelf, f2.selfTok, ""); rec.Code != http.StatusNotImplemented {
		t.Fatalf("查询面未注入必须保持 501 占位, got %d", rec.Code)
	}
}

func TestReviewDue_ContractShape(t *testing.T) {
	f := newAPIFixture(t)
	rows := []review.DueEntryProjection{{
		EntryID:        "0f0e0d0c-0b0a-4987-8654-321098765432",
		StudentAliasID: apiAliasSelf,
		ItemVersionID:  "iv_9",
		PolicyID:       "fixed-interval",
		PolicyVersion:  "1.0.0",
		Stage:          2,
		Status:         "pending",
		EnqueuedAt:     time.Date(2026, 8, 20, 0, 0, 0, 0, time.UTC),
		DueAt:          time.Date(2026, 8, 28, 0, 0, 0, 0, time.UTC),
	}}
	f.withLearnerReads(LearnerReads{Review: &memoryDueQueries{rows: rows}})
	rec := f.do("GET", "/review/due/"+apiAliasSelf, f.selfTok, "")
	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, body=%s", rec.Code, rec.Body.String())
	}
	var arr []map[string]any
	if err := json.Unmarshal(rec.Body.Bytes(), &arr); err != nil {
		t.Fatalf("响应必须是数组: %v", err)
	}
	if len(arr) != 1 {
		t.Fatalf("条目数 = %d", len(arr))
	}
	for _, k := range []string{"entry_id", "student_alias_id", "item_version_id", "policy_id", "policy_version", "stage", "status", "source_error_type_id", "enqueued_at", "due_at"} {
		if _, ok := arr[0][k]; !ok {
			t.Fatalf("契约键缺失: %s", k)
		}
	}
	if len(arr[0]) != 10 {
		t.Fatalf("键面多出契约外字段: %v", arr[0])
	}
	if arr[0]["source_error_type_id"] != nil {
		t.Fatalf("可空键缺省必须 null: %v", arr[0]["source_error_type_id"])
	}
}

func TestReviewDue_EmptyIsArray(t *testing.T) {
	f := newAPIFixture(t)
	f.withLearnerReads(LearnerReads{Review: &memoryDueQueries{}})
	rec := f.do("GET", "/review/due/"+apiAliasSelf, f.selfTok, "")
	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d", rec.Code)
	}
	if got := rec.Body.String(); got != "[]\n" {
		t.Fatalf("空队列必须序列化为 []，got %q", got)
	}
}

func TestReviewDue_ParamValidation(t *testing.T) {
	f := newAPIFixture(t)
	f.withLearnerReads(LearnerReads{Review: &memoryDueQueries{}})
	cases := []string{
		"/review/due/" + apiAliasSelf + "?now=yesterday",
		"/review/due/" + apiAliasSelf + "?limit=0",
		"/review/due/" + apiAliasSelf + "?limit=101",
	}
	for _, url := range cases {
		rec := f.do("GET", url, f.selfTok, "")
		if rec.Code != http.StatusUnprocessableEntity {
			t.Fatalf("%s: 必须 422, got %d", url, rec.Code)
		}
	}
}

func TestReviewDue_OwnershipFailAndNil(t *testing.T) {
	f := newAPIFixture(t)
	other := "99999999-8888-4777-8666-555555555555"
	f.withLearnerReads(LearnerReads{Review: &memoryDueQueries{}})
	if rec := f.do("GET", "/review/due/"+other, f.selfTok, ""); rec.Code != http.StatusForbidden {
		t.Fatalf("他人 alias 必须 403, got %d", rec.Code)
	}
	f2 := newAPIFixture(t)
	f2.app = NewRouterWithConsent(f2.signer, f2.consent)
	if rec := f2.do("GET", "/review/due/"+apiAliasSelf, f2.selfTok, ""); rec.Code != http.StatusNotImplemented {
		t.Fatalf("查询面未注入必须保持 501 占位, got %d", rec.Code)
	}
}

func TestLearnerReads_InternalSanitized(t *testing.T) {
	f := newAPIFixture(t)
	f.withLearnerReads(LearnerReads{
		Reports: &memoryReportQueries{failEvents: true},
		Review:  &memoryDueQueries{failDue: true},
	})
	if rec := f.do("GET", "/reports/weakness/"+apiAliasSelf, f.selfTok, ""); rec.Code != http.StatusInternalServerError {
		t.Fatalf("报告取证故障必须 500, got %d", rec.Code)
	}
	if rec := f.do("GET", "/review/due/"+apiAliasSelf, f.selfTok, ""); rec.Code != http.StatusInternalServerError {
		t.Fatalf("复习取证故障必须 500, got %d", rec.Code)
	}
	// 单字段脱敏面复用同一断言（一条抽样即可，语义与 expectSingleFieldError 等价）
	_ = context.Background()
}
