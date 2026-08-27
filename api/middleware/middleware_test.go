package middleware

// RequireAuth 的 HTTP 行为测试（T-W5-005 验收 #2/#5）：五类拒绝路径
// （无令牌 / 过期 / 签名错 / 角色不足 / 越权 alias）+ 正常路径。
// 用 httptest.ResponseRecorder 直驱 handler：不拉起 goroutine，天然
// 兼容 -race；令牌过期由注入时钟推进，零 sleep。

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/Cloudbird-Software/AI_Web_School/core/auth"
)

const mwTestKeyMaterial = "middleware-test-secret-0123456789-t-w5-005"

var (
	mwAliasMine   = "11111111-2222-4333-8444-555555555555"
	mwAliasOthers = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
)

type harness struct {
	signer  *auth.Signer
	advance func(time.Duration)
}

func newHarness(t *testing.T) *harness {
	t.Helper()
	now := time.Unix(1_700_000_000, 0).UTC()
	signer, err := auth.NewSignerWithClock([]byte(mwTestKeyMaterial), func() time.Time { return now })
	if err != nil {
		t.Fatalf("构造 Signer: %v", err)
	}
	return &harness{signer: signer, advance: func(d time.Duration) { now = now.Add(d) }}
}

// studentApp 构造受保护业务的最小样本：GET /report/{alias} 要求已认证，
// 且 handler 调 AssertOwnsAlias 强制学生只读自己的 alias（T-W5-006 将
// 全端点复刻该模式）。
func (h *harness) studentApp(t *testing.T) http.Handler {
	t.Helper()
	mux := http.NewServeMux()
	mux.HandleFunc("GET /report/{alias}", func(w http.ResponseWriter, r *http.Request) {
		p, ok := FromContext(r.Context())
		if !ok {
			t.Error("受保护路由必须能取到主体")
			writeError(w, http.StatusInternalServerError, "internal")
			return
		}
		if err := auth.AssertOwnsAlias(p, r.PathValue("alias")); err != nil {
			WriteAuthErrorResponse(w, err)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusOK)
		_ = json.NewEncoder(w).Encode(map[string]string{
			"subject": p.SubjectID, "alias": p.AliasID,
		})
	})
	return RequireAuth(h.signer, auth.RoleStudent)(mux)
}

func (h *harness) token(t *testing.T, p auth.Principal, ttl time.Duration) string {
	t.Helper()
	tok, err := h.signer.Issue(p, ttl)
	if err != nil {
		t.Fatalf("签发令牌: %v", err)
	}
	return tok
}

func bearerReq(token string) *http.Request {
	r := httptest.NewRequest(http.MethodGet, "/report/"+mwAliasMine, nil)
	r.Header.Set("Authorization", "Bearer "+token)
	return r
}

// decodeOneField 断言响应体是单字段 error_class JSON——字段最小化是
// 验收 #2"不泄露内部实现细节"的可执行形态。
func decodeErrorClass(t *testing.T, rec *httptest.ResponseRecorder) map[string]any {
	t.Helper()
	var body map[string]any
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
		t.Fatalf("响应必须是 JSON: %v（body=%q）", err, rec.Body.String())
	}
	if len(body) != 1 {
		t.Fatalf("错误响应字段必须最小化（仅 error_class），得到 %v", body)
	}
	if body["error_class"] == "" {
		t.Fatalf("error_class 不得为空: %v", body)
	}
	return body
}

func TestRequireAuth_NoToken(t *testing.T) {
	app := newHarness(t).studentApp(t)
	rec := httptest.NewRecorder()
	app.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/report/"+mwAliasMine, nil))
	if rec.Code != http.StatusUnauthorized {
		t.Fatalf("status = %d, want 401", rec.Code)
	}
	if v := rec.Header().Get("WWW-Authenticate"); !strings.HasPrefix(v, "Bearer") {
		t.Fatalf("401 必须携带 WWW-Authenticate 挑战头，得到 %q", v)
	}
	body := decodeErrorClass(t, rec)
	if body["error_class"] != ErrorClassUnauthorized {
		t.Fatalf("error_class = %v, want %q", body["error_class"], ErrorClassUnauthorized)
	}
}

// TestRequireAuth_WrongScheme 非 Bearer scheme 一律按无凭证处理：
// 不为探测者提供第二种可区分的失败反馈。
func TestRequireAuth_WrongScheme(t *testing.T) {
	app := newHarness(t).studentApp(t)
	r := httptest.NewRequest(http.MethodGet, "/report/"+mwAliasMine, nil)
	r.Header.Set("Authorization", "Basic dXNlcjpwYXNz")
	rec := httptest.NewRecorder()
	app.ServeHTTP(rec, r)
	if rec.Code != http.StatusUnauthorized {
		t.Fatalf("status = %d, want 401", rec.Code)
	}
	if got := decodeErrorClass(t, rec)["error_class"]; got != ErrorClassUnauthorized {
		t.Fatalf("error_class = %v", got)
	}
}

func TestRequireAuth_ExpiredToken(t *testing.T) {
	h := newHarness(t)
	app := h.studentApp(t)
	token := h.token(t, auth.Principal{Role: auth.RoleStudent, SubjectID: "acc-1", AliasID: mwAliasMine}, time.Minute)
	h.advance(time.Minute + time.Second)

	rec := httptest.NewRecorder()
	app.ServeHTTP(rec, bearerReq(token))
	if rec.Code != http.StatusUnauthorized {
		t.Fatalf("status = %d, want 401", rec.Code)
	}
	if got := decodeErrorClass(t, rec)["error_class"]; got != ErrorClassUnauthorized {
		t.Fatalf("过期令牌对外也应显示 unauthorized（不区分内部原因）: %v", got)
	}
}

func TestRequireAuth_BadSignature(t *testing.T) {
	h := newHarness(t)
	app := h.studentApp(t)
	token := h.token(t, auth.Principal{Role: auth.RoleStudent, SubjectID: "acc-1", AliasID: mwAliasMine}, time.Minute)
	tampered := flipLastChar(token)

	rec := httptest.NewRecorder()
	app.ServeHTTP(rec, bearerReq(tampered))
	if rec.Code != http.StatusUnauthorized {
		t.Fatalf("status = %d, want 401", rec.Code)
	}
	body := rec.Body.String()
	for _, leak := range []string{"hmac", "HMAC", "sha256", "secret"} {
		if strings.Contains(body, leak) {
			t.Fatalf("错误响应泄露内部机制 %q: %s", leak, body)
		}
	}
	if got := decodeErrorClass(t, rec)["error_class"]; got != ErrorClassUnauthorized {
		t.Fatalf("error_class = %v", got)
	}
}

// flipLastChar 把串的最后一个字符换成另一个合法 base64url 字符：
// 保证被篡改令牌仍能过结构解析、恰好落在签名比对阶段被拒。
func flipLastChar(s string) string {
	const alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
	out := []byte(s)
	last := out[len(out)-1]
	replacement := byte(alphabet[0])
	if last == replacement {
		replacement = alphabet[1]
	}
	out[len(out)-1] = replacement
	return string(out)
}

// TestRequireAuth_InsufficientRole 已认证但角色不足 → 403 forbidden。
func TestRequireAuth_InsufficientRole(t *testing.T) {
	h := newHarness(t)
	app := h.studentApp(t)
	token := h.token(t, auth.Principal{Role: auth.RoleStaff, SubjectID: "staff-7"}, time.Hour)

	rec := httptest.NewRecorder()
	app.ServeHTTP(rec, bearerReq(token))
	if rec.Code != http.StatusForbidden {
		t.Fatalf("status = %d, want 403", rec.Code)
	}
	if got := decodeErrorClass(t, rec)["error_class"]; got != ErrorClassForbidden {
		t.Fatalf("error_class = %v, want %q", got, ErrorClassForbidden)
	}
}

// TestRequireAuth_AlienAlias 学生持自己合法令牌访问他人 alias：
// D9 的核心拒绝路径，403 + forbidden。
func TestRequireAuth_AlienAlias(t *testing.T) {
	h := newHarness(t)
	app := h.studentApp(t)
	token := h.token(t, auth.Principal{Role: auth.RoleStudent, SubjectID: "acc-2", AliasID: mwAliasOthers}, time.Minute)

	r := httptest.NewRequest(http.MethodGet, "/report/"+mwAliasMine, nil)
	r.Header.Set("Authorization", "Bearer "+token)
	rec := httptest.NewRecorder()
	app.ServeHTTP(rec, r)
	if rec.Code != http.StatusForbidden {
		t.Fatalf("status = %d, want 403", rec.Code)
	}
	if got := decodeErrorClass(t, rec)["error_class"]; got != ErrorClassForbidden {
		t.Fatalf("error_class = %v, want %q", got, ErrorClassForbidden)
	}
}

func TestRequireAuth_HappyPath(t *testing.T) {
	h := newHarness(t)
	app := h.studentApp(t)
	p := auth.Principal{Role: auth.RoleStudent, SubjectID: "acc-1", AliasID: mwAliasMine}
	token := h.token(t, p, time.Minute)

	rec := httptest.NewRecorder()
	app.ServeHTTP(rec, bearerReq(token))
	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200（body=%s）", rec.Code, rec.Body.String())
	}
	var echo struct {
		Subject string `json:"subject"`
		Alias   string `json:"alias"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &echo); err != nil {
		t.Fatalf("正常路径必须是 JSON: %v", err)
	}
	if echo.Subject != "acc-1" || echo.Alias != mwAliasMine {
		t.Fatalf("注入 context 的主体不完整: %+v", echo)
	}
}

// TestRequireAuth_AllowAnyAuthenticated roles 为空时接受任意已认证主体，
// 但匿名请求仍被拒——D9 不存在匿名端点。
func TestRequireAuth_AllowAnyAuthenticated(t *testing.T) {
	h := newHarness(t)
	var gotPrincipal bool
	next := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, ok := FromContext(r.Context())
		gotPrincipal = ok
		w.WriteHeader(http.StatusOK)
	})
	app := RequireAuth(h.signer)(next)

	rec := httptest.NewRecorder()
	app.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/any", nil))
	if rec.Code != http.StatusUnauthorized {
		t.Fatalf("匿名 status = %d, want 401", rec.Code)
	}

	token := h.token(t, auth.Principal{Role: auth.RoleOps, SubjectID: "ops-1"}, time.Minute)
	rec = httptest.NewRecorder()
	app.ServeHTTP(rec, bearerReq(token))
	if rec.Code != http.StatusOK || !gotPrincipal {
		t.Fatalf("ops 主体应放行: code=%d principal=%v", rec.Code, gotPrincipal)
	}
}

func TestFromContext(t *testing.T) {
	if _, ok := FromContext(context.Background()); ok {
		t.Fatal("空 context 不应返回主体")
	}
	want := auth.Principal{Role: auth.RoleService, SubjectID: "job"}
	ctx := context.WithValue(context.Background(), ctxKey{}, want)
	got, ok := FromContext(ctx)
	if !ok || got != want {
		t.Fatalf("got (%+v,%v), want %+v", got, ok, want)
	}
}

// TestWriteAuthErrorResponse 映射契约：授权类→403，其余默认 fail-closed
// 落 401（未知错误绝不能被当成"已认证放行"）。
func TestWriteAuthErrorResponse(t *testing.T) {
	cases := []struct {
		name string
		err  error
		code int
	}{
		{"越权alias", auth.ErrAliasNotOwned, http.StatusForbidden},
		{"角色不足", auth.ErrRoleDenied, http.StatusForbidden},
		{"非法主体", auth.ErrInvalidSubject, http.StatusForbidden},
		{"过期", auth.ErrExpiredToken, http.StatusUnauthorized},
		{"未知错误", errors.New("boom"), http.StatusUnauthorized},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			rec := httptest.NewRecorder()
			WriteAuthErrorResponse(rec, tc.err)
			if rec.Code != tc.code {
				t.Fatalf("status = %d, want %d", rec.Code, tc.code)
			}
			decodeErrorClass(t, rec)
		})
	}
}
