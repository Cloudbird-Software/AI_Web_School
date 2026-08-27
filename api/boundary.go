// boundary.go 是 T-W5-008 的边界装配点：把 CORS / 限流 / 请求体上限 /
// panic 防线按固定序包在路由外圈，配置统一经环境变量注入。
//
// 固定链序（有注释论证，不得重排）：
//
//		recover → cors → rate-limit(IP) → body-limit → mux（路由级：auth → 主体配额 → handler）
//
//	  - recover 最外层：链条任何一环（含边界件自身）panic 都收敛为脱敏
//	    500 + 服务端全量日志，绝无裸 panic 出进程；
//	  - cors 次外层：跨域判定先于一切计费——被拒的跨域探测不消耗限流
//	    令牌、不触达任何业务面，也不借 429 泄露"限流器存在"的信号；
//	  - rate-limit 先于 auth：匿名洪水在签名验算/令牌解析等认证开销之前
//	    被配额拦下（DoS 面最小化）；健康探针经 scope 分类豁免（验收 #5）；
//	  - body-limit 介于限流与 auth 之间：洪水先被限流掐灭，幸存者的超大
//	    体量在缓冲（至多 MaxBodyBytes，内存有界）阶段即被 413 拒绝，不进入
//	    认证更不进入业务解析——T-W5-006 遗留的"统一请求体上限"在此收口；
//	  - auth 与主体维度配额属路由级盾（T-W5-006 已接线的 RequireAuth +
//	    本卡新增 RateLimitPrincipal）：主体配额必须能取到主体，天然只能
//	    排在 auth 之后——这是"IP 维度在前、主体维度在后"双维度设计的一体
//	    两面，不与本层固定序冲突。
package api

import (
	"net/http"
	"os"
	"strings"

	"github.com/Cloudbird-Software/AI_Web_School/api/middleware"
)

// BoundaryConfig 是边界三件套 + 请求体上限的装配配置。
type BoundaryConfig struct {
	CORS middleware.CORSConfig
	Rate middleware.RateLimitConfig
	// MaxBodyBytes 全局请求体上限（0 = 取 middleware.DefaultMaxBodyBytes）。
	MaxBodyBytes int64
}

// BoundaryConfigFromEnv 读全部边界配置；get 注入便于测试。
func BoundaryConfigFromEnv(get func(string) string) BoundaryConfig {
	return BoundaryConfig{
		CORS:         middleware.CORSConfigFromEnv(get),
		Rate:         middleware.RateLimitConfigFromEnv(get),
		MaxBodyBytes: middleware.MaxBodyBytesFromEnv(get),
	}
}

// route 是一条已接线的业务路由声明（T-W5-006 惯例的接缝）。shield 必须
// 非 nil（路由表不允许匿名条目，X13）；认证盾与主体配额在 shield 内组合。
type route struct {
	pattern string // Go 方法+路径形态，通配段名与契约保持一致
	shield  func(http.Handler) http.Handler
	handle  http.HandlerFunc
}

// scopeOf 是请求 → 限流域的分类器。放在 api 层：路径→端点域是路由知识，
// middleware 包保持端点无关；分类键是端点域而非学科（X6：不存在也不允许
// 出现按学科的分支）。
//
//   - /health、/healthz：存活探针豁免（验收 #5）——探针被限流误杀会被
//     编排器误判为服务不可用；
//   - POST /sessions*：作答提交写路径（冻结契约 v1 的会话族），独立配额；
//   - /reports/*、/review/*：报告与复习队列读路径，独立配额；
//   - 其余：兜底域。
func scopeOf(r *http.Request) middleware.RateScope {
	switch r.URL.Path {
	case "/health", "/healthz":
		return middleware.ScopeNone
	}
	if r.Method == http.MethodPost && strings.HasPrefix(r.URL.Path, "/sessions") {
		return middleware.ScopeSubmit
	}
	if strings.HasPrefix(r.URL.Path, "/reports/") || strings.HasPrefix(r.URL.Path, "/review/") {
		return middleware.ScopeReport
	}
	return middleware.ScopeDefault
}

// withBoundary 按固定链序包住 mux；limiter 由调用方持有（业务路由的
// 主体配额须与边界 IP 层共享同一桶存储）。
//
// 包装方向：Go 中间件"后包者先执行"，因此按链条的逆序依次包装——最终
// 执行序恰为 recover → cors → rate-limit → body-limit → mux。
func withBoundary(cfg BoundaryConfig, limiter *middleware.RateLimiter, mux *http.ServeMux) http.Handler {
	if cfg.MaxBodyBytes <= 0 {
		cfg.MaxBodyBytes = middleware.DefaultMaxBodyBytes
	}
	var h http.Handler = mux
	h = middleware.LimitBody(cfg.MaxBodyBytes)(h)   // 4) 体限（最内）
	h = middleware.RateLimitIP(limiter, scopeOf)(h) // 3) IP 限流
	h = middleware.CORS(cfg.CORS)(h)                // 2) 跨域白名单
	h = middleware.Recover(h)                       // 1) panic 防线（最外）
	return h
}

// getenv 是生产装配的环境读取口（测试走 BoundaryConfigFromEnv 注入）。
func getenv(key string) string { return os.Getenv(key) }
