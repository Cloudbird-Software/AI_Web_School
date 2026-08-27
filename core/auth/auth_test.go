package auth

// 主体模型与授权原语的语义测试（T-W5-005 验收 #1/#3）。

import (
	"errors"
	"strings"
	"testing"
)

func TestRoleValid(t *testing.T) {
	for _, r := range []Role{RoleStudent, RoleStaff, RoleOps, RoleService} {
		if !r.Valid() {
			t.Fatalf("role %q 应为合法值", r)
		}
	}
	for _, r := range []Role{"", "admin", "Student", "student "} {
		if r.Valid() {
			t.Fatalf("role %q 应为非法值", r)
		}
	}
}

// TestAssertOwnsAlias 授权原语核心语义：学生只碰自己的 alias；教研/
// 运维/内部作业由调用点显式授权（本原语不越权放行也不越权拒绝）；任何
// 非法主体类型必须报错——绝不允许零值主体静默通过（fail-closed）。
func TestAssertOwnsAlias(t *testing.T) {
	other := "not-my-alias"
	cases := []struct {
		name      string
		p         Principal
		aliasID   string
		wantError error
	}{
		{"学生访问自己的alias", studentPrincipal(), testAliasUUID, nil},
		{"学生访问他人alias", studentPrincipal(), other, ErrAliasNotOwned},
		{"学生alias为空", Principal{Role: RoleStudent, SubjectID: "a"}, other, ErrAliasNotOwned},
		{"staff任意alias", staffPrincipal(), other, nil},
		{"ops任意alias", Principal{Role: RoleOps, SubjectID: "o"}, other, nil},
		{"service任意alias", Principal{Role: RoleService, SubjectID: "j"}, other, nil},
		{"零值主体", Principal{}, other, ErrInvalidSubject},
		{"伪造类型", Principal{Role: "root", SubjectID: "x", AliasID: other}, other, ErrInvalidSubject},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			err := AssertOwnsAlias(tc.p, tc.aliasID)
			if tc.wantError == nil {
				if err != nil {
					t.Fatalf("应放行，得到 %v", err)
				}
				return
			}
			if !errors.Is(err, tc.wantError) {
				t.Fatalf("err = %v, want %v", err, tc.wantError)
			}
		})
	}
}

// TestValidatePrincipalRejectsOversizedID 超长标识符在模型层即拒，
// 防止异常载荷被照单全收进 context。
func TestValidatePrincipalRejectsOversizedID(t *testing.T) {
	p := studentPrincipal()
	p.AliasID = strings.Repeat("a", maxIDLen+1)
	if err := validatePrincipal(p); !errors.Is(err, ErrInvalidSubject) {
		t.Fatalf("err = %v, want ErrInvalidSubject", err)
	}
	p = staffPrincipal()
	p.SubjectID = strings.Repeat("s", maxIDLen+1)
	if err := validatePrincipal(p); !errors.Is(err, ErrInvalidSubject) {
		t.Fatalf("err = %v, want ErrInvalidSubject", err)
	}
}

// TestErrorClassification 认证/授权错误分类与 HTTP 状态映射的契约：
// 这两个函数是 api/middleware 脱敏映射的唯一依据。
func TestErrorClassification(t *testing.T) {
	for _, e := range []error{ErrNoToken, ErrMalformedToken, ErrBadSignature, ErrExpiredToken, ErrInvalidClaims} {
		if !IsAuthenticationError(e) || IsAuthorizationError(e) {
			t.Fatalf("%v 分类错误: authn=%v authz=%v", e, IsAuthenticationError(e), IsAuthorizationError(e))
		}
	}
	for _, e := range []error{ErrRoleDenied, ErrAliasNotOwned} {
		if IsAuthenticationError(e) || !IsAuthorizationError(e) {
			t.Fatalf("%v 分类错误: authn=%v authz=%v", e, IsAuthenticationError(e), IsAuthorizationError(e))
		}
	}
	// 包装过的错误必须保持可分类（调用方会 fmt.Errorf 加上下文）。
	wrapped := errors.Join(ErrAliasNotOwned)
	if !IsAuthorizationError(wrapped) {
		t.Fatal("包装后的授权错误应仍可分类")
	}
}
