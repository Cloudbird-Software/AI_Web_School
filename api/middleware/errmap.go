// errmap.go 承载 T-W5-008 的统一错误映射与边界防护（Recover / LimitBody）。
//
// 映射契约（验收 #3）：领域错误 → 明确 HTTP 状态 + error_class 单字段；
// 未知错误 → 500 且响应体不含栈/SQL/文件路径，完整信息只进服务端日志。
// 响应形态与既有 writeError 惯例同构（api/api.go 与本包 T-W5-005 的
// 认证错误输出），本文件把它提升为全边界的统一出口。
package middleware

import (
	"bytes"
	"encoding/json"
	"errors"
	"io"
	"log"
	"net/http"
	"runtime/debug"
	"strconv"

	"github.com/Cloudbird-Software/AI_Web_School/core/auth"
	"github.com/Cloudbird-Software/AI_Web_School/core/compliance"
)

// 对外错误类语义。线上形态只有 error_class 一个字段：类粒度刻意压粗——
// 对外暴露的分支越细，给探测者的可区分反馈越多（T-W5-005 既定纪律）。
// 不区分"签名错/过期/缺令牌"等内部分支，同理也不区分限流命中发生在
// 哪个配额维度。
const (
	ErrorClassUnauthorized    = "unauthorized"
	ErrorClassForbidden       = "forbidden"
	ErrorClassNotFound        = "not_found"
	ErrorClassBadRequest      = "bad_request"
	ErrorClassPayloadTooLarge = "payload_too_large"
	ErrorClassRateLimited     = "rate_limited"
	ErrorClassOriginForbidden = "origin_forbidden"
	ErrorClassInternal        = "internal"
)

// errorResponse 是错误响应的线上形态：单字段、零内部细节。
type errorResponse struct {
	ErrorClass string `json:"error_class"`
}

// WriteError 输出单字段脱敏 JSON 错误响应（T-W5-005 私有 writeError 的
// 统一化：边界三件套与业务 handler 共用同一出口，杜绝第二种错误形态）。
// 响应编码失败已无可用降级通道：记日志留痕即可。
func WriteError(w http.ResponseWriter, status int, class string) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	if err := json.NewEncoder(w).Encode(errorResponse{ErrorClass: class}); err != nil {
		log.Printf("error response encode failure class=%T", err)
	}
}

// MapError 是领域错误 → (HTTP 状态, error_class) 的统一映射矩阵。
//
// 矩阵（按判定顺序，先具体后兜底）：
//
//   - *http.MaxBytesError（请求体超限）→ 413 payload_too_large；
//   - 家长授权缺失（T-W5-010，missing/revoked/expired 三态统一哨兵）→
//     403 forbidden：宪法红线「家长授权前置」的协议映射。对外只用既有粗粒度
//     forbidden 类——三类拒绝态的区别只在服务端审计日志（ConsentRequiredError
//     的 State），给客户端细分等于向探测者开放他人授权状态的反馈通道；
//   - 授权类（角色不足/越权 alias/主体模型非法）→ 403 forbidden（D9）；
//   - 认证类（缺令牌/签名错/过期/载荷非法）→ 401 unauthorized；
//   - 其余一律 500 internal：未知错误绝不映射成 4xx"客户端错误"误导
//     调用方重试，也绝不把内部细节带出进程（fail-closed 脱敏）。
//
// 会话域等后续波次的业务哨兵错误在各自落地时扩展本矩阵（单点扩展，
// 不允许 handler 各自 errors.Is 满天飞）。
func MapError(err error) (int, string) {
	if err == nil {
		return http.StatusInternalServerError, ErrorClassInternal
	}
	var tooLarge *http.MaxBytesError
	switch {
	case errors.As(err, &tooLarge):
		return http.StatusRequestEntityTooLarge, ErrorClassPayloadTooLarge
	case errors.Is(err, compliance.ErrConsentRequired):
		return http.StatusForbidden, ErrorClassForbidden
	case auth.IsAuthorizationError(err):
		return http.StatusForbidden, ErrorClassForbidden
	case auth.IsAuthenticationError(err):
		return http.StatusUnauthorized, ErrorClassUnauthorized
	default:
		return http.StatusInternalServerError, ErrorClassInternal
	}
}

// HandleError 把 handler 侧错误按矩阵写成脱敏响应；完整原因只进服务端
// 日志（验收 #3：日志保留完整信息，响应体零内部细节）。
//
// 注意与 WriteAuthErrorResponse 的分野：后者面向认证/授权原语返回点，
// 未知错误按"身份不可信"fail-closed 落 401；本函数面向一般 handler 错误，
// 未知一律 500。两者的兜底语义不同且都不可互换。
func HandleError(w http.ResponseWriter, err error) {
	status, class := MapError(err)
	log.Printf("request error status=%d class=%q reason=%v", status, class, err)
	WriteError(w, status, class)
}

// interceptWriter 记录"响应头是否已发出"，供 Recover 判断 panic 发生时
// 还能不能补一个干净的 500。WriteHeader 幂等化：多余的二次调用静默丢弃
// （net/http 只会记告警日志，这里直接收敛）。
type interceptWriter struct {
	http.ResponseWriter
	wroteHeader bool
}

func (w *interceptWriter) WriteHeader(status int) {
	if w.wroteHeader {
		return
	}
	w.wroteHeader = true
	w.ResponseWriter.WriteHeader(status)
}

func (w *interceptWriter) Write(b []byte) (int, error) {
	if !w.wroteHeader {
		w.WriteHeader(http.StatusOK)
	}
	return w.ResponseWriter.Write(b)
}

// Recover 是最外层 panic 防线：任何内层（CORS/限流/体限/认证/业务
// handler）的 panic 都收敛为——
//
//   - 响应：500 {"error_class":"internal"}，不含 panic 值、栈、文件路径
//     （验收 #3/#4：未知异常不泄露内部信息）；
//   - 日志：panic 值 + 完整调用栈全量落服务端日志（验收 #3 后半句）。
//
// 若 panic 发生时响应头已写出（流式响应中途崩溃），状态码已无法更改，
// 只记日志不再补写——绝不对半截响应做二次 WriteHeader。
func Recover(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		iw := &interceptWriter{ResponseWriter: w}
		defer func() {
			rec := recover()
			if rec == nil {
				return
			}
			log.Printf("panic recovered kind=%T err=%v\n%s", rec, rec, debug.Stack())
			if !iw.wroteHeader {
				WriteError(iw, http.StatusInternalServerError, ErrorClassInternal)
				return
			}
			log.Printf("panic recovered after-header kind=%T response-left-as-is", rec)
		}()
		next.ServeHTTP(iw, r)
	})
}

// DefaultMaxBodyBytes 是请求体上限默认值（1 MiB）。冻结契约 v1 的请求体
// 只有会话启动/作答提交两类小 JSON（详见 openapi-v1.yaml），1 MiB 已是
// 两个数量级的余量；与 T-W5-006 骨架期占位路由的 1 MiB 上限同值——该卡
// 注释中"统一请求体上限在 T-W5-008 收口"的遗留由本卡接手。
const DefaultMaxBodyBytes int64 = 1 << 20

// LimitBody 请求体上限（T-W5-006 遗留收口，http.MaxBytesReader 实现）。
//
// 采用"边界预读缓冲"而非把 MaxBytesReader 丢给 handler 自行踩错：超限的
// 413 判定确定性发生在边界层，业务 handler 拿到的 Body 永远完整且小于
// 上限，不再各自处理"读一半失败"的分支；缓冲上界即 maxBytes，内存有界。
// 无体请求（GET/HEAD 探针）零读取直通。
//
// 读失败分流：*http.MaxBytesError → 413；其余（客户端中断/畸形流）→
// 400——无体可用时继续放行只会把半截输入漏给业务层，fail-closed。
func LimitBody(maxBytes int64) func(http.Handler) http.Handler {
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			if r.Body == nil {
				next.ServeHTTP(w, r)
				return
			}
			r.Body = http.MaxBytesReader(w, r.Body, maxBytes)
			body, err := io.ReadAll(r.Body)
			if err != nil {
				var tooLarge *http.MaxBytesError
				if errors.As(err, &tooLarge) {
					// limit 是服务端自配值，非请求派生数据，可安全入日志
					log.Printf("body limit exceeded class=%q limit=%d", ErrorClassPayloadTooLarge, tooLarge.Limit)
					WriteError(w, http.StatusRequestEntityTooLarge, ErrorClassPayloadTooLarge)
					return
				}
				log.Printf("body read failure class=%q reason_class=%T", ErrorClassBadRequest, err)
				WriteError(w, http.StatusBadRequest, ErrorClassBadRequest)
				return
			}
			r.Body = io.NopCloser(bytes.NewReader(body))
			next.ServeHTTP(w, r)
		})
	}
}

// MaxBodyBytesFromEnv 读 API_MAX_BODY_BYTES；未配置/非法/非正值回落默认
// （get 注入便于测试）。非法值宁可回落也不带病启动：体限是防护栏，不是
// 精确业务参数。
func MaxBodyBytesFromEnv(get func(string) string) int64 {
	const envMaxBody = "API_MAX_BODY_BYTES"
	raw := get(envMaxBody)
	if raw == "" {
		return DefaultMaxBodyBytes
	}
	v, err := strconv.ParseInt(raw, 10, 64)
	if err != nil || v <= 0 {
		log.Printf("body limit config ignored env=%s reason=invalid-value", envMaxBody)
		return DefaultMaxBodyBytes
	}
	return v
}
