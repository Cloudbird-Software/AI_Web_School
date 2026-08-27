package middleware

// 统一错误映射与边界防护测试（T-W5-008 验收 #3/#4）：映射矩阵、未知异常
// 不泄露内部信息（栈/SQL/文件路径）、panic 收敛、请求体上限。

import (
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/Cloudbird-Software/AI_Web_School/core/auth"
	"github.com/Cloudbird-Software/AI_Web_School/core/compliance"
)

// TestMapError_Matrix 错误 → (状态码, error_class) 映射矩阵（单点扩展处，
// handler 不得各自再写映射）。
func TestMapError_Matrix(t *testing.T) {
	cases := []struct {
		name      string
		err       error
		wantCode  int
		wantClass string
	}{
		{"缺令牌", auth.ErrNoToken, http.StatusUnauthorized, ErrorClassUnauthorized},
		{"签名错", auth.ErrBadSignature, http.StatusUnauthorized, ErrorClassUnauthorized},
		{"过期", auth.ErrExpiredToken, http.StatusUnauthorized, ErrorClassUnauthorized},
		{"包装的认证错", fmt.Errorf("verify: %w", auth.ErrExpiredToken), http.StatusUnauthorized, ErrorClassUnauthorized},
		{"角色不足", auth.ErrRoleDenied, http.StatusForbidden, ErrorClassForbidden},
		{"越权alias", auth.ErrAliasNotOwned, http.StatusForbidden, ErrorClassForbidden},
		{"主体模型非法", auth.ErrInvalidSubject, http.StatusForbidden, ErrorClassForbidden},
		// T-W5-010：授权缺失三态经统一哨兵进矩阵；对外一律粗粒度 forbidden
		//（细分只进服务端审计日志），任意 State 载体不得映射到别的类；
		// 授权账基础设施故障（非授权语义）保持 500 internal——fail-closed
		// 但不向运维伪装成客户端越权。
		{"授权缺失missing", &compliance.ConsentRequiredError{StudentAliasID: "a", Purpose: compliance.PurposeOnlinePractice, State: compliance.StateMissing}, http.StatusForbidden, ErrorClassForbidden},
		{"授权缺失revoked", &compliance.ConsentRequiredError{StudentAliasID: "a", Purpose: compliance.PurposeOnlinePractice, State: compliance.StateRevoked}, http.StatusForbidden, ErrorClassForbidden},
		{"授权缺失expired", &compliance.ConsentRequiredError{StudentAliasID: "a", Purpose: compliance.PurposeOnlinePractice, State: compliance.StateExpired}, http.StatusForbidden, ErrorClassForbidden},
		{"授权哨兵直传", compliance.ErrConsentRequired, http.StatusForbidden, ErrorClassForbidden},
		{"授权账故障按内部错", fmt.Errorf("session: 授权账读取失败（fail-closed，不放行）: %w", errors.New("db down")), http.StatusInternalServerError, ErrorClassInternal},
		{"请求体超限", &http.MaxBytesError{Limit: 1024}, http.StatusRequestEntityTooLarge, ErrorClassPayloadTooLarge},
		{"未知错误", errors.New("boom"), http.StatusInternalServerError, ErrorClassInternal},
		{"nil按内部错", nil, http.StatusInternalServerError, ErrorClassInternal},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			code, class := MapError(tc.err)
			if code != tc.wantCode || class != tc.wantClass {
				t.Fatalf("MapError = (%d, %q), want (%d, %q)", code, class, tc.wantCode, tc.wantClass)
			}
		})
	}
}

// TestHandleError_SanitizedBody 未知异常（带 SQL/路径/密钥样文本）经统一
// 出口后，响应体只剩 error_class 单字段——验收 #4 "未知异常不泄露内部信息"。
func TestHandleError_SanitizedBody(t *testing.T) {
	rec := httptest.NewRecorder()
	HandleError(rec, errors.New(`pq: relation "users" does not exist at /srv/app/db/main.go:88 key=sk-1234567890abcdef`))
	if rec.Code != http.StatusInternalServerError {
		t.Fatalf("status = %d, want 500", rec.Code)
	}
	body := rec.Body.String()
	for _, leak := range []string{"pq:", "users", "/srv/", "main.go:88", "sk-1234567890abcdef", "goroutine"} {
		if strings.Contains(body, leak) {
			t.Fatalf("响应体泄露内部信息 %q: %s", leak, body)
		}
	}
	if got := decodeErrorClass(t, rec)["error_class"]; got != ErrorClassInternal {
		t.Fatalf("error_class = %v, want %q", got, ErrorClassInternal)
	}
}

// TestRecover_PanicSanitized panic 收敛：500 + 单字段体，panic 值（含
// 连接串样文本）与栈绝不进响应；不打断测试进程。
func TestRecover_PanicSanitized(t *testing.T) {
	bomb := http.HandlerFunc(func(_ http.ResponseWriter, _ *http.Request) {
		panic(errors.New("postgres://root:hunter2@db.internal:5432/school sslmode=disable"))
	})
	app := Recover(bomb)

	rec := httptest.NewRecorder()
	app.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/boom", nil))
	if rec.Code != http.StatusInternalServerError {
		t.Fatalf("status = %d, want 500", rec.Code)
	}
	body := rec.Body.String()
	for _, leak := range []string{"postgres", "hunter2", "db.internal", "goroutine", "panic"} {
		if strings.Contains(body, leak) {
			t.Fatalf("panic 响应泄露内部信息 %q: %s", leak, body)
		}
	}
	if got := decodeErrorClass(t, rec)["error_class"]; got != ErrorClassInternal {
		t.Fatalf("error_class = %v, want %q", got, ErrorClassInternal)
	}
}

// TestRecover_PanicStringValues 非 error 的 panic 值同样收敛。
func TestRecover_PanicStringValues(t *testing.T) {
	app := Recover(http.HandlerFunc(func(_ http.ResponseWriter, _ *http.Request) {
		panic("配置对象未初始化")
	}))
	rec := httptest.NewRecorder()
	app.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/boom", nil))
	if rec.Code != http.StatusInternalServerError {
		t.Fatalf("status = %d, want 500", rec.Code)
	}
	decodeErrorClass(t, rec)
}

// TestRecover_PanicAfterHeader panic 发生在响应头已写出之后：状态码保持
// 首写值（200 已发出无法更改），进程不得因二次 WriteHeader 崩溃。
func TestRecover_PanicAfterHeader(t *testing.T) {
	app := Recover(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte("partial"))
		panic(errors.New("流式写出中途崩溃"))
	}))
	rec := httptest.NewRecorder()
	app.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/stream", nil))
	if rec.Code != http.StatusOK {
		t.Fatalf("已写出的状态码应保持，得到 %d", rec.Code)
	}
}

// TestRecover_NoPanicPassthrough 无 panic 时零干扰直通（含写头与写体）。
func TestRecover_NoPanicPassthrough(t *testing.T) {
	app := Recover(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("X-Trace", "t1")
		w.WriteHeader(http.StatusTeapot)
		_, _ = w.Write([]byte("brewing"))
	}))
	rec := httptest.NewRecorder()
	app.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/ok", nil))
	if rec.Code != http.StatusTeapot || rec.Body.String() != "brewing" || rec.Header().Get("X-Trace") != "t1" {
		t.Fatalf("直通被破坏: code=%d body=%q trace=%q", rec.Code, rec.Body.String(), rec.Header().Get("X-Trace"))
	}
}

// TestLimitBody_UnderLimit 上限内请求体原样透传给 handler。
func TestLimitBody_UnderLimit(t *testing.T) {
	var got []byte
	app := LimitBody(64)(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		b, err := io.ReadAll(r.Body)
		if err != nil {
			t.Errorf("读体失败: %v", err)
		}
		got = b
		w.WriteHeader(http.StatusOK)
	}))
	r := httptest.NewRequest(http.MethodPost, "/submit", strings.NewReader(`{"answer":"A"}`))
	rec := httptest.NewRecorder()
	app.ServeHTTP(rec, r)
	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200", rec.Code)
	}
	if string(got) != `{"answer":"A"}` {
		t.Fatalf("请求体被破坏: %q", got)
	}
}

// TestLimitBody_OverLimit 超限 → 413 payload_too_large 单字段体，
// handler 不被触达。
func TestLimitBody_OverLimit(t *testing.T) {
	reached := false
	app := LimitBody(16)(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		reached = true
	}))
	r := httptest.NewRequest(http.MethodPost, "/submit", strings.NewReader(strings.Repeat("x", 64)))
	rec := httptest.NewRecorder()
	app.ServeHTTP(rec, r)
	if rec.Code != http.StatusRequestEntityTooLarge {
		t.Fatalf("status = %d, want 413", rec.Code)
	}
	if reached {
		t.Fatal("超限请求绝不能触达 handler")
	}
	if got := decodeErrorClass(t, rec)["error_class"]; got != ErrorClassPayloadTooLarge {
		t.Fatalf("error_class = %v, want %q", got, ErrorClassPayloadTooLarge)
	}
}

// TestLimitBody_NoBodyGet 无体 GET 零读取直通。
func TestLimitBody_NoBodyGet(t *testing.T) {
	app := LimitBody(16)(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
	}))
	rec := httptest.NewRecorder()
	app.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/health", nil))
	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200", rec.Code)
	}
}

// TestMaxBodyBytesFromEnv 体限环境解析：未配置/非法回落默认。
func TestMaxBodyBytesFromEnv(t *testing.T) {
	if got := MaxBodyBytesFromEnv(func(string) string { return "" }); got != DefaultMaxBodyBytes {
		t.Fatalf("未配置 = %d, want 默认 %d", got, DefaultMaxBodyBytes)
	}
	if got := MaxBodyBytesFromEnv(func(k string) string {
		if k == "API_MAX_BODY_BYTES" {
			return "not-a-number"
		}
		return ""
	}); got != DefaultMaxBodyBytes {
		t.Fatalf("非法值 = %d, want 默认", got)
	}
	if got := MaxBodyBytesFromEnv(func(k string) string {
		if k == "API_MAX_BODY_BYTES" {
			return "2048"
		}
		return ""
	}); got != 2048 {
		t.Fatalf("合法覆盖 = %d, want 2048", got)
	}
}
