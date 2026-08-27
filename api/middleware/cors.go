// cors.go 承载 T-W5-008 的 CORS 白名单中间件。
//
// 语义基准：Python 冻结实现（src/api）从未配置 CORS——即"默认全拒跨域"。
// 本实现把这一隐式默认显式化：白名单来自环境变量注入，默认空 = 拒绝一切
// 跨域请求（验收 #1，fail-closed）。带凭据的响应永不使用通配符 "*"：
// 本中间件只做"白名单命中 → 精确回显该 Origin"，从结构上排除了
// Access-Control-Allow-Origin: * 的出现路径。
package middleware

import (
	"log"
	"net/http"
	"strings"
)

// CORS 环境变量名（.env.example 有对应占位；默认空即全拒跨域）。
const (
	// EnvCORSAllowedOrigins 逗号分隔的 Origin 白名单，如
	// "https://school.example.com,https://admin.example.com"。
	EnvCORSAllowedOrigins = "API_CORS_ALLOWED_ORIGINS"
	// EnvCORSAllowCredentials 是否允许携带凭据（cookie/Authorization）。
	EnvCORSAllowCredentials = "API_CORS_ALLOW_CREDENTIALS"
)

// CORSConfig 是跨域白名单配置。零值即生产默认：白名单空 + 不带凭据。
type CORSConfig struct {
	// AllowedOrigins 是允许跨域的 Origin 全量列表（scheme://host[:port]）。
	AllowedOrigins []string
	// AllowCredentials 显式凭据模式：仅当为 true 才输出
	// Access-Control-Allow-Credentials: true。凭据模式必须是显式开关，
	// 不允许"有白名单就默认带凭据"的隐式推断。
	AllowCredentials bool
}

// CORSConfigFromEnv 从环境读配置；get 注入便于测试。
// 空项/纯逗号/空白项忽略；未配置 = 白名单空 = 全拒跨域。
func CORSConfigFromEnv(get func(string) string) CORSConfig {
	var cfg CORSConfig
	for _, o := range strings.Split(get(EnvCORSAllowedOrigins), ",") {
		if o = strings.TrimSpace(o); o != "" {
			cfg.AllowedOrigins = append(cfg.AllowedOrigins, o)
		}
	}
	switch strings.ToLower(strings.TrimSpace(get(EnvCORSAllowCredentials))) {
	case "1", "true", "yes", "on":
		cfg.AllowCredentials = true
	}
	return cfg
}

// allows 判定请求 Origin 是否命中白名单。按 RFC 6454，Origin 只有
// scheme/host/port 三段（无路径、无查询），大小写不敏感比较是安全侧：
// 多匹配只会放行"同一站点的另一种大小写拼写"，不会放行不同站点。
func (c CORSConfig) allows(origin string) bool {
	for _, allowed := range c.AllowedOrigins {
		if strings.EqualFold(allowed, origin) {
			return true
		}
	}
	return false
}

// CORS 返回跨域白名单中间件。
//
// 行为矩阵：
//
//   - 无 Origin 头（同源/非浏览器）：直接放行，不下发任何 CORS 头；
//   - Origin 命中白名单：回显 ACAO（+ 显式凭据头），继续后续链条；
//   - Origin 未命中：403 + {"error_class":"origin_forbidden"}，请求不
//     触达 handler——预检与实际请求同一待遇（验收 #1 跨域拒绝）。
//
// 预检（OPTIONS + Access-Control-Request-Method）命中白名单时以 204 短路，
// 允许方法/头是固定保守集合（GET/HEAD/POST/OPTIONS；Authorization、
// Content-Type），不回显请求所求——回显会把任意方法/头都"允许"掉。
func CORS(cfg CORSConfig) func(http.Handler) http.Handler {
	allowMethods := strings.Join([]string{
		http.MethodGet, http.MethodHead, http.MethodPost, http.MethodOptions,
	}, ", ")
	const allowHeaders = "Authorization, Content-Type"
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			// 本中间件按请求 Origin 动态决定响应内容，必须声明 Vary: Origin，
			// 否则共享缓存可能把"放行源 A 的响应"回放给"源 B 的请求"。
			setVaryOrigin(w)
			origin := r.Header.Get("Origin")
			if origin == "" {
				next.ServeHTTP(w, r)
				return
			}
			if !cfg.allows(origin) {
				// preflight 布尔值由服务端判定，非请求派生文本，可安全入日志；
				// 请求 Origin 本身不进日志（log-injection 纪律，同 T-W5-006）。
				log.Printf("cors rejected class=%q preflight=%v", ErrorClassOriginForbidden, isPreflight(r))
				WriteError(w, http.StatusForbidden, ErrorClassOriginForbidden)
				return
			}
			w.Header().Set("Access-Control-Allow-Origin", origin)
			if cfg.AllowCredentials {
				w.Header().Set("Access-Control-Allow-Credentials", "true")
			}
			if isPreflight(r) {
				w.Header().Set("Access-Control-Allow-Methods", allowMethods)
				w.Header().Set("Access-Control-Allow-Headers", allowHeaders)
				w.Header().Set("Access-Control-Max-Age", "600")
				w.WriteHeader(http.StatusNoContent)
				return
			}
			next.ServeHTTP(w, r)
		})
	}
}

// isPreflight 判定 CORS 预检请求（WHATWG Fetch 规范：OPTIONS 方法且携带
// Access-Control-Request-Method 头）。
func isPreflight(r *http.Request) bool {
	return r.Method == http.MethodOptions && r.Header.Get("Access-Control-Request-Method") != ""
}

// setVaryOrigin 追加（而非覆盖）Vary: Origin，尊重下游已声明的其他 Vary 值。
func setVaryOrigin(w http.ResponseWriter) {
	const origin = "Origin"
	switch existing := w.Header().Get("Vary"); {
	case existing == "":
		w.Header().Set("Vary", origin)
	case !strings.Contains(existing, origin):
		w.Header().Set("Vary", existing+", "+origin)
	}
}
