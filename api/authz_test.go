package api

// T-W5-006 全端点认证接线的 HTTP 行为测试。四象限矩阵按端点类别铺开：
// 无令牌（401）/ 错误角色（403）/ 越权 alias（403）/ 合法主体（骨架期 501 占位）。
//
// 端点清单有两个互不信任的来源（P3 机器信号）：
//  1. routes() 声明表——结构审计断言每条都挂了 shield；
//  2. 冻结契约 openapi-v1.yaml——运行期最小缩进扫描（零依赖），对每个业务
//     端点发起匿名请求断言 401，新增契约端点未接线即红，漏加认证也红。
//
// 全部用 httptest.ResponseRecorder 直驱 mux：无 goroutine，兼容包内既有
// goleak TestMain 与 -race。
//
// 会话子资源（session_id 寻址）的"越权 alias"象限说明：会话归属
// （会话.alias == p.AliasID）需 DB 读支持，是业务落地波次的验收项
// （api.sessionScoped 已留痕）；骨架期该类路由在任何数据读取前就返回
// 501，不存在可泄露数据，故本文件对此类只测角色面。

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/Cloudbird-Software/AI_Web_School/api/middleware"
	"github.com/Cloudbird-Software/AI_Web_School/core/auth"
	"github.com/Cloudbird-Software/AI_Web_School/core/compliance"
)

const apiTestKeyMaterial = "api-authz-test-secret-0123456789-t-w5-006"

const (
	apiAliasSelf  = "11111111-2222-4333-8444-555555555555"
	apiAliasAlien = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
	apiSessionID  = "6f9619ff-8b86-4000-b42d-00cf4fc964ff"
)

// 授权账测试时刻锚：grant 窗口固定为「过去生效、远期（2100）才过期」，
// 与 MemoryStore.CheckConsent 的实时时钟组合下恒为 granted，测试不随
// 挂钟漂移。apiTestUntilEarly 是短窗口锚，供过期态测试复用.
var (
	apiTestSince      = time.Unix(1_700_000_000, 0).UTC() // 2023-11：过去
	apiTestUntilEarly = apiTestSince.Add(24 * time.Hour)  // 早已过期
	apiTestUntilFar   = time.Unix(4_102_444_800, 0).UTC() // 2100：远未过期
)

type apiFixture struct {
	signer  *auth.Signer
	app     http.Handler
	selfTok string // 学生 A 的合法令牌（绑定 alias=self）
	// consent 是 T-W5-010 的 fixture 授权账：A（apiAliasSelf）已持有效授权
	//（验收 #5 的「fixture 补授权」形态——既有测试靠补齐授权前提适配新
	// 门禁，绝不通过跳过检查变绿，X11）。需要别的授权态时在子测试里自建
	// store 并经 NewRouterWithConsent 重装（见 consent_test.go）。
	consent *compliance.MemoryStore
}

func newAPIFixture(t *testing.T) *apiFixture {
	t.Helper()
	signer, err := auth.NewSignerWithClock([]byte(apiTestKeyMaterial), func() time.Time {
		return time.Unix(1_800_000_000, 0).UTC()
	})
	if err != nil {
		t.Fatalf("构造 Signer: %v", err)
	}
	store := compliance.NewMemoryStore()
	if _, err := store.RecordGrant(context.Background(), nil, compliance.GrantInput{
		StudentAliasID: apiAliasSelf,
		Purpose:        compliance.PurposeOnlinePractice,
		ValidFrom:      apiTestSince,
		ValidUntil:     apiTestUntilFar,
		RecordedBy:     "api-test-fixture",
		At:             apiTestSince,
	}); err != nil {
		t.Fatalf("fixture 登记家长授权: %v", err)
	}
	f := &apiFixture{signer: signer, app: NewRouterWithConsent(signer, store), consent: store}
	f.selfTok = f.tokenFor(t, auth.Principal{Role: auth.RoleStudent, SubjectID: "acc-self", AliasID: apiAliasSelf})
	return f
}

func (f *apiFixture) tokenFor(t *testing.T, p auth.Principal) string {
	t.Helper()
	tok, err := f.signer.Issue(p, time.Hour)
	if err != nil {
		t.Fatalf("签发令牌: %v", err)
	}
	return tok
}

// do 对路由骨架发起请求；token 为空即匿名，body 为空即无载荷。
func (f *apiFixture) do(method, target, token, body string) *httptest.ResponseRecorder {
	r := httptest.NewRequest(method, target, nil)
	if body != "" {
		r = httptest.NewRequest(method, target, strings.NewReader(body))
	}
	if token != "" {
		r.Header.Set("Authorization", "Bearer "+token)
	}
	rec := httptest.NewRecorder()
	f.app.ServeHTTP(rec, r)
	return rec
}

// expectSingleFieldError 断言响应体是单字段 error_class JSON——字段最小化是
// 脱敏原则的可执行形态；error_class 取值与 middleware 的对外两值常量对齐，
// 不在本包另立真相源。
func expectSingleFieldError(t *testing.T, rec *httptest.ResponseRecorder, wantClass string) {
	t.Helper()
	var body map[string]any
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
		t.Fatalf("响应必须是 JSON: %v（body=%q）", err, rec.Body.String())
	}
	if len(body) != 1 {
		t.Fatalf("错误响应字段必须最小化（仅 error_class），得到 %v", body)
	}
	if got := body["error_class"]; got != wantClass {
		t.Fatalf("error_class = %v, want %q", got, wantClass)
	}
}

func expectUnauthorized(t *testing.T, rec *httptest.ResponseRecorder) {
	t.Helper()
	if rec.Code != http.StatusUnauthorized {
		t.Fatalf("status = %d, want 401", rec.Code)
	}
	if v := rec.Header().Get("WWW-Authenticate"); !strings.HasPrefix(v, "Bearer") {
		t.Fatalf("401 必须携带 WWW-Authenticate 挑战头，得到 %q", v)
	}
	expectSingleFieldError(t, rec, middleware.ErrorClassUnauthorized)
}

func expectForbidden(t *testing.T, rec *httptest.ResponseRecorder) {
	t.Helper()
	if rec.Code != http.StatusForbidden {
		t.Fatalf("status = %d, want 403", rec.Code)
	}
	expectSingleFieldError(t, rec, middleware.ErrorClassForbidden)
}

func expectPlaceholder(t *testing.T, rec *httptest.ResponseRecorder) {
	t.Helper()
	if rec.Code != http.StatusNotImplemented {
		t.Fatalf("status = %d, want 501（body=%s）", rec.Code, rec.Body.String())
	}
	expectSingleFieldError(t, rec, ErrorClassNotImplemented)
}

// studentOf 返回绑定指定 alias 的学生主体样本。
func studentOf(alias string) auth.Principal {
	return auth.Principal{Role: auth.RoleStudent, SubjectID: "acc-" + alias[:4], AliasID: alias}
}

var nonStudentPrincipals = []auth.Principal{
	{Role: auth.RoleStaff, SubjectID: "staff-7"},
	{Role: auth.RoleOps, SubjectID: "ops-9"},
	{Role: auth.RoleService, SubjectID: "svc-job"},
}

// --- 审计一：契约派生的匿名扫描（独立于 routes 表的第二事实源） ---

type contractEndpoint struct{ method, path string }

// contractEndpoints 对冻结契约做最小缩进扫描：v3 文档顶层键在 0 列、path 键
// 固定 2 空格缩进、操作方法键固定 4 空格缩进；多行描述等深缩进内容不会精确
// 匹配这些形态。零依赖（标准库）：为契约测试引入 YAML 解析器属新依赖评审范围。
func contractEndpoints(t *testing.T) []contractEndpoint {
	t.Helper()
	raw, err := os.ReadFile(filepath.Join("..", "specs", "contracts", "api", "openapi-v1.yaml"))
	if err != nil {
		t.Fatalf("读取冻结契约失败（契约即测试输入源，缺失不得静默跳过）: %v", err)
	}
	known := map[string]bool{"get": true, "post": true, "put": true, "patch": true, "delete": true}
	var out []contractEndpoint
	cur := ""
	inPaths := false
	for _, line := range strings.Split(string(raw), "\n") {
		line = strings.TrimRight(line, "\r")
		switch {
		case line == "paths:":
			inPaths, cur = true, ""
			continue
		case !inPaths:
			if line == "components:" {
				inPaths = false // 契约中 components 段在 paths 之后，至此收束
			}
			continue
		case strings.HasPrefix(line, "  /") && !strings.HasPrefix(line, "   ") && strings.HasSuffix(line, ":"):
			cur = strings.TrimSuffix(strings.TrimPrefix(line, "  "), ":")
		case strings.HasPrefix(line, "    ") && !strings.HasPrefix(line, "     ") && cur != "":
			m := strings.TrimSuffix(strings.TrimPrefix(line, "    "), ":")
			if known[m] {
				out = append(out, contractEndpoint{method: m, path: cur})
			}
		}
	}
	if len(out) == 0 {
		t.Fatal("契约扫描得到 0 个端点：解析逻辑与契约形态漂移，禁止静默放行")
	}
	return out
}

// literalize 把契约路径模板的通配段换成代表性字面量（占位路由只关心认证与
// 绑定语义，不做标识符格式校验）。
func literalize(path, aliasID string) string {
	segs := strings.Split(path, "/")
	for i, seg := range segs {
		if strings.HasPrefix(seg, "{") && strings.HasSuffix(seg, "}") {
			switch name := seg[1 : len(seg)-1]; name {
			case "student_alias_id":
				segs[i] = aliasID
			case "session_id":
				segs[i] = apiSessionID
			default:
				segs[i] = "sample-" + name
			}
		}
	}
	return strings.Join(segs, "/")
}

// TestContractEndpoints_RequireAuthentication 铁律 D9/X13 的契约级实证：
// 除健康检查白名单外，契约上每一个端点对匿名请求一律 401。端点清单直接从
// 冻结 YAML 扫出——将来契约新增端点而路由未接线（404）或漏加认证（放行）
// 本测试都变红。
func TestContractEndpoints_RequireAuthentication(t *testing.T) {
	f := newAPIFixture(t)
	for _, ep := range contractEndpoints(t) {
		name := ep.method + " " + ep.path
		t.Run(name, func(t *testing.T) {
			if ep.path == "/health" {
				// 契约 /health 即存活探针（D9 明文豁免匿名）；T-W5-008 起与
				// /healthz 同 handler 对外（healthHandler），白名单回归见
				// TestHealthz_StaysAnonymous。
				return
			}
			// httptest.NewRequest 接受纯路径；契约路径本身以 / 开头。
			// 契约方法键按惯例小写，HTTP 方法名规范化为大写后匹配路由。
			rec := f.do(strings.ToUpper(ep.method), literalize(ep.path, apiAliasSelf), "", "")
			expectUnauthorized(t, rec)
		})
	}
}

// --- 审计二：routes 声明表结构约束 ---

// TestRouteTable_NoAnonymousRoute 结构审计：声明表不允许出现无 shield 的
// 路由（X13 的构造性防线；行为级由契约扫描复核）。/healthz 白名单不经本表。
func TestRouteTable_NoAnonymousRoute(t *testing.T) {
	for _, rt := range routes(newAPIFixture(t).signer) {
		if rt.shield == nil || rt.handle == nil {
			t.Errorf("路由 %s 缺少认证中间件或 handler（X13：不存在无主体端点）", rt.pattern)
		}
	}
}

// --- 内容资产 / 门证书只读查询：教研 + 运维角色面 ---

func TestContentReads_RoleMatrix(t *testing.T) {
	contentTargets := []string{
		"/items/sample-item_id",
		"/item_versions/sample-item_version_id",
		"/templates/sample-template_id",
		"/gate_certificates/sample-cert_id",
	}
	for _, target := range contentTargets {
		t.Run(http.MethodGet+" "+target, func(t *testing.T) {
			f := newAPIFixture(t)

			// 错误角色：学生与内部作业主体一律 403——教学域查询不对学生开放，
			// service 也不因"系统调用方"身份获得默认数据面权限。
			denied := []auth.Principal{
				studentOf(apiAliasSelf),
				{Role: auth.RoleService, SubjectID: "svc-job"},
			}
			for _, p := range denied {
				rec := f.do(http.MethodGet, target, f.tokenFor(t, p), "")
				expectForbidden(t, rec)
			}

			// 授权角色到达占位 handler：staff 与 ops 都是 501（证明链路贯通，
			// 且不是伪造 200 业务响应）。
			rec := f.do(http.MethodGet, target, f.tokenFor(t, auth.Principal{Role: auth.RoleStaff, SubjectID: "staff-ok"}), "")
			expectPlaceholder(t, rec)
			rec = f.do(http.MethodGet, target, f.tokenFor(t, auth.Principal{Role: auth.RoleOps, SubjectID: "ops-ok"}), "")
			expectPlaceholder(t, rec)
		})
	}
}

// --- 会话生命周期：仅学生主体 ---

func TestSessionSubresources_RoleMatrix(t *testing.T) {
	subresources := []struct{ method, suffix, body string }{
		{http.MethodGet, "/" + apiSessionID, ""},
		{http.MethodGet, "/" + apiSessionID + "/next", ""},
		{http.MethodPost, "/" + apiSessionID + "/responses", "{}"},
		{http.MethodPost, "/" + apiSessionID + "/resume", ""},
		{http.MethodPost, "/" + apiSessionID + "/abandon", ""},
	}
	for _, tt := range subresources {
		t.Run(tt.method+" "+tt.suffix, func(t *testing.T) {
			full := "/sessions" + tt.suffix
			f := newAPIFixture(t)
			for _, p := range nonStudentPrincipals { // staff/ops/service 都不在学生会话授权面
				rec := f.do(tt.method, full, f.tokenFor(t, p), tt.body)
				expectForbidden(t, rec)
			}
			expectPlaceholder(t, f.do(tt.method, full, f.selfTok, tt.body))
		})
	}
}

// --- POST /sessions：创建身份取自令牌主体，请求体 alias 仅作冒用判据 ---

func TestCreateSession_BindsPrincipalNotRequestBody(t *testing.T) {
	f := newAPIFixture(t)

	// 无令牌 → 401（契约扫描已覆盖，本地保留同构断言便于单卡阅读）。
	expectUnauthorized(t, f.do(http.MethodPost, "/sessions", "", ""))

	// 教研代建也不行：POST /sessions 是学生作答域入口。
	rec := f.do(http.MethodPost, "/sessions", f.tokenFor(t, auth.Principal{Role: auth.RoleStaff, SubjectID: "staff-1"}), "{}")
	expectForbidden(t, rec)

	// 学生持合法令牌、请求体写他人 alias：跨学生代建尝试 → 403 越权
	//（铁律：请求体传入的 alias 不再是授权依据，令牌主体才是）。
	rec = f.do(http.MethodPost, "/sessions", f.selfTok,
		fmt.Sprintf(`{"student_alias_id":%q}`, apiAliasAlien))
	expectForbidden(t, rec)

	// 自己的 alias 或缺省体：通过越权判定与家长授权门（fixture 已为 self
	// 补授权，T-W5-010 验收 #5），落到业务占位 501。
	rec = f.do(http.MethodPost, "/sessions", f.selfTok,
		fmt.Sprintf(`{"student_alias_id":%q}`, apiAliasSelf))
	expectPlaceholder(t, rec)
	expectPlaceholder(t, f.do(http.MethodPost, "/sessions", f.selfTok, ""))
}

// --- 弱项报告 / 复习队列：角色面 + 主体↔alias 绑定 ---

func TestAliasBoundReads_EnforceOwnerBinding(t *testing.T) {
	prefixes := []string{"/reports/weakness/", "/review/due/"}
	for _, prefix := range prefixes {
		t.Run(prefix, func(t *testing.T) {
			f := newAPIFixture(t)

			// 非学生角色先被中间件拒绝（独立可审计的角色面）。
			for _, p := range nonStudentPrincipals {
				rec := f.do(http.MethodGet, prefix+apiAliasSelf, f.tokenFor(t, p), "")
				expectForbidden(t, rec)
			}

			// 学生访问他人 alias 数据 → 403（AssertOwnsAlias，宪法 D9 核心）。
			rec := f.do(http.MethodGet, prefix+apiAliasAlien, f.selfTok, "")
			expectForbidden(t, rec)

			// 访问自己的 alias → 通过绑定校验到业务占位 501。
			expectPlaceholder(t, f.do(http.MethodGet, prefix+apiAliasSelf, f.selfTok, ""))
		})
	}
}

// --- /healthz 白名单豁免保持匿名 ---

// TestHealthz_StaysAnonymous 白名单回归：接线全量认证之后存活探针仍须对
// 匿名开放（编排器无凭证探测）；一旦误锁死立即在此显红。
func TestHealthz_StaysAnonymous(t *testing.T) {
	f := newAPIFixture(t)
	rec := f.do(http.MethodGet, "/healthz", "", "")
	if rec.Code != http.StatusOK {
		t.Fatalf("/healthz 匿名 status = %d, want 200（健康检查为 D9 明文豁免）", rec.Code)
	}
	var body map[string]any
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil || body["status"] != "ok" {
		t.Fatalf("/healthz 响应异常: %q（err=%v）", rec.Body.String(), err)
	}
}
