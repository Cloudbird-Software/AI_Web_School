// ratelimit.go 承载 T-W5-008 的令牌桶限流（单实例级别，分布式限流集群是
// 本卡非目标）。标准库 time + sync 实现，零新依赖；时钟注入使窗口推进
// 完全可测（测试零 sleep，-race 稳定）。
//
// 双维度（验收 #2）：
//   - IP 维度（RateLimitIP，DimensionIP）：挂在认证之前——匿名洪水在签名
//     验算/令牌解析开销之前被甩掉，键为 TCP 对端主机；
//   - 主体维度（RateLimitPrincipal，DimensionPrincipal）：挂在 RequireAuth
//     之后——需要已认证主体做键，供"作答提交 / 报告查询"等端点域独立配额。
//
// 两维配额分表且量级不同：一个 IP 后面可能是一整个 NAT 办公室/校园网，
// IP 维度配额必须显著宽于单主体配额——这正是双维度设计的意义（等额的
// 双维度等于没有主体维度）。两维共用同一桶存储但键空间按维度隔离。
//
// X-Forwarded-For 不作为键：它可被任意伪造，信任代理头属 API 网关卡的范围
// （本卡 non_goals）。
package middleware

import (
	"log"
	"math"
	"net"
	"net/http"
	"strconv"
	"strings"
	"sync"
	"time"
)

// 限流环境变量名（.env.example 有对应占位；未配置走 DefaultRateLimitConfig）。
// RPM 覆盖值同时作用于两维（突发容量仍按维度的默认值，不给"拉满突发
// 变相关闭限流"留口子）。
const (
	EnvRateLimitSubmitRPM  = "API_RATE_LIMIT_SUBMIT_RPM"
	EnvRateLimitReportRPM  = "API_RATE_LIMIT_REPORT_RPM"
	EnvRateLimitDefaultRPM = "API_RATE_LIMIT_DEFAULT_RPM"
)

// RateScope 是限流命名空间：作答提交与报告查询各自独立配额（验收 #2），
// 按端点域而非 HTTP 方法粗分——读写路径的爆炸半径不同，配额必须分开。
type RateScope string

const (
	// ScopeNone 限流豁免（健康检查探针，验收 #5：编排器探活不允许被限流误杀）。
	ScopeNone RateScope = ""
	// ScopeSubmit 作答提交域（POST /sessions* 写路径）。
	ScopeSubmit RateScope = "submit"
	// ScopeReport 报告/复习查询域（GET /reports/*、/review/* 读路径）。
	ScopeReport RateScope = "report"
	// ScopeDefault 其余端点兜底域。
	ScopeDefault RateScope = "default"
)

// Dimension 是限流计费维度。
type Dimension string

const (
	// DimensionIP 边界层维度（认证前，键为 TCP 对端主机）。
	DimensionIP Dimension = "ip"
	// DimensionPrincipal 主体维度（路由盾层，键为已认证主体）。
	DimensionPrincipal Dimension = "principal"
)

// RateQuota 是一个域的令牌桶参数。
type RateQuota struct {
	// PerMinute 每分钟补充令牌数；<=0 表示该域不限流（显式关闭）。
	PerMinute int
	// Burst 桶容量（可累积的瞬时突发上限）。
	Burst int
}

// RateLimitConfig 是双维度的全量域配额表。
type RateLimitConfig struct {
	// IPScopes 边界层（认证前）IP 维度配额。
	IPScopes map[RateScope]RateQuota
	// PrincipalScopes 路由盾层（认证后）主体维度配额。
	PrincipalScopes map[RateScope]RateQuota
}

// DefaultRateLimitConfig 是未配置时的默认配额。主体维度对着冻结契约 v1
// 的调用形态（学生作答间隔秒级、报告查看低频）放宽一个数量级：正常用户
// 无感，脚本洪水在秒级被挡；IP 维度按 NAT 聚合放大，避免误伤共用出口的
// 多名学生。
func DefaultRateLimitConfig() RateLimitConfig {
	return RateLimitConfig{
		IPScopes: map[RateScope]RateQuota{
			ScopeSubmit:  {PerMinute: 120, Burst: 30},
			ScopeReport:  {PerMinute: 240, Burst: 60},
			ScopeDefault: {PerMinute: 480, Burst: 120},
		},
		PrincipalScopes: map[RateScope]RateQuota{
			ScopeSubmit:  {PerMinute: 30, Burst: 5},
			ScopeReport:  {PerMinute: 60, Burst: 10},
			ScopeDefault: {PerMinute: 120, Burst: 20},
		},
	}
}

// RateLimitConfigFromEnv 读各域 RPM 覆盖值（同时作用于两维）；未配置/
// 非法/负值回落默认。只暴露 RPM 单旋钮：Burst 与 RPM 的比例是安全参数。
func RateLimitConfigFromEnv(get func(string) string) RateLimitConfig {
	cfg := DefaultRateLimitConfig()
	set := func(scope RateScope, env string) {
		raw := strings.TrimSpace(get(env))
		if raw == "" {
			return
		}
		v, err := strconv.Atoi(raw)
		if err != nil || v < 0 {
			log.Printf("rate limit config ignored env=%s reason=invalid-value", env)
			return
		}
		ipQ, prQ := cfg.IPScopes[scope], cfg.PrincipalScopes[scope]
		ipQ.PerMinute, prQ.PerMinute = v, v
		cfg.IPScopes[scope], cfg.PrincipalScopes[scope] = ipQ, prQ
	}
	set(ScopeSubmit, EnvRateLimitSubmitRPM)
	set(ScopeReport, EnvRateLimitReportRPM)
	set(ScopeDefault, EnvRateLimitDefaultRPM)
	return cfg
}

// quotasOf 取维度配额表（nil 表 = 该维全域不限流）。
func (c RateLimitConfig) quotasOf(d Dimension) map[RateScope]RateQuota {
	switch d {
	case DimensionIP:
		return c.IPScopes
	case DimensionPrincipal:
		return c.PrincipalScopes
	default:
		return nil
	}
}

// bucket 是单个键的令牌桶状态。
type bucket struct {
	tokens float64
	last   time.Time
}

// RateLimiter 是并发安全的单实例令牌桶集合（双维度共享存储，键按
// 维度+域隔离）。
type RateLimiter struct {
	mu      sync.Mutex
	cfg     RateLimitConfig
	buckets map[string]*bucket
	now     func() time.Time
	maxKeys int
}

// NewRateLimiter 构造限流器；now 为 nil 时用真实时钟（生产路径），测试
// 注入假时钟驱动窗口推进。cfg 零值时回落默认配额。
func NewRateLimiter(cfg RateLimitConfig, now func() time.Time) *RateLimiter {
	if now == nil {
		now = time.Now
	}
	if cfg.IPScopes == nil && cfg.PrincipalScopes == nil {
		cfg = DefaultRateLimitConfig()
	}
	return &RateLimiter{
		cfg:     cfg,
		buckets: make(map[string]*bucket),
		now:     now,
		// 单实例内存护栏：IP 源有界，但伪造源仍可能撑大键空间
		maxKeys: 1 << 16,
	}
}

// Allow 对 (维度, 域, identity) 消费一枚令牌。放行返回 (true, 0)；拒绝
// 返回 (false, retryAfter)——retryAfter 是补充到 1 枚令牌所需时长的向上
// 取整，直接落 Retry-After 头（验收 #2 的"重试提示"）。
func (l *RateLimiter) Allow(dim Dimension, scope RateScope, identity string) (bool, time.Duration) {
	quotas := l.cfg.quotasOf(dim)
	l.mu.Lock()
	defer l.mu.Unlock()
	quota, ok := quotas[scope]
	if !ok || quota.PerMinute <= 0 || quota.Burst <= 0 {
		return true, 0 // 配额未定义/显式关闭的域不限流
	}
	key := string(dim) + "\x00" + string(scope) + "\x00" + identity
	now := l.now()
	b := l.buckets[key]
	if b == nil {
		l.pruneLocked(now)
		b = &bucket{tokens: float64(quota.Burst), last: now}
		l.buckets[key] = b
	}
	if elapsed := now.Sub(b.last); elapsed > 0 {
		b.tokens = math.Min(float64(quota.Burst),
			b.tokens+elapsed.Seconds()*(float64(quota.PerMinute)/60))
		b.last = now
	}
	if b.tokens >= 1 {
		b.tokens--
		return true, 0
	}
	wait := (1 - b.tokens) / (float64(quota.PerMinute) / 60)
	return false, time.Duration(math.Ceil(wait)) * time.Second
}

// pruneLocked 惰性清理：桶数触顶时丢弃 10 分钟未活跃的键，把键空间拉回
// 与活跃客户端数相当的水平。O(n) 扫描只在触顶时发生，均摊可忽略。
func (l *RateLimiter) pruneLocked(now time.Time) {
	if len(l.buckets) < l.maxKeys {
		return
	}
	const idleTTL = 10 * time.Minute
	for k, b := range l.buckets {
		if now.Sub(b.last) > idleTTL {
			delete(l.buckets, k)
		}
	}
}

// RateLimitIP 是 IP 维度边界限流。scopeOf 由装配方注入（api 层持有路由
// 知识，本包保持端点无关）；返回 ScopeNone 即豁免。
func RateLimitIP(l *RateLimiter, scopeOf func(*http.Request) RateScope) func(http.Handler) http.Handler {
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			scope := ScopeDefault
			if scopeOf != nil {
				scope = scopeOf(r)
			}
			if scope == ScopeNone {
				next.ServeHTTP(w, r)
				return
			}
			ok, retryAfter := l.Allow(DimensionIP, scope, clientIP(r))
			if !ok {
				writeRateLimited(w, retryAfter, scope)
				return
			}
			next.ServeHTTP(w, r)
		})
	}
}

// RateLimitPrincipal 是主体维度配额，挂在 RequireAuth 之后（依赖其注入
// context 的主体）。主体缺失说明装配被破坏，按"身份不可信"fail-closed
// 落 401（惯例同 api 层纵深防御分支），绝不当作匿名流量放行。
func RateLimitPrincipal(l *RateLimiter, scope RateScope) func(http.Handler) http.Handler {
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			p, ok := FromContext(r.Context())
			if !ok {
				log.Printf("rate limit denied class=%q reason=principal-missing", ErrorClassUnauthorized)
				WriteError(w, http.StatusUnauthorized, ErrorClassUnauthorized)
				return
			}
			// 主键用 SubjectID：一名学生对应一个 alias，不存在同主体多 alias
			// 场景；Role 前缀隔离复用同 id 的不同类主体
			identity := string(p.Role) + "\x00" + p.SubjectID
			ok, retryAfter := l.Allow(DimensionPrincipal, scope, identity)
			if !ok {
				writeRateLimited(w, retryAfter, scope)
				return
			}
			next.ServeHTTP(w, r)
		})
	}
}

// writeRateLimited 统一 429 输出：Retry-After 头 + 单字段脱敏体。
// 日志只含服务端判定的 scope 与秒数，不含 IP/路径等请求派生值
// （log-injection 纪律，同 T-W5-006）。
func writeRateLimited(w http.ResponseWriter, retryAfter time.Duration, scope RateScope) {
	secs := int(math.Ceil(retryAfter.Seconds()))
	if secs < 1 {
		secs = 1
	}
	w.Header().Set("Retry-After", strconv.Itoa(secs))
	log.Printf("rate limited class=%q scope=%s retry_after_sec=%d", ErrorClassRateLimited, scope, secs)
	WriteError(w, http.StatusTooManyRequests, ErrorClassRateLimited)
}

// clientIP 取 TCP 对端主机（剥掉临时端口，使同一客户端共享一个桶）。
// 只用 RemoteAddr：它由内核从连接生成、必为合法地址文本；任何请求头
// （X-Forwarded-For 等）都可被客户端任意伪造，不作为计费键。
func clientIP(r *http.Request) string {
	if host, _, err := net.SplitHostPort(r.RemoteAddr); err == nil {
		return host
	}
	return r.RemoteAddr
}
