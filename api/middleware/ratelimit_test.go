package middleware

// 令牌桶限流测试（T-W5-008 验收 #2/#5）：窗口耗尽 429 与恢复、双维度
// （IP / 主体）键与配额隔离、作答提交与报告查询独立配额、健康探针豁免、
// 环境解析。时钟全部注入推进——零 sleep，-race 稳定。

import (
	"context"
	"net/http"
	"net/http/httptest"
	"strconv"
	"sync"
	"testing"
	"time"

	"github.com/Cloudbird-Software/AI_Web_School/core/auth"
)

// fakeClock 是互斥保护的可推进时钟：并发测试下 -race 干净。
type fakeClock struct {
	mu  sync.Mutex
	now time.Time
}

func newFakeClock() *fakeClock { return &fakeClock{now: time.Unix(1_700_000_000, 0).UTC()} }

func (c *fakeClock) Now() time.Time {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.now
}

func (c *fakeClock) Advance(d time.Duration) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.now = c.now.Add(d)
}

// TestRateLimiter_ExhaustAndRecover 窗口耗尽 → 429 语义（含 Retry-After
// 时长），推进假时钟跨过补充窗口后恢复放行（验收 #2 恢复路径）。
func TestRateLimiter_ExhaustAndRecover(t *testing.T) {
	clk := newFakeClock()
	l := NewRateLimiter(RateLimitConfig{
		IPScopes: map[RateScope]RateQuota{ScopeSubmit: {PerMinute: 2, Burst: 2}}, // 30s 补 1 枚
	}, clk.Now)

	for i := 0; i < 2; i++ {
		ok, retry := l.Allow(DimensionIP, ScopeSubmit, "192.0.2.1")
		if !ok {
			t.Fatalf("第 %d 次应放行（burst=2）", i+1)
		}
		if retry != 0 {
			t.Fatalf("放行时 retryAfter 必须为 0，得到 %v", retry)
		}
	}
	ok, retry := l.Allow(DimensionIP, ScopeSubmit, "192.0.2.1")
	if ok {
		t.Fatal("超出 burst 应拒绝")
	}
	// (1-0)/(2/60) = 30s → 向上取整恰 30s
	if retry != 30*time.Second {
		t.Fatalf("retryAfter = %v, want 30s", retry)
	}

	clk.Advance(29 * time.Second)
	if ok, _ := l.Allow(DimensionIP, ScopeSubmit, "192.0.2.1"); ok {
		t.Fatal("29s 时令牌未补足 1 枚，应仍拒绝")
	}
	clk.Advance(2 * time.Second) // 累计 31s → 补 1.03 枚
	if ok, _ := l.Allow(DimensionIP, ScopeSubmit, "192.0.2.1"); !ok {
		t.Fatal("31s 后应恢复放行")
	}
}

// TestRateLimiter_ScopeQuotasIndependent 作答提交与报告查询独立配额：
// 一个域打空不侵蚀另一域（验收 #2）。
func TestRateLimiter_ScopeQuotasIndependent(t *testing.T) {
	clk := newFakeClock()
	l := NewRateLimiter(RateLimitConfig{
		IPScopes: map[RateScope]RateQuota{
			ScopeSubmit: {PerMinute: 1, Burst: 1},
			ScopeReport: {PerMinute: 5, Burst: 5},
		},
	}, clk.Now)

	if ok, _ := l.Allow(DimensionIP, ScopeSubmit, "192.0.2.1"); !ok {
		t.Fatal("submit 首次应放行")
	}
	if ok, _ := l.Allow(DimensionIP, ScopeSubmit, "192.0.2.1"); ok {
		t.Fatal("submit 第二次应拒绝")
	}
	for i := 0; i < 5; i++ {
		if ok, _ := l.Allow(DimensionIP, ScopeReport, "192.0.2.1"); !ok {
			t.Fatalf("report 第 %d 次应放行——submit 耗尽不得波及 report", i+1)
		}
	}
}

// TestRateLimiter_DimensionIsolation 维度隔离：IP 维打空不影响主体维
// （分表 + 键空间隔离）。
func TestRateLimiter_DimensionIsolation(t *testing.T) {
	clk := newFakeClock()
	l := NewRateLimiter(RateLimitConfig{
		IPScopes:        map[RateScope]RateQuota{ScopeDefault: {PerMinute: 1, Burst: 1}},
		PrincipalScopes: map[RateScope]RateQuota{ScopeDefault: {PerMinute: 1, Burst: 1}},
	}, clk.Now)

	if ok, _ := l.Allow(DimensionIP, ScopeDefault, "192.0.2.1"); !ok {
		t.Fatal("IP 维首次应放行")
	}
	if ok, _ := l.Allow(DimensionIP, ScopeDefault, "192.0.2.1"); ok {
		t.Fatal("IP 维第二次应拒绝")
	}
	if ok, _ := l.Allow(DimensionPrincipal, ScopeDefault, "student\x00acc-1"); !ok {
		t.Fatal("主体维与 IP 维分表，不得受 IP 维耗尽影响")
	}
	if ok, _ := l.Allow(DimensionIP, ScopeDefault, "192.0.2.2"); !ok {
		t.Fatal("其他 IP 不受影响")
	}
}

// TestRateLimiter_DisabledScope 显式 PerMinute<=0 = 该域不限流。
func TestRateLimiter_DisabledScope(t *testing.T) {
	clk := newFakeClock()
	l := NewRateLimiter(RateLimitConfig{
		IPScopes: map[RateScope]RateQuota{ScopeDefault: {PerMinute: 0, Burst: 1}},
	}, clk.Now)
	for i := 0; i < 100; i++ {
		if ok, _ := l.Allow(DimensionIP, ScopeDefault, "192.0.2.1"); !ok {
			t.Fatalf("第 %d 次不应拒绝（域已显式关闭）", i+1)
		}
	}
}

// TestRateLimiter_UndefinedScopePasses 未定义的域不限流（显式豁免语义）。
func TestRateLimiter_UndefinedScopePasses(t *testing.T) {
	clk := newFakeClock()
	l := NewRateLimiter(RateLimitConfig{
		IPScopes: map[RateScope]RateQuota{ScopeSubmit: {PerMinute: 1, Burst: 1}},
	}, clk.Now)
	if ok, _ := l.Allow(DimensionIP, ScopeReport, "192.0.2.1"); !ok {
		t.Fatal("未定义域应放行")
	}
}

// TestRateLimitIP_Middleware IP 层中间件：超限 → 429 + Retry-After 头 +
// rate_limited 单字段体；ScopeNone（健康探针）豁免。
func TestRateLimitIP_Middleware(t *testing.T) {
	clk := newFakeClock()
	l := NewRateLimiter(RateLimitConfig{
		IPScopes: map[RateScope]RateQuota{
			ScopeSubmit: {PerMinute: 1, Burst: 1},
		},
	}, clk.Now)
	next := http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
	})
	classify := func(r *http.Request) RateScope {
		if r.URL.Path == "/healthz" {
			return ScopeNone
		}
		return ScopeSubmit
	}
	app := RateLimitIP(l, classify)(next)

	for i := 0; i < 5; i++ {
		rec := httptest.NewRecorder()
		app.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/healthz", nil))
		if rec.Code != http.StatusOK {
			t.Fatalf("健康探针第 %d 次被限流：%d（验收 #5）", i+1, rec.Code)
		}
	}

	rec := httptest.NewRecorder()
	app.ServeHTTP(rec, httptest.NewRequest(http.MethodPost, "/sessions", nil))
	if rec.Code != http.StatusOK {
		t.Fatalf("首次放行，status = %d", rec.Code)
	}
	rec = httptest.NewRecorder()
	app.ServeHTTP(rec, httptest.NewRequest(http.MethodPost, "/sessions", nil))
	if rec.Code != http.StatusTooManyRequests {
		t.Fatalf("status = %d, want 429", rec.Code)
	}
	secs, err := strconv.Atoi(rec.Header().Get("Retry-After"))
	if err != nil || secs < 1 {
		t.Fatalf("Retry-After = %q, 必须是 >=1 的秒数（重试提示）", rec.Header().Get("Retry-After"))
	}
	if got := decodeErrorClass(t, rec)["error_class"]; got != ErrorClassRateLimited {
		t.Fatalf("error_class = %v, want %q", got, ErrorClassRateLimited)
	}
}

// TestRateLimitPrincipal_Middleware 主体层：按主体键计费；主体缺失按
// 装配破坏 fail-closed 401。
func TestRateLimitPrincipal_Middleware(t *testing.T) {
	clk := newFakeClock()
	l := NewRateLimiter(RateLimitConfig{
		PrincipalScopes: map[RateScope]RateQuota{ScopeSubmit: {PerMinute: 1, Burst: 1}},
	}, clk.Now)

	var nextGot bool
	next := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		nextGot = true
		w.WriteHeader(http.StatusOK)
	})
	app := RateLimitPrincipal(l, ScopeSubmit)(next)

	// 主体缺失（装配破坏）→ 401 而非放行
	rec := httptest.NewRecorder()
	app.ServeHTTP(rec, httptest.NewRequest(http.MethodPost, "/sessions", nil))
	if rec.Code != http.StatusUnauthorized {
		t.Fatalf("主体缺失 status = %d, want 401（fail-closed）", rec.Code)
	}

	putPrincipal := func(r *http.Request, subject string) *http.Request {
		p := auth.Principal{Role: auth.RoleStudent, SubjectID: subject, AliasID: subject}
		return r.WithContext(context.WithValue(r.Context(), ctxKey{}, p))
	}
	rec = httptest.NewRecorder()
	app.ServeHTTP(rec, putPrincipal(httptest.NewRequest(http.MethodPost, "/sessions", nil), "acc-1"))
	if rec.Code != http.StatusOK || !nextGot {
		t.Fatalf("首个请求应放行: code=%d reached=%v", rec.Code, nextGot)
	}
	rec = httptest.NewRecorder()
	app.ServeHTTP(rec, putPrincipal(httptest.NewRequest(http.MethodPost, "/sessions", nil), "acc-1"))
	if rec.Code != http.StatusTooManyRequests {
		t.Fatalf("同主体第二次 status = %d, want 429", rec.Code)
	}
	// 另一主体不受影响（共享 NAT/IP 时配额仍按人计费——双维度设计的意义）
	rec = httptest.NewRecorder()
	app.ServeHTTP(rec, putPrincipal(httptest.NewRequest(http.MethodPost, "/sessions", nil), "acc-2"))
	if rec.Code != http.StatusOK {
		t.Fatalf("其他主体 status = %d, want 200", rec.Code)
	}
}

// TestRateLimitConfigFromEnv 环境解析矩阵：非法值/负值回落默认；合法 RPM
// 同时覆盖两维；突发容量保持各维默认（IP 维按 NAT 聚合放大）。
func TestRateLimitConfigFromEnv(t *testing.T) {
	cases := []struct {
		name    string
		env     map[string]string
		wantRPM map[Dimension]map[RateScope]int
	}{
		{
			"未配置走默认",
			map[string]string{},
			map[Dimension]map[RateScope]int{
				DimensionIP:        {ScopeSubmit: 120, ScopeReport: 240, ScopeDefault: 480},
				DimensionPrincipal: {ScopeSubmit: 30, ScopeReport: 60, ScopeDefault: 120},
			},
		},
		{
			"覆盖submit（两维同值）",
			map[string]string{"API_RATE_LIMIT_SUBMIT_RPM": "7"},
			map[Dimension]map[RateScope]int{
				DimensionIP:        {ScopeSubmit: 7, ScopeReport: 240, ScopeDefault: 480},
				DimensionPrincipal: {ScopeSubmit: 7, ScopeReport: 60, ScopeDefault: 120},
			},
		},
		{
			"非法值回落",
			map[string]string{"API_RATE_LIMIT_REPORT_RPM": "abc"},
			map[Dimension]map[RateScope]int{
				DimensionIP:        {ScopeSubmit: 120, ScopeReport: 240, ScopeDefault: 480},
				DimensionPrincipal: {ScopeSubmit: 30, ScopeReport: 60, ScopeDefault: 120},
			},
		},
		{
			"负值回落",
			map[string]string{"API_RATE_LIMIT_DEFAULT_RPM": "-5"},
			map[Dimension]map[RateScope]int{
				DimensionIP:        {ScopeSubmit: 120, ScopeReport: 240, ScopeDefault: 480},
				DimensionPrincipal: {ScopeSubmit: 30, ScopeReport: 60, ScopeDefault: 120},
			},
		},
		{
			"零=显式关闭",
			map[string]string{"API_RATE_LIMIT_DEFAULT_RPM": "0"},
			map[Dimension]map[RateScope]int{
				DimensionIP:        {ScopeSubmit: 120, ScopeReport: 240, ScopeDefault: 0},
				DimensionPrincipal: {ScopeSubmit: 30, ScopeReport: 60, ScopeDefault: 0},
			},
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			cfg := RateLimitConfigFromEnv(func(k string) string { return tc.env[k] })
			for dim, wants := range tc.wantRPM {
				quotas := cfg.quotasOf(dim)
				for scope, want := range wants {
					if got := quotas[scope].PerMinute; got != want {
						t.Errorf("%s %s RPM = %d, want %d", dim, scope, got, want)
					}
				}
			}
			// 突发容量不被环境触碰（两维默认值不同：IP 维按 NAT 聚合放大）
			if cfg.IPScopes[ScopeSubmit].Burst == cfg.PrincipalScopes[ScopeSubmit].Burst {
				t.Fatal("两维 submit 突发容量不应相同——IP 维必须按 NAT 聚合放大")
			}
		})
	}
}

// TestDefaultRateLimitConfig IP 维配额必须宽于主体维（NAT 聚合语义）。
func TestDefaultRateLimitConfig(t *testing.T) {
	cfg := DefaultRateLimitConfig()
	for _, scope := range []RateScope{ScopeSubmit, ScopeReport, ScopeDefault} {
		ip := cfg.IPScopes[scope]
		pr := cfg.PrincipalScopes[scope]
		if ip.PerMinute <= pr.PerMinute || ip.Burst < pr.Burst {
			t.Fatalf("%s: IP 配额 (%d/%d) 必须宽于主体配额 (%d/%d)",
				scope, ip.PerMinute, ip.Burst, pr.PerMinute, pr.Burst)
		}
	}
}

// TestRateLimiter_ConcurrentUnlimitedQuota 并发打点：配额充足时全部放行
// 且 -race 零告警（桶状态的互斥保护）。真实时钟，无注入推进。
func TestRateLimiter_ConcurrentUnlimitedQuota(t *testing.T) {
	l := NewRateLimiter(RateLimitConfig{
		IPScopes: map[RateScope]RateQuota{ScopeDefault: {PerMinute: 60000, Burst: 2000}},
	}, nil)

	const workers, per = 50, 20
	var wg sync.WaitGroup
	denied := make(chan struct{}, workers*per)
	for i := 0; i < workers; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for j := 0; j < per; j++ {
				if ok, _ := l.Allow(DimensionIP, ScopeDefault, "192.0.2.9"); !ok {
					denied <- struct{}{}
				}
			}
		}()
	}
	wg.Wait()
	close(denied)
	if n := len(denied); n != 0 {
		t.Fatalf("burst 覆盖全部请求时应零拒绝，被拒 %d 次", n)
	}
}
