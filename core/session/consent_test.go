// T-W5-010：RequireOnlinePracticeConsent（会话域授权门）的域级单测。
// HTTP 侧映射与脱敏形态在 api/consent_test.go，本文件只锁业务规则本身：
// purpose 选择、三态拒绝分型、故障透传分型（授权语义 vs 基础设施语义）。
package session

import (
	"context"
	"errors"
	"strings"
	"testing"
	"time"

	"github.com/Cloudbird-Software/AI_Web_School/core/compliance"
)

const (
	testAlias  = "11111111-2222-4333-8444-555555555555"
	testBroken = "not-a-uuid" // 非法链键：store 前置校验失败路径
)

// 测试时刻锚：窗口固定为「过去生效、2100 过期」，CheckConsent 的实时时钟
// 下恒 granted；短窗口锚恒 expired（相对当前日期）.
var (
	testSince      = time.Unix(1_700_000_000, 0).UTC()
	testUntilFar   = time.Unix(4_102_444_800, 0).UTC()
	testUntilEarly = testSince.Add(24 * time.Hour)
)

// grantedStore 返回 alias 已持有效 online_practice 授权的内存账.
func grantedStore(t *testing.T, alias string) *compliance.MemoryStore {
	t.Helper()
	s := compliance.NewMemoryStore()
	if _, err := s.RecordGrant(context.Background(), nil, compliance.GrantInput{
		StudentAliasID: alias,
		Purpose:        compliance.PurposeOnlinePractice,
		ValidFrom:      testSince,
		ValidUntil:     testUntilFar,
		RecordedBy:     "session-test",
		At:             testSince,
	}); err != nil {
		t.Fatalf("登记授权: %v", err)
	}
	return s
}

// TestRequireOnlinePracticeConsent_States 四态判定：granted 放行；其余三态
// 一律拒绝且错误可 errors.Is 到哨兵、可 errors.As 取出细分态（审计面）.
func TestRequireOnlinePracticeConsent_States(t *testing.T) {
	cases := []struct {
		name      string
		build     func(t *testing.T) *compliance.MemoryStore
		wantState compliance.State
		wantAllow bool
	}{
		{"granted放行", func(t *testing.T) *compliance.MemoryStore {
			return grantedStore(t, testAlias)
		}, "", true},
		{"missing拒绝", func(t *testing.T) *compliance.MemoryStore {
			return compliance.NewMemoryStore()
		}, compliance.StateMissing, false},
		{"revoked拒绝", func(t *testing.T) *compliance.MemoryStore {
			s := grantedStore(t, testAlias)
			if _, err := s.Revoke(context.Background(), nil, compliance.RevokeInput{
				StudentAliasID: testAlias,
				Purpose:        compliance.PurposeOnlinePractice,
				At:             testSince.Add(time.Hour),
			}); err != nil {
				t.Fatalf("撤回: %v", err)
			}
			return s
		}, compliance.StateRevoked, false},
		{"expired拒绝", func(t *testing.T) *compliance.MemoryStore {
			s := compliance.NewMemoryStore()
			if _, err := s.RecordGrant(context.Background(), nil, compliance.GrantInput{
				StudentAliasID: testAlias,
				Purpose:        compliance.PurposeOnlinePractice,
				ValidFrom:      testSince,
				ValidUntil:     testUntilEarly,
				At:             testSince,
			}); err != nil {
				t.Fatalf("登记授权: %v", err)
			}
			return s
		}, compliance.StateExpired, false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			err := RequireOnlinePracticeConsent(context.Background(), tc.build(t), testAlias)
			if tc.wantAllow {
				if err != nil {
					t.Fatalf("granted 必须放行，得到 %v", err)
				}
				return
			}
			if err == nil {
				t.Fatal("非 granted 态必须拒绝（X12 fail-closed）")
			}
			if !errors.Is(err, compliance.ErrConsentRequired) {
				t.Fatalf("拒绝必须锚定授权哨兵（协议层据此映射 403），得到 %v", err)
			}
			var cre *compliance.ConsentRequiredError
			if !errors.As(err, &cre) {
				t.Fatalf("拒绝错误必须可取出细分态载体，得到 %T", err)
			}
			if cre.State != tc.wantState {
				t.Fatalf("state = %q, want %q", cre.State, tc.wantState)
			}
			if cre.Purpose != compliance.PurposeOnlinePractice {
				t.Fatalf("purpose = %q, want %q（门只认在线练习入口键）", cre.Purpose, compliance.PurposeOnlinePractice)
			}
			// 验收 #4（可审计）：错误文本携带 alias/purpose/state 三元组，
			// 且 alias 经 Quote——日志行可审计又不可被控制字符注入。
			msg := err.Error()
			for _, want := range []string{testAlias, compliance.PurposeOnlinePractice, string(tc.wantState)} {
				if !strings.Contains(msg, want) {
					t.Fatalf("审计文本缺 %q: %s", want, msg)
				}
			}
		})
	}
}

// TestRequireOnlinePracticeConsent_FailClosed 故障分型：账本未装配 / 读取
// 失败 / 链键非法，全部拒绝且**不得**锚定授权哨兵——它们不是「无授权」
// （403）而是基础设施/输入故障（500），混淆会让运维把 DB 故障当越权排查.
func TestRequireOnlinePracticeConsent_FailClosed(t *testing.T) {
	cases := []struct {
		name  string
		store compliance.ConsentStore
		alias string
	}{
		{"store未装配", nil, testAlias},
		{"store读取失败", errStore(), testAlias},
		{"链键非法（store前置校验拒绝）", compliance.NewMemoryStore(), testBroken},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			err := RequireOnlinePracticeConsent(context.Background(), tc.store, tc.alias)
			if err == nil {
				t.Fatal("故障路径必须拒绝（绝不放行）")
			}
			if errors.Is(err, compliance.ErrConsentRequired) {
				t.Fatalf("基础设施故障不得伪装成授权缺失: %v", err)
			}
		})
	}
}

// faultStore 是恒故障的授权账（接口四方法全实现，故障注入同 API 侧形态）.
type faultStore struct{ err error }

func (f faultStore) RecordGrant(context.Context, compliance.Executor, compliance.GrantInput) (*compliance.ConsentEvent, error) {
	return nil, f.err
}

func (f faultStore) Revoke(context.Context, compliance.Executor, compliance.RevokeInput) (*compliance.ConsentEvent, error) {
	return nil, f.err
}

func (f faultStore) CheckConsent(context.Context, compliance.Executor, string, string, *time.Time) (*compliance.ConsentStatus, error) {
	return nil, f.err
}

func (f faultStore) History(context.Context, compliance.Executor, string, string) ([]compliance.ConsentEvent, error) {
	return nil, f.err
}

// errStore 返回 CheckConsent 恒失败的授权账.
func errStore() compliance.ConsentStore { return faultStore{err: errors.New("consent ledger down")} }
