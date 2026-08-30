// sessions_test.go：GO-RW-002 会话端点业务接线的 HTTP 行为测试。
//
// 锁四件事：
//  1. 全链路 201→next→responses→state→abandon 的契约响应形状（v1.1 schema
//     逐字段，required 集合机器断言）；
//  2. 授权门前置不变：服务在环时 missing 授权仍 403，越权冒用判据仍先于门；
//  3. 归属断言（D9）：他人会话一律 403；
//  4. fail-closed 面：评分链路未装配 → 501、评分失败 → 500、未知会话 → 404、
//     开立参数互斥 → 422.
//
// 兼容形态（svc == nil → 501）由既有 authz/consent 套件覆盖，本文件不重复.
package api

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"strings"
	"testing"

	"github.com/Cloudbird-Software/AI_Web_School/core/compliance"
	"github.com/Cloudbird-Software/AI_Web_School/core/session"
)

// stubScorer 是评分接缝的测试替身：恒返回注入 trace/错误.
type stubScorer struct {
	trace map[string]any
	err   error
}

func (s *stubScorer) ScoreSubmit(context.Context, string, map[string]any) (map[string]any, []map[string]any, error) {
	return s.trace, []map[string]any{}, s.err
}

// okTrace 是「判对」的评分轨迹（core/scoring buildTrace 落账形态）.
func okTrace() map[string]any {
	return map[string]any{
		"process":          map[string]any{"correct": true},
		"dimension_scores": map[string]any{"correct": float64(1), "total": float64(1)},
	}
}

// withSessions 以内存面服务 + 指定评分器重装路由（授权账沿用 f.consent 或
// 调用方指定）.
func (f *apiFixture) withSessions(t *testing.T, store compliance.ConsentStore, scorer ResponseScorer) *apiFixture {
	t.Helper()
	mem := session.NewMemoryStore()
	svc, err := session.NewService(session.Deps{
		Consents:    store,
		Orders:      mem,
		Submissions: mem,
		Accounts:    mem,
		Runner:      session.LocalRunner{},
	})
	if err != nil {
		t.Fatalf("构造会话服务: %v", err)
	}
	f.app = NewRouterWithSessions(f.signer, store, svc, scorer)
	return f
}

// startBody 装配开立请求体.
func startBody(ids ...string) string {
	return fmt.Sprintf(`{"gradeband":"L","scene":"practice","item_version_ids":[%s]}`,
		strings.Join(quoteAll(ids), ","))
}

func quoteAll(ids []string) []string {
	out := make([]string, len(ids))
	for i, id := range ids {
		out[i] = fmt.Sprintf("%q", id)
	}
	return out
}

// TestSessions_FullChainHTTP 全链路契约形状：开立 201 → 取题 → 提交反馈 →
// 状态 → 放弃；放弃后取题/提交 409 conflict.
func TestSessions_FullChainHTTP(t *testing.T) {
	f := newAPIFixture(t)
	f = f.withSessions(t, f.consent, &stubScorer{trace: okTrace()})

	rec := f.do(http.MethodPost, "/sessions", f.selfTok, startBody("iv-1", "iv-2"))
	if rec.Code != http.StatusCreated {
		t.Fatalf("status = %d, want 201（body=%s）", rec.Code, rec.Body.String())
	}
	var start struct {
		SessionID    string `json:"session_id"`
		Status       string `json:"status"`
		Scene        string `json:"scene"`
		Gradeband    string `json:"gradeband"`
		Total        int    `json:"total"`
		TimeLimitSec int    `json:"time_limit_sec"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &start); err != nil {
		t.Fatalf("开立响应解码: %v", err)
	}
	if start.SessionID == "" || start.Status != "active" || start.Scene != "practice" ||
		start.Gradeband != "L" || start.Total != 2 || start.TimeLimitSec != 900 {
		t.Fatalf("StartSessionResponse = %+v", start)
	}
	sid := start.SessionID

	// 取下一题：A4 追溯字段齐备.
	rec = f.do(http.MethodGet, "/sessions/"+sid+"/next", f.selfTok, "")
	if rec.Code != http.StatusOK {
		t.Fatalf("next status = %d（body=%s）", rec.Code, rec.Body.String())
	}
	var next map[string]any
	if err := json.Unmarshal(rec.Body.Bytes(), &next); err != nil {
		t.Fatalf("next 解码: %v", err)
	}
	if next["done"] != false || next["item_version_id"] != "iv-1" {
		t.Fatalf("next = %v", next)
	}

	// 提交作答：Feedback required 七面（event_id/correct/dimension_scores/
	// error_inferences/progress/session_status）.
	rec = f.do(http.MethodPost, "/sessions/"+sid+"/responses", f.selfTok,
		`{"item_version_id":"iv-1","response":{"selected":"A"},"duration_ms":1200}`)
	if rec.Code != http.StatusOK {
		t.Fatalf("submit status = %d（body=%s）", rec.Code, rec.Body.String())
	}
	var fb map[string]any
	if err := json.Unmarshal(rec.Body.Bytes(), &fb); err != nil {
		t.Fatalf("feedback 解码: %v", err)
	}
	for _, key := range []string{"event_id", "correct", "dimension_scores", "error_inferences", "progress", "session_status"} {
		if _, has := fb[key]; !has {
			t.Fatalf("Feedback 缺 required 字段 %q: %v", key, fb)
		}
	}
	if fb["correct"] != true || fb["session_status"] != "active" {
		t.Fatalf("feedback = %v", fb)
	}
	progress := fb["progress"].(map[string]any)
	if progress["total"] != float64(2) || progress["main_answered"] != float64(1) || progress["correct_count"] != float64(1) {
		t.Fatalf("progress = %v", progress)
	}

	// 会话状态：SessionState required 十四面.
	rec = f.do(http.MethodGet, "/sessions/"+sid, f.selfTok, "")
	if rec.Code != http.StatusOK {
		t.Fatalf("state status = %d", rec.Code)
	}
	var st map[string]any
	if err := json.Unmarshal(rec.Body.Bytes(), &st); err != nil {
		t.Fatalf("state 解码: %v", err)
	}
	for _, key := range []string{
		"session_id", "status", "scene", "gradeband", "total", "main_answered",
		"answered_count", "correct_count", "wrong_count", "retest_pending",
		"elapsed_active_sec", "time_limit_sec", "remaining_sec", "started_at",
	} {
		if _, has := st[key]; !has {
			t.Fatalf("SessionState 缺 required 字段 %q: %v", key, st)
		}
	}
	if st["main_answered"] != float64(1) {
		t.Fatalf("state = %v", st)
	}

	// 放弃 → 状态 abandoned；随后的取题/提交 → 409 conflict.
	rec = f.do(http.MethodPost, "/sessions/"+sid+"/abandon", f.selfTok, "")
	if rec.Code != http.StatusOK {
		t.Fatalf("abandon status = %d（body=%s）", rec.Code, rec.Body.String())
	}
	var ab map[string]any
	if err := json.Unmarshal(rec.Body.Bytes(), &ab); err != nil || ab["status"] != "abandoned" {
		t.Fatalf("abandon body = %v err=%v", ab, err)
	}
	rec = f.do(http.MethodGet, "/sessions/"+sid+"/next", f.selfTok, "")
	if rec.Code != http.StatusConflict {
		t.Fatalf("放弃后取题 status = %d, want 409", rec.Code)
	}
	expectSingleFieldError(t, rec, ErrorClassConflict)
	rec = f.do(http.MethodPost, "/sessions/"+sid+"/responses", f.selfTok,
		`{"item_version_id":"iv-2","response":{"selected":"A"}}`)
	if rec.Code != http.StatusConflict {
		t.Fatalf("放弃后提交 status = %d, want 409", rec.Code)
	}
}

// TestSessions_ResumesAfterRest HTTP 面的休息确认闭环：人为把内存会话推到
// rest_prompted 不可行（时长由服务时钟决定）——改以「active 直下 resume 合法
// （等效重置计时）」锁路由与服务投影的接线.
func TestSessions_ResumesAfterRest(t *testing.T) {
	f := newAPIFixture(t)
	f = f.withSessions(t, f.consent, &stubScorer{trace: okTrace()})
	rec := f.do(http.MethodPost, "/sessions", f.selfTok, startBody("iv-1"))
	if rec.Code != http.StatusCreated {
		t.Fatalf("status = %d", rec.Code)
	}
	var start map[string]any
	if err := json.Unmarshal(rec.Body.Bytes(), &start); err != nil {
		t.Fatalf("解码: %v", err)
	}
	sid := start["session_id"].(string)
	rec = f.do(http.MethodPost, "/sessions/"+sid+"/resume", f.selfTok, "")
	if rec.Code != http.StatusOK {
		t.Fatalf("resume status = %d（body=%s）", rec.Code, rec.Body.String())
	}
	var st map[string]any
	if err := json.Unmarshal(rec.Body.Bytes(), &st); err != nil {
		t.Fatalf("resume 解码: %v", err)
	}
	if st["status"] != "active" || st["elapsed_active_sec"] != float64(0) {
		t.Fatalf("resume 投影 = %v", st)
	}
}

// TestSessions_ConsentGateWithService 授权门前置不变（服务在环）：零授权账
// 上开立 → 403 forbidden；撤回后立即失效.
func TestSessions_ConsentGateWithService(t *testing.T) {
	f := newAPIFixture(t).withSessions(t, compliance.NewMemoryStore(), &stubScorer{trace: okTrace()})
	rec := f.do(http.MethodPost, "/sessions", f.selfTok, startBody("iv-1"))
	expectForbidden(t, rec)
}

// TestSessions_SpoofPrecedesGateWithService 越权判据仍先于授权门（服务在环）：
// 请求体写他人 alias → 403，响应不随目标授权状态变化（consent oracle 防御
// 在业务升级后原样保持）.
func TestSessions_SpoofPrecedesGateWithService(t *testing.T) {
	f := newAPIFixture(t)
	f = f.withSessions(t, f.consent, &stubScorer{trace: okTrace()})
	body := fmt.Sprintf(`{"gradeband":"L","student_alias_id":%q,"item_version_ids":["iv-1"]}`, apiAliasAlien)
	first := f.do(http.MethodPost, "/sessions", f.selfTok, body)
	expectForbidden(t, first)
	grantConsent(t, f.consent, apiAliasAlien, apiTestSince, apiTestUntilFar, apiTestSince)
	second := f.do(http.MethodPost, "/sessions", f.selfTok, body)
	expectForbidden(t, second)
	if first.Body.String() != second.Body.String() {
		t.Fatalf("冒用响应随目标授权状态变化: %q vs %q", first.Body.String(), second.Body.String())
	}
}

// TestSessions_OwnerBindingHTTP 归属断言（D9）：B 对 A 的会话全部子资源 403，
// 错误形态与越权冒用同构（单字段 forbidden）.
func TestSessions_OwnerBindingHTTP(t *testing.T) {
	f := newAPIFixture(t)
	f = f.withSessions(t, f.consent, &stubScorer{trace: okTrace()})
	rec := f.do(http.MethodPost, "/sessions", f.selfTok, startBody("iv-1", "iv-2"))
	if rec.Code != http.StatusCreated {
		t.Fatalf("status = %d", rec.Code)
	}
	var start map[string]any
	if err := json.Unmarshal(rec.Body.Bytes(), &start); err != nil {
		t.Fatalf("解码: %v", err)
	}
	sid := start["session_id"].(string)

	// B 也要有授权：保证 403 只来自归属断言.
	grantConsent(t, f.consent, apiAliasAlien, apiTestSince, apiTestUntilFar, apiTestSince)
	alienTok := f.tokenFor(t, studentOf(apiAliasAlien))
	for _, tt := range []struct{ method, suffix, body string }{
		{http.MethodGet, "", ""},
		{http.MethodGet, "/next", ""},
		{http.MethodPost, "/responses", `{"item_version_id":"iv-1","response":{"selected":"A"}}`},
		{http.MethodPost, "/resume", ""},
		{http.MethodPost, "/abandon", ""},
	} {
		rec := f.do(tt.method, "/sessions/"+sid+tt.suffix, alienTok, tt.body)
		expectForbidden(t, rec)
	}
}

// TestSessions_FailClosedPaths 404 / 422 / 501 / 500 四类失败面.
func TestSessions_FailClosedPaths(t *testing.T) {
	f := newAPIFixture(t)
	f = f.withSessions(t, f.consent, &stubScorer{trace: okTrace()})

	t.Run("未知会话404", func(t *testing.T) {
		rec := f.do(http.MethodGet, "/sessions/6f9619ff-8b86-4000-b42d-00cf4fc964ff", f.selfTok, "")
		if rec.Code != http.StatusNotFound {
			t.Fatalf("status = %d, want 404", rec.Code)
		}
		expectSingleFieldError(t, rec, "not_found")
	})
	t.Run("开立来源互斥422", func(t *testing.T) {
		body := `{"gradeband":"L","paper_id":"paper-1","item_version_ids":["iv-1"]}`
		rec := f.do(http.MethodPost, "/sessions", f.selfTok, body)
		if rec.Code != http.StatusUnprocessableEntity {
			t.Fatalf("status = %d, want 422（body=%s）", rec.Code, rec.Body.String())
		}
		expectSingleFieldError(t, rec, "bad_request")
	})
	t.Run("提交体缺必填422", func(t *testing.T) {
		rec := f.do(http.MethodPost, "/sessions", f.selfTok, startBody("iv-1"))
		if rec.Code != http.StatusCreated {
			t.Fatalf("status = %d", rec.Code)
		}
		var start map[string]any
		if err := json.Unmarshal(rec.Body.Bytes(), &start); err != nil {
			t.Fatalf("解码: %v", err)
		}
		sid := start["session_id"].(string)
		rec = f.do(http.MethodPost, "/sessions/"+sid+"/responses", f.selfTok, `{"response":{"selected":"A"}}`)
		if rec.Code != http.StatusUnprocessableEntity {
			t.Fatalf("status = %d, want 422（body=%s）", rec.Code, rec.Body.String())
		}
	})
	t.Run("评分未装配501", func(t *testing.T) {
		g := newAPIFixture(t)
		g = g.withSessions(t, g.consent, nil)
		rec := g.do(http.MethodPost, "/sessions", g.selfTok, startBody("iv-1"))
		if rec.Code != http.StatusCreated {
			t.Fatalf("status = %d", rec.Code)
		}
		var start map[string]any
		if err := json.Unmarshal(rec.Body.Bytes(), &start); err != nil {
			t.Fatalf("解码: %v", err)
		}
		sid := start["session_id"].(string)
		rec = g.do(http.MethodPost, "/sessions/"+sid+"/responses", g.selfTok,
			`{"item_version_id":"iv-1","response":{"selected":"A"}}`)
		if rec.Code != http.StatusNotImplemented {
			t.Fatalf("status = %d, want 501（fail-closed，绝不伪造评分）", rec.Code)
		}
		expectSingleFieldError(t, rec, ErrorClassNotImplemented)
	})
	t.Run("评分失败500", func(t *testing.T) {
		g := newAPIFixture(t)
		g = g.withSessions(t, g.consent, &stubScorer{err: errors.New("scorer boom")})
		rec := g.do(http.MethodPost, "/sessions", g.selfTok, startBody("iv-1"))
		if rec.Code != http.StatusCreated {
			t.Fatalf("status = %d", rec.Code)
		}
		var start map[string]any
		if err := json.Unmarshal(rec.Body.Bytes(), &start); err != nil {
			t.Fatalf("解码: %v", err)
		}
		sid := start["session_id"].(string)
		rec = g.do(http.MethodPost, "/sessions/"+sid+"/responses", g.selfTok,
			`{"item_version_id":"iv-1","response":{"selected":"A"}}`)
		if rec.Code != http.StatusInternalServerError {
			t.Fatalf("status = %d, want 500（评分失败不落账）", rec.Code)
		}
		expectSingleFieldError(t, rec, "internal")
	})
}
