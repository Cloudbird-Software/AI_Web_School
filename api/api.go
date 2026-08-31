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
// 内容只读四端点接线（GO-RW-001）：items/item_versions/templates/
// gate_certificates 经 ContentQueries 接口注入 core/content 取证面——
// api 只做协议层（鉴权后取路径 id → 查询 → 契约 JSON 直出/脱敏错误映射），
// 查询面未注入时四端点保持 501 占位（装配语义，认证盾照挂）。
//
// T-W5-008 边界加固：全部请求经固定链序的边界层（见 boundary.go 的顺序
// 论证）——panic 收敛、CORS 白名单、IP 维度限流、请求体上限；健康探针
// 豁免限流。与 006 的合流分工：路由表（route{shield}，全端点挂认证盾）
// 构造 mux 内圈，边界层包外圈，NewRouter 对外返回 http.Handler。健康
// 探针语义对齐 T-W0-010：成功 200 {"status":"ok"}；异常路径只暴露
// error_class，堆栈与内部地址仅进服务端日志。契约中的 GET /health 与
// /healthz 是同一存活探针语义（健康检查属 D9 明文豁免），对外统一路径
// 在本卡收口（见 healthHandler）。
//
// T-W5-010 家长授权接入在线会话入口（宪法红线「家长授权前置」/ X12）：
// POST /sessions 从「认证通过→501」升级为「认证通过→越权判据→授权检查→
// 业务占位」。授权门下沉 core/session（业务规则）+ core/compliance（授权
// 账语义），本层只做装配与错误映射。授权账经 NewRouterWithConsent 以
// compliance.ConsentStore 接口注入；未注入（nil）时授权门 fail-closed
// 500——线上没有授权账就绝不发在线会话，宁可拒服务不可降级（X12）。
//
// GO-RW-002 会话全链路业务接线：sessions 族路由在 NewRouterWithSessions
// 装配下从占位升级为 core/session.Service 的真实行为（契约 v1.1 JSON），
// 会话归属断言（D9 第二道校验）随服务面落地；svc 未注入的调用方保留
// 501 兼容形态。处理序与错误映射见 api/sessions.go.
package api

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"net/http"

	"github.com/Cloudbird-Software/AI_Web_School/api/middleware"
	"github.com/Cloudbird-Software/AI_Web_School/core/auth"
	"github.com/Cloudbird-Software/AI_Web_School/core/compliance"
	"github.com/Cloudbird-Software/AI_Web_School/core/content"
	"github.com/Cloudbird-Software/AI_Web_School/core/session"
)

// healthResponse 是健康探针的响应体（字段最小化，脱敏原则同 Python 版）。
type healthResponse struct {
	Status string `json:"status"`
}

// ErrorClassNotImplemented 是骨架期占位路由的对外错误类：认证与主体绑定
// 已闭合、业务实现未到位。与 401/403 相同的单字段脱敏形态。
const ErrorClassNotImplemented = "not_implemented"

// ContentQueries 是 api 对内容只读查询面的最小消费接口（consumer-side：
// 在消费点按需声明，core/content.ContentQueryService 天然满足——编译锚见
// 下方 var _）。GO-RW-001 的四条内容只读端点面向本接口编程，测试注入
// Memory fake，生产注入 NewContentQueryService(pgxpool)（W6 接池装配）。
type ContentQueries interface {
	GetItem(ctx context.Context, itemID string) (*content.ItemDetail, error)
	GetItemVersion(ctx context.Context, itemVersionID string) (*content.ItemVersionView, error)
	GetTemplate(ctx context.Context, templateID string) (*content.TemplateDetail, error)
	GetGateCertificate(ctx context.Context, certID string) (*content.GateCertificateDetail, error)
}

// 编译期锚定：领域实现即消费端口（W6 装配直通的假设防线）.
var _ ContentQueries = (*content.ContentQueryService)(nil)

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
//     GO-RW-001 起经 ContentQueries 注入真实取证（见 routesWithConsent /
//     NewRouterWithQueries）；本函数不注入，四端点保持 501 占位。
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
//
// T-W5-010 授权门覆盖面（论证见 createSession 与 sessionScoped）：授权检查
// 只挂会话**创建**入口 POST /sessions——任务卡验收 #1 的原文是「会话创建
// ……前置校验」，resume/next/状态读取等子资源路由不在卡面要求内，且骨架期
// 它们在任何数据读取前就 501，无可泄露面；作答提交的二次校验（卡验收 #2）
// 以会话归属读取为前提，属业务波次落地项（sessionScoped 留痕）。
func routes(signer *auth.Signer) []route {
	return routesWithConsent(signer, nil, nil, nil, nil, LearnerReads{}, PapersWiring{})
}

// routesWithConsent 在 routes 之上注入家长授权账、内容只读查询面、会话
// 全链路服务面与学生只读面：POST /sessions 的 handler 捕获 store（闭包接缝），
// 四条内容只读端点经 contentRead 捕获 queries，sessions 族经 svc 捕获
// SessionService，reports/review 两条学生只读端点经 reads 捕获查询面；
// queries/svc/reads 字段为 nil 时对应端点保持 501 占位（装配语义，认证盾照挂）。
// papers 聚合组卷/出题三端（#148）的接缝，语义同上（见 papers.go）。
func routesWithConsent(signer *auth.Signer, store compliance.ConsentStore, queries ContentQueries, svc *session.Service, scorer ResponseScorer, reads LearnerReads, papers PapersWiring) []route {
	staffOrOps := middleware.RequireAuth(signer, auth.RoleStaff, auth.RoleOps)
	student := middleware.RequireAuth(signer, auth.RoleStudent)
	itemHandle, itemVersionHandle := notImplemented, notImplemented
	templateHandle, certHandle := notImplemented, notImplemented
	if queries != nil {
		itemHandle = contentRead("item_id", queries.GetItem)
		itemVersionHandle = contentRead("item_version_id", queries.GetItemVersion)
		templateHandle = contentRead("template_id", queries.GetTemplate)
		certHandle = contentRead("cert_id", queries.GetGateCertificate)
	}
	// 未注入查询面时保持 aliasBoundRead：归属断言（D9）照常执行后才落 501
	// 占位——骨架语义不因装配降级而弱化主体↔alias 绑定.
	learnerWeaknessHandle, learnerDueHandle := aliasBoundRead("student_alias_id"), aliasBoundRead("student_alias_id")
	if reads.Reports != nil {
		learnerWeaknessHandle = reportsWeakness(reads)
	}
	if reads.Review != nil {
		learnerDueHandle = reviewDue(reads)
	}
	paperCreate, paperRead, paperGenerate := papersHandlers(papers)
	return []route{
		{pattern: "GET /items/{item_id}", shield: staffOrOps, handle: itemHandle},
		{pattern: "GET /item_versions/{item_version_id}", shield: staffOrOps, handle: itemVersionHandle},
		{pattern: "GET /templates/{template_id}", shield: staffOrOps, handle: templateHandle},
		{pattern: "GET /gate_certificates/{cert_id}", shield: staffOrOps, handle: certHandle},

		// 组卷/出题三端（#148）：教研/运维的生产域（学生消费题目只能经会话
		// 链路），角色面与内容只读四端点同构.
		{pattern: "POST /papers", shield: staffOrOps, handle: paperCreate},
		{pattern: "GET /papers/{paper_id}", shield: staffOrOps, handle: paperRead},
		{pattern: "POST /generate", shield: staffOrOps, handle: paperGenerate},

		{pattern: "POST /sessions", shield: student, handle: createSession(store, svc)},
		{pattern: "GET /sessions/{session_id}", shield: student, handle: sessionState(svc)},
		{pattern: "GET /sessions/{session_id}/next", shield: student, handle: sessionNext(svc)},
		{pattern: "POST /sessions/{session_id}/responses", shield: student, handle: sessionSubmit(svc, scorer, reads.Sync)},
		{pattern: "POST /sessions/{session_id}/resume", shield: student, handle: sessionResume(svc)},
		{pattern: "POST /sessions/{session_id}/abandon", shield: student, handle: sessionAbandon(svc)},

		{pattern: "GET /reports/weakness/{student_alias_id}", shield: student, handle: learnerWeaknessHandle},
		{pattern: "GET /review/due/{student_alias_id}", shield: student, handle: learnerDueHandle},
	}
}

// NewRouterWithSessions 是 GO-RW-002 的生产装配接缝：在 NewRouterWithConsent
// 之上注入会话全链路服务（core/session.Service）与作答评分桥，sessions 族
// 路由返回契约 v1.1 JSON。授权门前置不变；svc 为 nil 时等价于
// NewRouterWithConsent（501 兼容形态）.
func NewRouterWithSessions(signer *auth.Signer, store compliance.ConsentStore, svc *session.Service, scorer ResponseScorer) http.Handler {
	return newRouterWithConfig(BoundaryConfigFromEnv(getenv), routesWithConsent(signer, store, nil, svc, scorer, LearnerReads{}, PapersWiring{})...)
}

// NewRouterWithLearnerReads 是学生只读面（弱项报告/复习到期）的生产装配
// 接缝：在 NewRouterWithSessions 之上注入 reads。reads 各字段为 nil 时对应
// 端点保持 501 占位（fail-closed：查询面未接线绝不回伪造/空数据）。
func NewRouterWithLearnerReads(signer *auth.Signer, store compliance.ConsentStore, svc *session.Service, scorer ResponseScorer, queries ContentQueries, reads LearnerReads) http.Handler {
	return newRouterWithConfig(BoundaryConfigFromEnv(getenv), routesWithConsent(signer, store, queries, svc, scorer, reads, PapersWiring{})...)
}

// NewRouterWithPapers 是组卷/出题面（#148）的生产装配接缝：在
// NewRouterWithLearnerReads 之上注入 PapersWiring（编排器 + 制品存储）。
// wiring 各依赖未注入时对应端点保持 501 占位（fail-closed：候选题源或
// 制品账未接线绝不回伪造卷面）.
func NewRouterWithPapers(signer *auth.Signer, store compliance.ConsentStore, svc *session.Service, scorer ResponseScorer, queries ContentQueries, reads LearnerReads, wiring PapersWiring) http.Handler {
	return newRouterWithConfig(BoundaryConfigFromEnv(getenv), routesWithConsent(signer, store, queries, svc, scorer, reads, wiring)...)
}

// NewRouter 生产装配：环境变量注入边界配置（见 boundary.go），006 的
// routes(signer) 路由表构造 mux 内圈、008 的边界层包外圈（合流形态）。
// signer 由入口进程装配期提供（core/auth.EnsureSigner），nil 属装配编程
// 错误（fail fast，与 middleware.RequireAuth 同一纪律）。独立函数便于
// httptest 集成测试。
//
// 本签名下授权账未注入：POST /sessions 的授权门 fail-closed（500）——
// 保留兼容是给不触达会话入口的既有调用方/测试；需要在线会话入口的
// 装配必须走 NewRouterWithConsent。
func NewRouter(signer *auth.Signer) http.Handler {
	return NewRouterWithConsent(signer, nil)
}

// NewRouterWithConsent 是 T-W5-010 的装配接缝（形态对齐 008 的 Config 注入
// 风格：NewRouter 原签名不动，依赖以 With 变体显式注入）。store 为 nil 属
// "授权基础设施未接线"：授权门 fail-closed 拒绝会话创建，绝不放行（X12）；
// 生产注入 compliance.PGStore（W6 接事务执行面），测试注入 MemoryStore.
// 内容只读查询面在本签名下未注入（四端点 501 占位），需要真实取证走
// NewRouterWithQueries。
func NewRouterWithConsent(signer *auth.Signer, store compliance.ConsentStore) http.Handler {
	return NewRouterWithQueries(signer, store, nil)
}

// NewRouterWithQueries 是 GO-RW-001 的装配接缝：在授权账之上注入内容只读
// 查询面（core/content.ContentQueryService 绑定 pgxpool 的生产形态）。queries
// 为 nil 等价 NewRouterWithConsent——四条只读端点保持 501 占位（查询面未
// 接线绝不回伪造/空数据，fail-closed 同 X12 纪律）。
func NewRouterWithQueries(signer *auth.Signer, store compliance.ConsentStore, queries ContentQueries) http.Handler {
	return newRouterWithConfig(BoundaryConfigFromEnv(getenv), routesWithConsent(signer, store, queries, nil, nil, LearnerReads{}, PapersWiring{})...)
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

// contentRead 把一次内容取数闭包成只读 handler（GO-RW-001 四端点共用形态；
// 路径变量名与冻结契约保持一致，由调用方钉入）。响应序：
//
//	200 → 契约 JSON（core/content 视图直出，本层零业务语义）
//	404 → not_found（"账面无行"哨兵集合，单字段脱敏）
//	500 → internal（其余错误一律归一：原始错误只进服务端日志，绝不外泄
//	      消息/驱动细节——与认证错误同构的脱敏形态）
func contentRead[T any](idVar string, fetch func(ctx context.Context, id string) (T, error)) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		v, err := fetch(r.Context(), r.PathValue(idVar))
		if err != nil {
			if contentUnknownRow(err) {
				writeErrorClass(w, http.StatusNotFound, middleware.ErrorClassNotFound)
				return
			}
			// 日志只落 route pattern（服务端路由表常量）——r.URL.Path 是
			// 请求方可控字节，落日志即注入面（CodeQL go/log-injection
			// #29/#32/#33 的修法：请求派生值一律不入日志）。
			log.Printf("content query failure route=%s error_class=%s", r.Pattern, errClass(err))
			writeErrorClass(w, http.StatusInternalServerError, middleware.ErrorClassInternal)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusOK)
		if encErr := json.NewEncoder(w).Encode(v); encErr != nil {
			log.Printf("content response encode failure error_class=%T", encErr)
		}
	}
}

// contentUnknownRow 判定错误是否属"账面无行"哨兵集合（→404 的唯一映射面）；
// 哨兵清单收口在 core/content，本层逐项 errors.Is 归一，不许字符串匹配.
func contentUnknownRow(err error) bool {
	for _, sentinel := range []error{
		content.ErrUnknownItem,
		content.ErrUnknownItemVersion,
		content.ErrUnknownTemplate,
		content.ErrUnknownGateCertificate,
	} {
		if errors.Is(err, sentinel) {
			return true
		}
	}
	return false
}

// sessionScoped 是 /sessions/{session_id} 子资源的骨架占位。
//
// 已机器强制：仅学生主体可达（RequireAuth(RoleStudent)）。待业务层落地时
// 必须执行的第二道校验在此留痕：按 session_id 读会话后断言会话归属
// （会话.student_alias_id == p.AliasID，从 FromContext 取主体）才可继续
// ——那是数据访问层面的归属判定，需 DB 读支持，属业务波次卡的验收项；
// 本占位在任何数据读取之前就返回 501，今天没有泄露面。
//
// T-W5-010 授权门覆盖面论证：任务卡验收 #2 要求「提交作答时二次校验授权
// 仍有效（撤回后立即失效）」——该二次校验的正确形态是按会话归属读出
// alias 后查授权账，与会话归属断言同源（同一 DB 读），因此属于提交业务
// 落地波次（T-W5-018）的验收项，随作答写入路径一并实现并补零写入断言；
// 骨架期在此挂一个基于 p.AliasID 的预检只会得到假保护（会话归属未断言，
// 校验对象可能是别人的会话），且对外行为与卡面要求无对应关系。resume/next
// 等读取/恢复路由卡面未要求授权检查，不在本卡范围。
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
			log.Printf("auth denied class=%q reason=principal-missing route=%s", middleware.ErrorClassUnauthorized, r.Pattern) // route=服务端常量，禁落 r.URL.Path（go/log-injection）
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

// createSession（POST /sessions）已随 GO-RW-002 升级为业务接线，本体与
// 处理序注释移至 api/sessions.go（认证 → 越权判据 → 授权门 → SessionService，
// 顺序即安全论证，不得重排）。

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
