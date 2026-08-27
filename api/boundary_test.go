package api

// 边界链集成测试（T-W5-008 验收 #2/#4/#5）：真实 http.Server 上验证固定
// 链序 recover → cors → rate-limit(IP) → body-limit → 路由盾（auth → 主体
// 配额）→ handler 的端到端行为与顺序论证。限流窗口用大 burst + 少量请求
// 控制，无 sleep；-race 下运行。
//
// 顺序的可观测证据：
//   - 跨域拒绝反复 403（而非第二次起 429）→ cors 先于限流；
//   - 跨域拒绝返回 403 而非 mux 的 404 → cors 先于路由；
//   - 无令牌超大请求体返回 413 而非 401 → body-limit 先于 auth；
//   - 匿名业务路由 401、panic 收敛 500 单字段 → 链路贯通。

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/Cloudbird-Software/AI_Web_School/api/middleware"
	"github.com/Cloudbird-Software/AI_Web_School/core/auth"
)

const boundaryTestKeyMaterial = "boundary-test-secret-0123456789-t-w5-008"

const (
	aliasA = "11111111-2222-4333-8444-555555555555"
	aliasB = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
)

type boundaryFixture struct {
	t      *testing.T
	srv    *httptest.Server
	signer *auth.Signer
}

func newBoundaryFixture(t *testing.T) *boundaryFixture {
	t.Helper()
	cfg := BoundaryConfig{
		CORS: middleware.CORSConfig{
			AllowedOrigins:   []string{"https://app.example.com"},
			AllowCredentials: true,
		},
		Rate: middleware.RateLimitConfig{
			// default 压到 1：健康探针若未豁免必然立刻 429（验收 #5 的尖锐化）
			IPScopes: map[middleware.RateScope]middleware.RateQuota{
				middleware.ScopeDefault: {PerMinute: 1, Burst: 1},
				// IP 维 submit burst=4；主体维 submit burst=2。计费轨迹：
				// A#1/A#2 过两维（IP 余 2）；A#3 过 IP（余 1）后被主体桶拒 →
				// 429；B#1 过 IP（余 0）与自身主体桶 → 200（反证 A#3 是主体
				// 维度拒绝）；B#2 IP 桶见底 → 429（IP 维度证明）
				middleware.ScopeSubmit: {PerMinute: 4, Burst: 4},
				middleware.ScopeReport: {PerMinute: 60, Burst: 60},
			},
			PrincipalScopes: map[middleware.RateScope]middleware.RateQuota{
				middleware.ScopeDefault: {PerMinute: 1, Burst: 1},
				middleware.ScopeSubmit:  {PerMinute: 2, Burst: 2},
				middleware.ScopeReport:  {PerMinute: 60, Burst: 60},
			},
		},
		MaxBodyBytes: 512,
	}
	signer, err := auth.NewSignerWithClock([]byte(boundaryTestKeyMaterial), nil)
	if err != nil {
		t.Fatalf("构造 Signer: %v", err)
	}
	limiter := middleware.NewRateLimiter(cfg.Rate, nil)

	// 路由盾：认证在外、主体配额在内（执行序 auth → 主体配额 → handler，
	// 与 boundary.go 固定链序的尾部一致；配额依赖认证注入的主体）。
	shield := func(scope middleware.RateScope) func(http.Handler) http.Handler {
		return func(next http.Handler) http.Handler {
			return middleware.RequireAuth(signer, auth.RoleStudent)(
				middleware.RateLimitPrincipal(limiter, scope)(next))
		}
	}
	echoOK := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if _, ok := middleware.FromContext(r.Context()); !ok {
			t.Error("受盾路由必须能取到主体")
		}
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte(`{"ok":true}`))
	})
	reportDemo := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		p, _ := middleware.FromContext(r.Context())
		if err := auth.AssertOwnsAlias(p, r.PathValue("student_alias_id")); err != nil {
			middleware.WriteAuthErrorResponse(w, err)
			return
		}
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte(`{"report":true}`))
	})
	panicDemo := http.HandlerFunc(func(_ http.ResponseWriter, _ *http.Request) {
		panic("secret-internal-detail /srv/app/db/main.go:88")
	})

	f := &boundaryFixture{t: t, signer: signer}
	mux := http.NewServeMux()
	// 生产健康探针同一对 handler（契约路径 + 编排别名）
	mux.HandleFunc("GET /health", healthHandler)
	mux.HandleFunc("GET /healthz", healthHandler)
	mux.Handle("POST /sessions", shield(middleware.ScopeSubmit)(echoOK))
	mux.Handle("GET /reports/weakness/{student_alias_id}", shield(middleware.ScopeReport)(reportDemo))
	mux.Handle("GET /panic", middleware.RequireAuth(signer)(panicDemo))
	f.srv = httptest.NewServer(withBoundary(cfg, limiter, mux))
	t.Cleanup(f.srv.Close)
	return f
}

func (f *boundaryFixture) token(t *testing.T, subject, alias string) string {
	t.Helper()
	tok, err := f.signer.Issue(auth.Principal{
		Role: auth.RoleStudent, SubjectID: subject, AliasID: alias,
	}, time.Hour)
	if err != nil {
		t.Fatalf("签发令牌: %v", err)
	}
	return tok
}

func (f *boundaryFixture) do(req *http.Request) *http.Response {
	f.t.Helper()
	resp, err := f.srv.Client().Do(req)
	if err != nil {
		f.t.Fatalf("请求失败: %v", err)
	}
	f.t.Cleanup(func() { _ = resp.Body.Close() })
	return resp
}

func (f *boundaryFixture) get(path, origin, token string) *http.Response {
	f.t.Helper()
	req, err := http.NewRequestWithContext(context.Background(), http.MethodGet, f.srv.URL+path, nil)
	if err != nil {
		f.t.Fatalf("构造请求: %v", err)
	}
	if origin != "" {
		req.Header.Set("Origin", origin)
	}
	if token != "" {
		req.Header.Set("Authorization", "Bearer "+token)
	}
	return f.do(req)
}

// decodeErrorClass 断言单字段 error_class JSON（字段最小化是脱敏验收的
// 可执行形态，惯例同 middleware 测试）。
func decodeErrorClass(t *testing.T, resp *http.Response) map[string]any {
	t.Helper()
	body, err := io.ReadAll(resp.Body)
	if err != nil {
		t.Fatalf("读响应体: %v", err)
	}
	var out map[string]any
	if err := json.Unmarshal(body, &out); err != nil {
		t.Fatalf("响应必须是 JSON: %v（body=%q）", err, body)
	}
	if len(out) != 1 {
		t.Fatalf("错误响应字段必须最小化（仅 error_class），得到 %v", out)
	}
	if out["error_class"] == "" {
		t.Fatalf("error_class 不得为空: %v", out)
	}
	return out
}

// TestBoundary_HealthExemptAndContractPath 健康探针不限流（验收 #5）：
// default 配额已被压到 1，仍可反复探测；契约冻结路径 /health 与编排别名
// /healthz 同语义（T-W5-006 遗留收口）。
func TestBoundary_HealthExemptAndContractPath(t *testing.T) {
	f := newBoundaryFixture(t)
	for i := 0; i < 5; i++ {
		resp := f.get("/healthz", "", "")
		if resp.StatusCode != http.StatusOK {
			t.Fatalf("/healthz 第 %d 次被限流/拒绝: %d", i+1, resp.StatusCode)
		}
		var body map[string]any
		if err := json.NewDecoder(resp.Body).Decode(&body); err != nil {
			t.Fatalf("/healthz 必须是 JSON: %v", err)
		}
		if len(body) != 1 || body["status"] != "ok" {
			t.Fatalf("/healthz 响应体 = %v, want 仅 status:ok", body)
		}
	}
	resp := f.get("/health", "", "")
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("契约路径 /health status = %d, want 200", resp.StatusCode)
	}
	var body map[string]any
	if err := json.NewDecoder(resp.Body).Decode(&body); err != nil || body["status"] != "ok" {
		t.Fatalf("/health 响应体必须与 /healthz 同语义: %v (%v)", body, err)
	}
}

// TestBoundary_CORSAndOrdering 跨域拒绝/放行 + 顺序论证的可观测证据。
func TestBoundary_CORSAndOrdering(t *testing.T) {
	f := newBoundaryFixture(t)

	// 白名单外 Origin：403 而非 404（cors 先于 mux），且反复 403 而非 429
	// （cors 先于限流——default 配额只有 1，若限流在前第二次必 429）
	for i := 0; i < 3; i++ {
		resp := f.get("/no-such-path", "https://evil.example.com", "")
		if resp.StatusCode != http.StatusForbidden {
			t.Fatalf("第 %d 次跨域 status = %d, want 403", i+1, resp.StatusCode)
		}
		if got := decodeErrorClass(t, resp)["error_class"]; got != middleware.ErrorClassOriginForbidden {
			t.Fatalf("error_class = %v", got)
		}
		if resp.Header.Get("Access-Control-Allow-Origin") != "" {
			t.Fatal("拒绝响应不得携带 ACAO")
		}
	}

	// 白名单内 Origin：放行 + 精确回显 + 显式凭据头 + Vary: Origin
	resp := f.get("/no-such-path", "https://app.example.com", "")
	if resp.StatusCode != http.StatusOK && resp.StatusCode != http.StatusNotFound {
		t.Fatalf("白名单内跨域不应被 CORS 拒绝: %d", resp.StatusCode)
	}
	if got := resp.Header.Get("Access-Control-Allow-Origin"); got != "https://app.example.com" {
		t.Fatalf("ACAO = %q, want 精确回显", got)
	}
	if got := resp.Header.Get("Access-Control-Allow-Credentials"); got != "true" {
		t.Fatalf("显式凭据模式 ACA-Credentials = %q, want true", got)
	}
	if got := resp.Header.Get("Vary"); !strings.Contains(got, "Origin") {
		t.Fatalf("Vary = %q, 必须含 Origin", got)
	}

	// 预检：白名单内 204 短路；白名单外 403
	preflight := func(origin string) *http.Response {
		req, err := http.NewRequestWithContext(context.Background(), http.MethodOptions, f.srv.URL+"/sessions", nil)
		if err != nil {
			t.Fatalf("构造预检: %v", err)
		}
		req.Header.Set("Origin", origin)
		req.Header.Set("Access-Control-Request-Method", http.MethodPost)
		return f.do(req)
	}
	resp = preflight("https://app.example.com")
	if resp.StatusCode != http.StatusNoContent {
		t.Fatalf("预检 status = %d, want 204", resp.StatusCode)
	}
	if resp.Header.Get("Access-Control-Allow-Methods") == "" {
		t.Fatal("预检必须声明允许方法")
	}
	resp = preflight("https://evil.example.com")
	if resp.StatusCode != http.StatusForbidden {
		t.Fatalf("跨域预检 status = %d, want 403", resp.StatusCode)
	}
}

// TestBoundary_RateLimitDualDimension 限流双维度 + 独立配额（验收 #2）：
// 主体桶先空（A 第 3 次 429）而另一主体不受影响；报告查询独立配额；
// 最终 IP 桶见底（B 第 3 次 429）。
func TestBoundary_RateLimitDualDimension(t *testing.T) {
	f := newBoundaryFixture(t)
	tokA := f.token(t, "acc-A", aliasA)
	tokB := f.token(t, "acc-B", aliasB)

	post := func(token string) *http.Response {
		req, err := http.NewRequestWithContext(context.Background(), http.MethodPost,
			f.srv.URL+"/sessions", strings.NewReader(`{"student_alias_id":"`+aliasA+`"}`))
		if err != nil {
			t.Fatalf("构造请求: %v", err)
		}
		req.Header.Set("Authorization", "Bearer "+token)
		req.Header.Set("Content-Type", "application/json")
		return f.do(req)
	}

	// 学生 A：主体配额 submit burst=2
	if resp := post(tokA); resp.StatusCode != http.StatusOK {
		t.Fatalf("A 第 1 次 status = %d, want 200", resp.StatusCode)
	}
	if resp := post(tokA); resp.StatusCode != http.StatusOK {
		t.Fatalf("A 第 2 次 status = %d, want 200", resp.StatusCode)
	}
	resp := post(tokA)
	if resp.StatusCode != http.StatusTooManyRequests {
		t.Fatalf("A 第 3 次 status = %d, want 429（主体配额耗尽）", resp.StatusCode)
	}
	if got := decodeErrorClass(t, resp)["error_class"]; got != middleware.ErrorClassRateLimited {
		t.Fatalf("error_class = %v, want rate_limited", got)
	}
	if secs := resp.Header.Get("Retry-After"); secs == "" {
		t.Fatal("429 必须带 Retry-After 重试提示")
	}

	// 报告查询独立配额：提交域打空不波及查询域
	resp = f.get("/reports/weakness/"+aliasA, "", tokA)
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("报告查询 status = %d, want 200（独立配额）", resp.StatusCode)
	}

	// 学生 B（同 IP）：主体桶独立，仍可提交 → 主体维度证明。
	// 推理：若 A#3 的 429 来自 IP 桶，B 的这次请求也必然被拒；B 通过
	// 即证明 A#3 是主体维度拒绝（B#1 后 IP 桶恰好见底）。
	resp = post(tokB)
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("B 第 1 次 status = %d, want 200（主体隔离）", resp.StatusCode)
	}

	// B 第 2 次：IP 桶见底 → 429（主体桶尚余 3，必是 IP 维度拒绝）
	resp = post(tokB)
	if resp.StatusCode != http.StatusTooManyRequests {
		t.Fatalf("B 第 2 次 status = %d, want 429（IP 配额耗尽）", resp.StatusCode)
	}

	// 匿名业务路由：认证盾在限流配额内正常工作
	resp = f.get("/reports/weakness/"+aliasA, "", "")
	if resp.StatusCode != http.StatusUnauthorized {
		t.Fatalf("匿名 status = %d, want 401", resp.StatusCode)
	}
}

// TestBoundary_PanicAndBodyLimit panic 收敛与体限（验收 #3/#4 + T-W5-006
// 遗留收口）：panic → 500 单字段零内部细节；超大体量在 auth 之前被 413。
func TestBoundary_PanicAndBodyLimit(t *testing.T) {
	f := newBoundaryFixture(t)
	tok := f.token(t, "acc-A", aliasA)

	resp := f.get("/panic", "", tok)
	if resp.StatusCode != http.StatusInternalServerError {
		t.Fatalf("panic status = %d, want 500", resp.StatusCode)
	}
	body, err := io.ReadAll(resp.Body)
	if err != nil {
		t.Fatalf("读体: %v", err)
	}
	for _, leak := range []string{"secret-internal-detail", "/srv/", "main.go", "goroutine", "panic"} {
		if strings.Contains(string(body), leak) {
			t.Fatalf("panic 响应泄露 %q: %s", leak, body)
		}
	}
	var out map[string]any
	if jerr := json.Unmarshal(body, &out); jerr != nil || len(out) != 1 || out["error_class"] != middleware.ErrorClassInternal {
		t.Fatalf("panic 响应必须是单字段 internal: %q (%v)", body, jerr)
	}

	// 无令牌 + 超大体量 → 413 而非 401：body-limit 先于 auth 的顺序证据
	big := strings.Repeat("x", 2048)
	req, err := http.NewRequestWithContext(context.Background(), http.MethodPost,
		f.srv.URL+"/sessions", strings.NewReader(big))
	if err != nil {
		t.Fatalf("构造请求: %v", err)
	}
	resp = f.do(req)
	if resp.StatusCode != http.StatusRequestEntityTooLarge {
		t.Fatalf("超大体量 status = %d, want 413", resp.StatusCode)
	}
	if got := decodeErrorClass(t, resp)["error_class"]; got != middleware.ErrorClassPayloadTooLarge {
		t.Fatalf("error_class = %v, want payload_too_large", got)
	}
}
