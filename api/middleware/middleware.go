// Package middleware 承载 HTTP 中间件层（T-W5-005 重锚定落点：api/ 侧的
// 认证框架半边；对应 Python 时代 src/api/auth 的依赖注入原语）。
//
// 本包只提供"已认证主体"的通用装配：RequireAuth 完成认证 + 角色检查并
// 把 Principal 注入 request context；AssertOwnsAlias 等 alias 归属判定
// 仍在 core/auth，由业务 handler 取出主体后调用（全端点接线是 T-W5-006）。
// 错误响应对齐 api/api.go 脱敏惯例：对外只暴露 error_class 一个字段，
// 具体拒绝分支仅写服务端日志。
package middleware

import (
	"context"
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"strings"

	"github.com/Cloudbird-Software/AI_Web_School/core/auth"
)

// 对外错误类语义。固定为粗粒度两值：不区分"签名错/过期/缺令牌"等
// 内部分支——对外暴露的区别越细，给攻击者的校验反馈越多。
const (
	ErrorClassUnauthorized = "unauthorized"
	ErrorClassForbidden    = "forbidden"
)

// ctxKey 是 context 存取主体键的私有类型：防止其他包用同名基础类型
// 碰撞伪造主体（context key 必须非内建类型，这是 go vet 的要求也是安全惯例）。
type ctxKey struct{}

// FromContext 取请求上下文中的已认证主体。任何 handler 都不得自行从
// header 解析令牌——唯一入口是 RequireAuth 注入的值，避免绕过中间件
// 出现"自己看一眼 token 就信了"的分叉实现。
func FromContext(ctx context.Context) (auth.Principal, bool) {
	p, ok := ctx.Value(ctxKey{}).(auth.Principal)
	return p, ok
}

// errClass 返回错误的 Go 类型名（脱敏日志惯例，与 api.errClass 同源）：
// 类型名不含消息与参数，可在日志里定位分支又不倾倒底层细节。
func errClass(err error) string {
	if err == nil {
		return ""
	}
	return fmt.Sprintf("%T", err)
}

// parseBearer 解析 Authorization 头中的 Bearer 令牌。方案名按 RFC 7235
// 大小写不敏感；任何其他 scheme（Basic/Negotiate…）或空值一律按"无凭证"
// 处理——对外不区分失败细节，避免给探测者多一个可区分的反馈通道。
func parseBearer(header string) (string, bool) {
	const scheme = "bearer"
	fields := strings.Fields(header)
	if len(fields) != 2 || !strings.EqualFold(fields[0], scheme) {
		return "", false
	}
	return fields[1], true
}

// RequireAuth 是认证 + 角色检查中间件：解析 Authorization: Bearer 头，
// 验签/验期后把 Principal 注入 context，再放行 next。
//
//   - roles 为空 = 接受任意合法主体（仍必须认证成功，无匿名放行）；
//   - 认证失败一律 401 + WWW-Authenticate: Bearer（RFC 7235 挑战头），
//     统一响应体 {"error_class":"unauthorized"}；
//   - 已认证但角色不在集合内 → 403 {"error_class":"forbidden"}。
//
// signer 为 nil 属于装配期编程错误：与其在每请求路径上判空，不如在
// 启动时 panic 尽早暴露（fail fast）。
func RequireAuth(signer *auth.Signer, roles ...auth.Role) func(http.Handler) http.Handler {
	if signer == nil {
		panic("middleware.RequireAuth: signer 为 nil，属于启动装配错误")
	}
	allowed := make(map[auth.Role]struct{}, len(roles))
	for _, r := range roles {
		allowed[r] = struct{}{}
	}
	allowAll := len(roles) == 0
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			token, ok := parseBearer(r.Header.Get("Authorization"))
			if !ok {
				writeUnauthorized(w, auth.ErrNoToken)
				return
			}
			p, err := signer.Verify(token)
			if err != nil {
				writeUnauthorized(w, err)
				return
			}
			if !allowAll {
				if _, ok := allowed[p.Role]; !ok {
					log.Printf("auth denied class=forbidden reason_class=%s path=%s", errClass(auth.ErrRoleDenied), r.URL.Path)
					writeError(w, http.StatusForbidden, ErrorClassForbidden)
					return
				}
			}
			next.ServeHTTP(w, r.WithContext(context.WithValue(r.Context(), ctxKey{}, p)))
		})
	}
}

// WriteAuthErrorResponse 把授权原语返回的错误写成脱敏 HTTP 响应：
// IsAuthorizationError 类 → 403 forbidden；其余（含未知错误）→ 401
// unauthorized。默认落 401 是刻意的 fail-closed：分类失败宁可当成
// "身份不可信"，也不能当成"已认证可继续"。
//
// 业务 handler 在 AssertOwnsAlias 返回错误后调用本函数即可完成映射。
func WriteAuthErrorResponse(w http.ResponseWriter, err error) {
	if auth.IsAuthorizationError(err) {
		log.Printf("auth denied class=%q reason_class=%T", ErrorClassForbidden, err)
		writeError(w, http.StatusForbidden, ErrorClassForbidden)
		return
	}
	log.Printf("auth denied class=%q reason_class=%T", ErrorClassUnauthorized, err)
	writeError(w, http.StatusUnauthorized, ErrorClassUnauthorized)
}

// writeUnauthorized 统一 401 输出：挑战头 + 单字段脱敏体 + 服务端日志
// （reason 只进日志）。
func writeUnauthorized(w http.ResponseWriter, reason error) {
	log.Printf("auth denied class=%q reason_class=%T", ErrorClassUnauthorized, reason)
	w.Header().Set("WWW-Authenticate", `Bearer realm="school-api"`)
	writeError(w, http.StatusUnauthorized, ErrorClassUnauthorized)
}

// errorResponse 是认证/授权失败的线上形态：单字段、零内部细节。
type errorResponse struct {
	ErrorClass string `json:"error_class"`
}

func writeError(w http.ResponseWriter, status int, class string) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	// 响应编码失败已无可用降级通道：记日志留痕即可（对齐 api.go 惯例）。
	if err := json.NewEncoder(w).Encode(errorResponse{ErrorClass: class}); err != nil {
		log.Printf("auth error encode failure class=%T", err)
	}
}
