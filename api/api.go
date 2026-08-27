// Package api 是 W5-R 的路由骨架：HTTP 边界装配点。
//
// 职责边界（ADR-0004 §三）：api 只做协议层（路由/错误映射/中间件），
// 业务语义全部下沉 core/；api 不出现任何学科概念（X6 同样适用于本层：
// 学科差异经 registry 的条目参数表达，不经 if-subject 分支表达）。
//
// 认证接线（T-W5-006，宪法 D9/X13）：除健康探针白名单外，每一条业务
// 路由都经 middleware.RequireAuth 挂上已认证主体——不存在无主体端点；
// 学生数据端点在 handler 内调 auth.AssertOwnsAlias 做主体↔alias 绑定校验
// （铁律 D9：学生只能读写自身 student_alias_id 关联的数据）。骨架期尚无
// 业务实现的路由显式返回 501 not_implemented 占位：认证先行、业务后补，
// 绝不以"功能未实现"为由跳过认证。
//
// T-W5-008 边界加固：全部请求经固定链序的边界层（见 boundary.go 的顺序
// 论证）——panic 收敛、CORS 白名单、IP 维度限流、请求体上限；健康探针
// 豁免限流。与 006 的合流分工：路由表（route{shield}，全端点挂认证盾）
// 构造 mux 内圈，边界层包外圈，NewRouter 对外返回 http.Handler。健康
// 探针语义对齐 T-W0-010：成功 200 {"status":"ok"}；异常路径只暴露
// error_class，堆栈与内部地址仅进服务端日志。契约中的 GET /health 与
// /healthz 是同一存活探针语义（健康检查属 D9 明文豁免），对外统一路径
// 在本卡收口（见 healthHandler）。
package api

import (
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net/http"

	"github.com/Cloudbird-Software/AI_Web_School/api/middleware"
	"github.com/Cloudbird-Software/AI_Web_School/core/auth"
)

// healthResponse 是健康探针的响应体（字段最小化，脱敏原则同 Python 版）。
type healthResponse struct {
	Status string `json:"status"`
}

// ErrorClassNotImplemented 是骨架期占位路由的对外错误类：认证与主体绑定
// 已闭合、业务实现未到位。与 401/403 相同的单字段脱敏形态。
const ErrorClassNotImplemented = "not_implemented"

// maxPlaceholderBodyBytes 限制占位路由读取请求体的字节数：POST /sessions
// 需要 peek 请求体里的 alias 字段做越权比对，先卡长度防止超大体量输入进入
// 解码路径。统一请求体上限在 T-W5-008 API 加固卡收口。
const maxPlaceholderBodyBytes = 1 << 20

// errClass 返回异常类名（脱敏：不含消息与堆栈，对齐 py/stack-trace-exposure 语义）。
func errClass(err error) string {
	if err == nil {
		return ""
	}
	return fmt.Sprintf("%T", err)
}

// healthHandler 是存活探针本体，无 DB 依赖、零业务触达。
//
// T-W5-006 遗留收口（本卡）：冻结契约（specs/contracts/api/openapi-v1.yaml）
// 的探针路径是 GET /health，骨架期只挂了 /healthz——现以契约为对外统一
// 路径；/healthz 保留为编排惯例别名，同一 handler 同一响应，语义零分叉。
// 两路径同在限流豁免白名单（boundary.go scopeOf）。
//
// 白名单豁免理由（宪法 D9 明文，006 惯例保留）：编排器/负载均衡必须能在
// 无凭证条件下探测进程存活——探针若要求认证，探活失败本身会被误判为
// 服务不可用。它不触达任何业务数据，不构成 X13 的"无主体数据访问路径"。
func healthHandler(w http.ResponseWriter, _ *http.Request) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusOK)
	// 写失败只记服务端日志，不向响应注入内部错误细节
	if err := json.NewEncoder(w).Encode(healthResponse{Status: "ok"}); err != nil {
		log.Printf("healthz encode error_class=%s", errClass(err))
	}
}

// routes 返回契约 v1 的全部业务端点接线表（specs/contracts/api/openapi-v1.yaml，
// 冻结快照；契约不改，只按其面接线）。角色矩阵与主体绑定说明：
//
//   - 内容资产/门证书只读查询（items/item_versions/templates/gate_certificates）：
//     教研（staff）与运维（ops）的生产域查询面；学生消费题目只能经会话链路。
//   - 会话生命周期（sessions 族）：学生作答域，仅学生主体；创建时的学生身份
//     取自令牌主体（见 createSession——铁律"不再信任请求体传入的 alias"）。
//   - 弱项报告与复习队列（reports/review）：alias 寻址的学生数据，经
//     AssertOwnsAlias 强制 path 中 alias 与令牌主体一致（D9 机器强制）。
//   - service 主体是内部作业调用方，API 业务面暂无其路由需求；不给默认放行，
//     后续批量作业需要时按最小权限逐路由授权。
//
// route 类型与 shield 惯例见 boundary.go。注册路径唯一经 newRouterWithConfig
// 遍历本表完成，新增端点漏加认证即无法登记（结构保证），匿名扫描测试再从
// 冻结契约侧独立复核（P3）。
func routes(signer *auth.Signer) []route {
	staffOrOps := middleware.RequireAuth(signer, auth.RoleStaff, auth.RoleOps)
	student := middleware.RequireAuth(signer, auth.RoleStudent)
	return []route{
		{pattern: "GET /items/{item_id}", shield: staffOrOps, handle: notImplemented},
		{pattern: "GET /item_versions/{item_version_id}", shield: staffOrOps, handle: notImplemented},
		{pattern: "GET /templates/{template_id}", shield: staffOrOps, handle: notImplemented},
		{pattern: "GET /gate_certificates/{cert_id}", shield: staffOrOps, handle: notImplemented},

		{pattern: "POST /sessions", shield: student, handle: createSession},
		{pattern: "GET /sessions/{session_id}", shield: student, handle: sessionScoped},
		{pattern: "GET /sessions/{session_id}/next", shield: student, handle: sessionScoped},
		{pattern: "POST /sessions/{session_id}/responses", shield: student, handle: sessionScoped},
		{pattern: "POST /sessions/{session_id}/resume", shield: student, handle: sessionScoped},
		{pattern: "POST /sessions/{session_id}/abandon", shield: student, handle: sessionScoped},

		{pattern: "GET /reports/weakness/{student_alias_id}", shield: student, handle: aliasBoundRead("student_alias_id")},
		{pattern: "GET /review/due/{student_alias_id}", shield: student, handle: aliasBoundRead("student_alias_id")},
	}
}

// NewRouter 生产装配：环境变量注入边界配置（见 boundary.go），006 的
// routes(signer) 路由表构造 mux 内圈、008 的边界层包外圈（合流形态）。
// signer 由入口进程装配期提供（core/auth.EnsureSigner），nil 属装配编程
// 错误（fail fast，与 middleware.RequireAuth 同一纪律）。独立函数便于
// httptest 集成测试。
func NewRouter(signer *auth.Signer) http.Handler {
	return newRouterWithConfig(BoundaryConfigFromEnv(getenv), routes(signer)...)
}

// newRouterWithConfig 构造带边界层的路由：注册健康探针与 extra 路由
// （生产传 routes(signer) 全表，每条经 route.shield 挂认证盾——X13 的
// 结构保证；测试可注入演示路由），再按固定链序包上边界层（withBoundary）。
func newRouterWithConfig(cfg BoundaryConfig, extra ...route) http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /health", healthHandler)
	mux.HandleFunc("GET /healthz", healthHandler)
	for _, rt := range extra {
		mux.Handle(rt.pattern, rt.shield(rt.handle))
	}
	limiter := middleware.NewRateLimiter(cfg.Rate, nil)
	return withBoundary(cfg, limiter, mux)
}

// notImplemented 是骨架期业务占位：认证与主体绑定已完成，业务实现未到位。
// 显式 501 而不是回伪造数据/空数据——fail-closed，且对外暴露的信息只有
// error_class 一个字段（与其他认证错误同构，不给探测者额外反馈通道）。
func notImplemented(w http.ResponseWriter, _ *http.Request) {
	writeErrorClass(w, http.StatusNotImplemented, ErrorClassNotImplemented)
}

// sessionScoped 是 /sessions/{session_id} 子资源的骨架占位。
//
// 已机器强制：仅学生主体可达（RequireAuth(RoleStudent)）。待业务层落地时
// 必须执行的第二道校验在此留痕：按 session_id 读会话后断言会话归属
// （会话.student_alias_id == p.AliasID，从 FromContext 取主体）才可继续
// ——那是数据访问层面的归属判定，需 DB 读支持，属业务波次卡的验收项；
// 本占位在任何数据读取之前就返回 501，今天没有泄露面。
func sessionScoped(w http.ResponseWriter, r *http.Request) {
	notImplemented(w, r)
}

// aliasBoundRead 为 alias 寻址的学生数据端点生成 handler：从路径取出
// 目标 alias，与令牌主体做绑定校验（AssertOwnsAlias——铁律 D9 的机器
// 强制：学生只能访问自己 alias 的数据；staff/ops/service 不在本类路由的
// 授权面上，角色检查已于中间件拒绝）。校验失败统一走脱敏映射。
func aliasBoundRead(aliasVar string) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		p, ok := middleware.FromContext(r.Context())
		if !ok {
			// 纵深防御：中间件保证存在主体，到这里缺失说明装配被破坏，
			// 按"身份不可信"处理（fail-closed），不当成可继续的业务态。
			log.Printf("auth denied class=%q reason=principal-missing path=%s", middleware.ErrorClassUnauthorized, r.URL.Path)
			writeErrorClass(w, http.StatusUnauthorized, middleware.ErrorClassUnauthorized)
			return
		}
		if err := auth.AssertOwnsAlias(p, r.PathValue(aliasVar)); err != nil {
			middleware.WriteAuthErrorResponse(w, err)
			return
		}
		notImplemented(w, r)
	}
}

// createSession 是 POST /sessions 的骨架占位。铁律（T-W5-006 验收 #3）：
// 会话相关端点从令牌主体取 student_alias_id，**不信任请求体传入的 alias**
// 作为授权输入。业务未落地期间能机器强制的是反向判据——请求体若显式给出
// 与主体不一致的 student_alias_id，即跨学生代建尝试，按越权 403 拒绝；
// 其余情况一律 501 留给业务实现接管（届时以 p.AliasID 为唯一身份来源，
// 请求体字段仅作契约形状解析、绝不作为授权依据）。
func createSession(w http.ResponseWriter, r *http.Request) {
	p, ok := middleware.FromContext(r.Context())
	if !ok {
		// 纵深防御，同 aliasBoundRead。
		log.Printf("auth denied class=%q reason=principal-missing path=%s", middleware.ErrorClassUnauthorized, r.URL.Path)
		writeErrorClass(w, http.StatusUnauthorized, middleware.ErrorClassUnauthorized)
		return
	}
	var req struct {
		StudentAliasID string `json:"student_alias_id"`
	}
	body, err := io.ReadAll(io.LimitReader(r.Body, maxPlaceholderBodyBytes+1))
	if err == nil && len(body) <= maxPlaceholderBodyBytes {
		// 解析失败/字段缺省留给业务层的契约校验（T-W5-008 错误映射统一）；
		// 此处只关心"能确凿读出的 alias 是否冒用他人身份"。
		if jerr := json.Unmarshal(body, &req); jerr == nil && req.StudentAliasID != "" {
			if aerr := auth.AssertOwnsAlias(p, req.StudentAliasID); aerr != nil {
				middleware.WriteAuthErrorResponse(w, aerr)
				return
			}
		}
	} else if err != nil {
		log.Printf("create_session body read error_class=%s", errClass(err))
	}
	notImplemented(w, r)
}

// writeErrorClass 输出单字段脱敏 JSON 错误响应（body 编码失败无降级通道，
// 记日志留痕，惯例同 /healthz 与 middleware.WriteError）。
func writeErrorClass(w http.ResponseWriter, status int, class string) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	if err := json.NewEncoder(w).Encode(struct {
		ErrorClass string `json:"error_class"`
	}{ErrorClass: class}); err != nil {
		log.Printf("error response encode failure class=%T", err)
	}
}
