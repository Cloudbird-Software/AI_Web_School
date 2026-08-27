// T-W5-010 家长授权接入在线会话入口的 HTTP 行为测试。
//
// 四象限矩阵（本卡核心验收）：
//
//	granted  → 通过授权门，落到业务占位 501
//	missing  → 403 forbidden
//	revoked  → 403 forbidden（撤回后立即失效）
//	expired  → 403 forbidden
//	store 故障（nil / 读失败）→ 500 internal，绝不放行（X12 fail-closed）
//
// 脱敏纪律断言：三种拒绝态对外响应完全同构（单字段 forbidden）——对外
// 粗粒度、内部分型只进服务端审计日志。与既有 openapi 匿名扫描测试共存：
// 本文件全部走已认证主体，匿名面由 authz_test.go 独立复核。
package api

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"testing"
	"time"

	"github.com/Cloudbird-Software/AI_Web_School/api/middleware"
	"github.com/Cloudbird-Software/AI_Web_School/core/compliance"
)

// grantConsent 在指定授权账上为 alias 登记一条 grant 事件（窗口由调用方给；
// MemoryStore 不需要事务执行面，Executor 传 nil 是其契约内用法）。
func grantConsent(t *testing.T, store *compliance.MemoryStore, alias string, from, until, at time.Time) {
	t.Helper()
	if _, err := store.RecordGrant(context.Background(), nil, compliance.GrantInput{
		StudentAliasID: alias,
		Purpose:        compliance.PurposeOnlinePractice,
		ValidFrom:      from,
		ValidUntil:     until,
		RecordedBy:     "consent-test",
		At:             at,
	}); err != nil {
		t.Fatalf("登记家长授权（alias=%s）: %v", alias, err)
	}
}

// revokeConsent 撤回 alias 的有效授权（失败即测试前提破坏，立刻红）。
func revokeConsent(t *testing.T, store *compliance.MemoryStore, alias string, at time.Time) {
	t.Helper()
	if _, err := store.Revoke(context.Background(), nil, compliance.RevokeInput{
		StudentAliasID: alias,
		Purpose:        compliance.PurposeOnlinePractice,
		RecordedBy:     "consent-test/parent",
		At:             at,
	}); err != nil {
		t.Fatalf("撤回家长授权（alias=%s）: %v", alias, err)
	}
}

// withStore 以指定授权账重装路由：复用 fixture 的 signer 与令牌面，只换
// store——各授权态子测试互不污染。
func (f *apiFixture) withStore(store compliance.ConsentStore) *apiFixture {
	f.app = NewRouterWithConsent(f.signer, store)
	return f
}

// brokenConsentStore 是授权账故障注入实现：全部方法恒返回注入错误
// （模拟 DB 不可达）。四方法齐备以兑现 ConsentStore 并发契约的接口形状.
type brokenConsentStore struct{ err error }

func (b *brokenConsentStore) RecordGrant(context.Context, compliance.Executor, compliance.GrantInput) (*compliance.ConsentEvent, error) {
	return nil, b.err
}

func (b *brokenConsentStore) Revoke(context.Context, compliance.Executor, compliance.RevokeInput) (*compliance.ConsentEvent, error) {
	return nil, b.err
}

func (b *brokenConsentStore) CheckConsent(context.Context, compliance.Executor, string, string, *time.Time) (*compliance.ConsentStatus, error) {
	return nil, b.err
}

func (b *brokenConsentStore) History(context.Context, compliance.Executor, string, string) ([]compliance.ConsentEvent, error) {
	return nil, b.err
}

// TestCreateSession_ConsentGateQuadrants 四象限：granted 放行到业务占位，
// missing/revoked/expired 一律 403；且三种拒绝态对外形态逐字节相同
// （脱敏纪律：客户端不可区分授权拒绝的具体原因，分型只在审计日志）。
func TestCreateSession_ConsentGateQuadrants(t *testing.T) {
	grantedUntil := func(t *testing.T) *compliance.MemoryStore {
		s := compliance.NewMemoryStore()
		grantConsent(t, s, apiAliasSelf, apiTestSince, apiTestUntilFar, apiTestSince)
		return s
	}
	cases := []struct {
		name      string
		build     func(t *testing.T) *compliance.MemoryStore
		wantCode  int
		wantClass string
	}{
		{
			name:      "granted_放行到业务占位",
			build:     grantedUntil,
			wantCode:  http.StatusNotImplemented,
			wantClass: ErrorClassNotImplemented,
		},
		{
			name: "missing_从未授权",
			build: func(t *testing.T) *compliance.MemoryStore {
				return compliance.NewMemoryStore()
			},
			wantCode:  http.StatusForbidden,
			wantClass: middleware.ErrorClassForbidden,
		},
		{
			name: "revoked_撤回后立即失效",
			build: func(t *testing.T) *compliance.MemoryStore {
				s := grantedUntil(t)
				revokeConsent(t, s, apiAliasSelf, apiTestSince.Add(time.Hour))
				return s
			},
			wantCode:  http.StatusForbidden,
			wantClass: middleware.ErrorClassForbidden,
		},
		{
			name: "expired_授权窗口已过",
			build: func(t *testing.T) *compliance.MemoryStore {
				s := compliance.NewMemoryStore()
				grantConsent(t, s, apiAliasSelf, apiTestSince, apiTestUntilEarly, apiTestSince)
				return s
			},
			wantCode:  http.StatusForbidden,
			wantClass: middleware.ErrorClassForbidden,
		},
	}
	var denyBodies []string
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			f := newAPIFixture(t).withStore(tc.build(t))
			rec := f.do(http.MethodPost, "/sessions", f.selfTok, "")
			if rec.Code != tc.wantCode {
				t.Fatalf("status = %d, want %d（body=%s）", rec.Code, tc.wantCode, rec.Body.String())
			}
			expectSingleFieldError(t, rec, tc.wantClass)
			if tc.wantCode == http.StatusForbidden {
				denyBodies = append(denyBodies, rec.Body.String())
			}
		})
	}
	for i, body := range denyBodies {
		if body != denyBodies[0] {
			t.Fatalf("拒绝态对外形态必须同构：case %d = %q, want %q", i, body, denyBodies[0])
		}
	}
}

// TestCreateSession_ConsentStoreUnavailable_FailClosed 授权账不可用 ≠ 无授权
// ≠ 放行：账本未装配（nil，生产 W6 接线前的既定形态）或读取失败（DB 不可
// 达）时创建入口一律 500 internal 拒绝——绝不落到业务占位 501，也绝不伪装
// 成 403 把基础设施故障说成授权问题（X12：合规失败宁可拒服务）。
func TestCreateSession_ConsentStoreUnavailable_FailClosed(t *testing.T) {
	t.Run("store未装配nil", func(t *testing.T) {
		f := newAPIFixture(t).withStore(nil)
		rec := f.do(http.MethodPost, "/sessions", f.selfTok, "")
		if rec.Code != http.StatusInternalServerError {
			t.Fatalf("status = %d, want 500（fail-closed，绝不放行）", rec.Code)
		}
		expectSingleFieldError(t, rec, middleware.ErrorClassInternal)
	})
	t.Run("store读取失败", func(t *testing.T) {
		f := newAPIFixture(t).withStore(&brokenConsentStore{err: errors.New("db connection refused")})
		rec := f.do(http.MethodPost, "/sessions", f.selfTok, "")
		if rec.Code != http.StatusInternalServerError {
			t.Fatalf("status = %d, want 500（账本读不到更不能放行）", rec.Code)
		}
		expectSingleFieldError(t, rec, middleware.ErrorClassInternal)
	})
}

// TestCreateSession_SpoofCheckPrecedesConsentGate 处理序论证的机器化：
// 越权判据先于授权门——请求体携带他人 alias 恒 403，且响应不随目标学生
// 的授权状态变化；否则 /sessions 就是任意 alias 授权状态的探测 oracle.
func TestCreateSession_SpoofCheckPrecedesConsentGate(t *testing.T) {
	f := newAPIFixture(t) // self 已有授权（fixture），alien 未授权
	body := fmt.Sprintf(`{"student_alias_id":%q}`, apiAliasAlien)
	first := f.do(http.MethodPost, "/sessions", f.selfTok, body)
	expectForbidden(t, first)

	// 给 alien 补授权后重试：对外响应逐字节不变——探测者拿不到「他人是否
	// 有授权」的任何可区分信号。
	grantConsent(t, f.consent, apiAliasAlien, apiTestSince, apiTestUntilFar, apiTestSince)
	second := f.do(http.MethodPost, "/sessions", f.selfTok, body)
	expectForbidden(t, second)
	if second.Body.String() != first.Body.String() {
		t.Fatalf("冒用请求的对外响应不得随目标授权状态变化: %q vs %q",
			first.Body.String(), second.Body.String())
	}

	// 对照：本人 alias 走授权门放行到占位——证明门只对令牌主体生效，
	// 403 全部来自越权判据而非门的误伤。
	expectPlaceholder(t, f.do(http.MethodPost, "/sessions", f.selfTok,
		fmt.Sprintf(`{"student_alias_id":%q}`, apiAliasSelf)))
}

// TestCreateSession_ConsentDenied_ZeroWrites 验收 #3 的骨架期形态：拒绝路径
// 发生在任何业务逻辑之前——授权账自身零副作用（门只读，拒绝前后事件数不
// 变）。骨架期不存在会话/作答写入面，response_event/practice_session 计数
// 不变的断言随业务写入路径落地（T-W5-018）在集成侧补齐；本测试锁住其前提
// 「未授权即拒绝、拒绝零写入」。
func TestCreateSession_ConsentDenied_ZeroWrites(t *testing.T) {
	build := map[string]func(t *testing.T) *compliance.MemoryStore{
		"missing": func(t *testing.T) *compliance.MemoryStore {
			return compliance.NewMemoryStore()
		},
		"revoked": func(t *testing.T) *compliance.MemoryStore {
			s := compliance.NewMemoryStore()
			grantConsent(t, s, apiAliasSelf, apiTestSince, apiTestUntilFar, apiTestSince)
			revokeConsent(t, s, apiAliasSelf, apiTestSince.Add(time.Hour))
			return s
		},
	}
	for name, mk := range build {
		t.Run(name, func(t *testing.T) {
			store := mk(t)
			before, err := store.History(context.Background(), nil, apiAliasSelf, compliance.PurposeOnlinePractice)
			if err != nil {
				t.Fatalf("读授权账: %v", err)
			}
			f := newAPIFixture(t).withStore(store)
			rec := f.do(http.MethodPost, "/sessions", f.selfTok, "")
			if rec.Code != http.StatusForbidden {
				t.Fatalf("status = %d, want 403", rec.Code)
			}
			after, err := store.History(context.Background(), nil, apiAliasSelf, compliance.PurposeOnlinePractice)
			if err != nil {
				t.Fatalf("读授权账: %v", err)
			}
			if len(after) != len(before) {
				t.Fatalf("拒绝路径改写了授权账: before=%d after=%d", len(before), len(after))
			}
		})
	}
}

// TestSessionSubresources_NotConsentGated 授权门覆盖面的回归锚：任务卡
// 验收 #1 只要求会话**创建**入口前置校验（#2 的提交二次校验以会话归属
// 读取为前提，随业务写入路径落地，见 sessionScoped 留痕）；故零授权账上
// 子资源路由仍应直达 501 占位。若未来要把门扩到恢复/读取路径，先改本
// 断言再改路由，不允许静默扩散门禁范围。
func TestSessionSubresources_NotConsentGated(t *testing.T) {
	f := newAPIFixture(t).withStore(compliance.NewMemoryStore()) // 全空授权账
	for _, tt := range []struct{ method, suffix string }{
		{http.MethodGet, "/" + apiSessionID},
		{http.MethodGet, "/" + apiSessionID + "/next"},
		{http.MethodPost, "/" + apiSessionID + "/responses"},
		{http.MethodPost, "/" + apiSessionID + "/resume"},
		{http.MethodPost, "/" + apiSessionID + "/abandon"},
	} {
		rec := f.do(tt.method, "/sessions"+tt.suffix, f.selfTok, "")
		expectPlaceholder(t, rec)
	}
}
