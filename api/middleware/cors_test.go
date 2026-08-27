package middleware

// CORS 白名单行为测试（T-W5-008 验收 #1）：拒绝 / 放行 / 预检 / Vary /
// 显式凭据模式 / 零配置默认全拒 / 环境变量解析。httptest 直驱无 goroutine，
// 天然兼容 -race。复用 middleware_test.go 的 decodeErrorClass 断言单字段体。

import (
	"net/http"
	"net/http/httptest"
	"slices"
	"strings"
	"testing"
)

func corsApp(t *testing.T, cfg CORSConfig) http.Handler {
	t.Helper()
	next := http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte("ok"))
	})
	return CORS(cfg)(next)
}

func originReq(method, origin string) *http.Request {
	r := httptest.NewRequest(method, "/anything", nil)
	if origin != "" {
		r.Header.Set("Origin", origin)
	}
	return r
}

// TestCORS_ZeroConfigRejectsAll 零值配置（生产默认：白名单空）拒绝任何跨域。
func TestCORS_ZeroConfigRejectsAll(t *testing.T) {
	app := corsApp(t, CORSConfig{})
	rec := httptest.NewRecorder()
	app.ServeHTTP(rec, originReq(http.MethodGet, "https://app.example.com"))
	if rec.Code != http.StatusForbidden {
		t.Fatalf("status = %d, want 403（默认全拒）", rec.Code)
	}
	if got := decodeErrorClass(t, rec)["error_class"]; got != ErrorClassOriginForbidden {
		t.Fatalf("error_class = %v, want %q", got, ErrorClassOriginForbidden)
	}
	if rec.Header().Get("Access-Control-Allow-Origin") != "" {
		t.Fatal("拒绝响应绝不能携带 ACAO 头")
	}
}

// TestCORS_DisallowedOriginRejected 白名单外 Origin → 403，不触达 handler。
func TestCORS_DisallowedOriginRejected(t *testing.T) {
	cfg := CORSConfig{AllowedOrigins: []string{"https://app.example.com"}}
	app := corsApp(t, cfg)
	rec := httptest.NewRecorder()
	app.ServeHTTP(rec, originReq(http.MethodGet, "https://evil.example.com"))
	if rec.Code != http.StatusForbidden {
		t.Fatalf("status = %d, want 403", rec.Code)
	}
	if got := decodeErrorClass(t, rec)["error_class"]; got != ErrorClassOriginForbidden {
		t.Fatalf("error_class = %v", got)
	}
}

// TestCORS_AllowedOriginPasses 白名单命中 → 精确回显 Origin + Vary: Origin。
func TestCORS_AllowedOriginPasses(t *testing.T) {
	cfg := CORSConfig{AllowedOrigins: []string{"https://app.example.com"}}
	app := corsApp(t, cfg)
	rec := httptest.NewRecorder()
	app.ServeHTTP(rec, originReq(http.MethodGet, "https://app.example.com"))
	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200", rec.Code)
	}
	if got := rec.Header().Get("Access-Control-Allow-Origin"); got != "https://app.example.com" {
		t.Fatalf("ACAO = %q, want 精确回显请求 Origin", got)
	}
	if got := rec.Header().Get("Vary"); !slices.Contains(varyTokens(got), "Origin") {
		t.Fatalf("Vary = %q, 必须含 Origin", got)
	}
}

// TestCORS_OriginCaseInsensitive scheme/host 大小写差异不影响白名单判定
// （URL 规范：scheme 与 host 大小写不敏感；端口/路径不存在于 Origin）。
func TestCORS_OriginCaseInsensitive(t *testing.T) {
	cfg := CORSConfig{AllowedOrigins: []string{"https://app.example.com"}}
	app := corsApp(t, cfg)
	rec := httptest.NewRecorder()
	app.ServeHTTP(rec, originReq(http.MethodGet, "HTTPS://APP.EXAMPLE.COM"))
	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200（大小写不敏感）", rec.Code)
	}
}

// TestCORS_NoOriginHeader 同源/非浏览器请求（无 Origin 头）直接放行，
// 不下发任何 CORS 头，但 Vary: Origin 仍然声明（动态回显的缓存正确性）。
func TestCORS_NoOriginHeader(t *testing.T) {
	app := corsApp(t, CORSConfig{AllowedOrigins: []string{"https://app.example.com"}})
	rec := httptest.NewRecorder()
	app.ServeHTTP(rec, originReq(http.MethodGet, ""))
	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200", rec.Code)
	}
	if got := rec.Header().Get("Access-Control-Allow-Origin"); got != "" {
		t.Fatalf("无 Origin 请求不应获得 ACAO，得到 %q", got)
	}
	if got := rec.Header().Get("Vary"); !slices.Contains(varyTokens(got), "Origin") {
		t.Fatalf("Vary = %q, 必须恒含 Origin", got)
	}
}

// TestCORS_PreflightAllowed 预检命中白名单：204 短路 + 固定保守方法/头集。
func TestCORS_PreflightAllowed(t *testing.T) {
	cfg := CORSConfig{AllowedOrigins: []string{"https://app.example.com"}, AllowCredentials: true}
	app := corsApp(t, cfg)
	r := originReq(http.MethodOptions, "https://app.example.com")
	r.Header.Set("Access-Control-Request-Method", http.MethodPost)
	r.Header.Set("Access-Control-Request-Headers", "X-Custom-Header")
	rec := httptest.NewRecorder()
	app.ServeHTTP(rec, r)
	if rec.Code != http.StatusNoContent {
		t.Fatalf("status = %d, want 204", rec.Code)
	}
	h := rec.Header()
	if got := h.Get("Access-Control-Allow-Origin"); got != "https://app.example.com" {
		t.Fatalf("ACAO = %q", got)
	}
	if got := h.Get("Access-Control-Allow-Credentials"); got != "true" {
		t.Fatalf("显式凭据模式必须输出 ACA-Credentials: true，得到 %q", got)
	}
	if got := h.Get("Access-Control-Allow-Methods"); !containsToken(got, http.MethodPost) {
		t.Fatalf("Allow-Methods = %q, 必须含 POST", got)
	}
	if got := h.Get("Access-Control-Allow-Headers"); !containsToken(got, "Authorization") || !containsToken(got, "Content-Type") {
		t.Fatalf("Allow-Headers = %q, 必须含 Authorization 与 Content-Type", got)
	}
	if h.Get("Access-Control-Max-Age") == "" {
		t.Fatal("预检必须带 Max-Age（避免浏览器高频预检）")
	}
}

// TestCORS_PreflightRejected 白名单外的预检同样 403——预检与实际请求同一待遇。
func TestCORS_PreflightRejected(t *testing.T) {
	cfg := CORSConfig{AllowedOrigins: []string{"https://app.example.com"}}
	app := corsApp(t, cfg)
	r := originReq(http.MethodOptions, "https://evil.example.com")
	r.Header.Set("Access-Control-Request-Method", http.MethodPost)
	rec := httptest.NewRecorder()
	app.ServeHTTP(rec, r)
	if rec.Code != http.StatusForbidden {
		t.Fatalf("status = %d, want 403", rec.Code)
	}
	if got := decodeErrorClass(t, rec)["error_class"]; got != ErrorClassOriginForbidden {
		t.Fatalf("error_class = %v", got)
	}
}

// TestCORS_PreflightEmptyBody 预检 204 不得携带响应体。
func TestCORS_PreflightEmptyBody(t *testing.T) {
	cfg := CORSConfig{AllowedOrigins: []string{"https://app.example.com"}}
	app := corsApp(t, cfg)
	r := originReq(http.MethodOptions, "https://app.example.com")
	r.Header.Set("Access-Control-Request-Method", http.MethodGet)
	rec := httptest.NewRecorder()
	app.ServeHTTP(rec, r)
	if rec.Code != http.StatusNoContent || rec.Body.Len() != 0 {
		t.Fatalf("status=%d body=%q, want 204 空体", rec.Code, rec.Body.String())
	}
}

// TestCORS_CredentialsHeaderExplicitOnly 凭据头只随显式开关出现：
// 无凭据配置的放行响应绝不能暗示可携带凭据。
func TestCORS_CredentialsHeaderExplicitOnly(t *testing.T) {
	cfg := CORSConfig{AllowedOrigins: []string{"https://app.example.com"}}
	app := corsApp(t, cfg)
	rec := httptest.NewRecorder()
	app.ServeHTTP(rec, originReq(http.MethodGet, "https://app.example.com"))
	if got := rec.Header().Get("Access-Control-Allow-Credentials"); got != "" {
		t.Fatalf("未配置凭据时 ACA-Credentials 必须缺省，得到 %q", got)
	}
}

// TestCORS_NeverWildcard 白名单放行只精确回显，结构上排除 ACAO:*——
// 带凭据场景的通配符等于向任意站点开放。
func TestCORS_NeverWildcard(t *testing.T) {
	cfg := CORSConfig{AllowedOrigins: []string{"https://app.example.com"}, AllowCredentials: true}
	app := corsApp(t, cfg)
	rec := httptest.NewRecorder()
	app.ServeHTTP(rec, originReq(http.MethodGet, "https://app.example.com"))
	if got := rec.Header().Get("Access-Control-Allow-Origin"); got == "*" {
		t.Fatal("ACAO 绝不允许为 *")
	}
}

// TestCORSConfigFromEnv 环境解析矩阵：空/纯空白/多值/凭据开关各形态。
func TestCORSConfigFromEnv(t *testing.T) {
	get := func(kv map[string]string) func(string) string {
		return func(k string) string { return kv[k] }
	}
	cases := []struct {
		name        string
		env         map[string]string
		wantOrigins []string
		wantCreds   bool
	}{
		{"未配置", map[string]string{}, nil, false},
		{"纯逗号空白", map[string]string{"API_CORS_ALLOWED_ORIGINS": " , ,"}, nil, false},
		{"单值", map[string]string{"API_CORS_ALLOWED_ORIGINS": "https://a.example.com"}, []string{"https://a.example.com"}, false},
		{"多值含空白", map[string]string{"API_CORS_ALLOWED_ORIGINS": " https://a.example.com , https://b.example.com "}, []string{"https://a.example.com", "https://b.example.com"}, false},
		{"凭据1", map[string]string{"API_CORS_ALLOW_CREDENTIALS": "1"}, nil, true},
		{"凭据TRUE", map[string]string{"API_CORS_ALLOW_CREDENTIALS": "TRUE"}, nil, true},
		{"凭据关", map[string]string{"API_CORS_ALLOW_CREDENTIALS": "0"}, nil, false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			cfg := CORSConfigFromEnv(get(tc.env))
			if !slices.Equal(cfg.AllowedOrigins, tc.wantOrigins) {
				t.Fatalf("origins = %v, want %v", cfg.AllowedOrigins, tc.wantOrigins)
			}
			if cfg.AllowCredentials != tc.wantCreds {
				t.Fatalf("credentials = %v, want %v", cfg.AllowCredentials, tc.wantCreds)
			}
		})
	}
}

// varyTokens 把 Vary/Allow-* 头的逗号分隔值拆成 token 集合（标准库解析）。
func varyTokens(v string) []string {
	if strings.TrimSpace(v) == "" {
		return nil
	}
	return strings.Split(v, ",")
}

// containsToken 判断逗号分隔头字段值是否含指定 token（大小写不敏感）。
func containsToken(header, token string) bool {
	for _, tok := range varyTokens(header) {
		if strings.EqualFold(strings.TrimSpace(tok), token) {
			return true
		}
	}
	return false
}
