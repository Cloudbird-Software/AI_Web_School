// Package main 是 W5-R Go 模块化单体的入口（T-W5-031 骨架；GO-RW-002 起
// 会话域生产装配就位）。
//
// 分层（ADR-0004 §三）：cmd/ 入口 · core/ 六边形核心域（零学科特判，X6/GO-3
// 由 tools/go-lint 强制） · api/ 路由 · packs/ 学科与学段包 · registry/ 双注册表
// （D4：作答交互 + 评分器）。
//
// main 只做装配：读环境、构造 server、挂 api 路由。会话全链路依赖在装配期
// 显式接线（GO-RW-002）：pgxpool（SCHOOL_DATABASE_URL）→ compliance.PGStore
// （家长授权账）+ session.PGStore（题序/提交/运行态三面同账）+ poolTxRunner
// （显式事务执行面）+ dbResponseScorer（评分桥）→ api.NewRouterWithLearnerReads
// （含 ContentQueries 与学生只读面）。
package main

import (
	"context"
	"log"
	"net/http"
	"os"
	"time"

	"github.com/Cloudbird-Software/AI_Web_School/api"
	"github.com/Cloudbird-Software/AI_Web_School/api/middleware"
	"github.com/Cloudbird-Software/AI_Web_School/core/auth"
	"github.com/Cloudbird-Software/AI_Web_School/core/compliance"
	"github.com/Cloudbird-Software/AI_Web_School/core/content"
	"github.com/Cloudbird-Software/AI_Web_School/core/report"
	"github.com/Cloudbird-Software/AI_Web_School/core/review"
	"github.com/Cloudbird-Software/AI_Web_School/core/scoring"
	"github.com/Cloudbird-Software/AI_Web_School/core/session"
	"github.com/jackc/pgx/v5/pgxpool"
)

// envVarLLMGatewayKey 是 LLM 网关角色 key 的环境变量名（docs/secrets.md §2：
// 业务进程只持角色虚拟 key，供应商真实 key 不出网关）。凭证属于 ClassLLM：
// 缺失不阻断启动，但调用期被显式拒绝。
const envVarLLMGatewayKey = "LITELLM_ROLE_KEY"

// envVarDatabaseURL 是会话域生产装配的连接串环境变量（连接串不进仓库，
// 硬规则 4；命名随 SCHOOL_* 入口惯例）。缺失即启动期硬失败：会话入口的
// 生产装配以账本存在为前提——没有账本的会话服务就是违宪装配（fail fast，
// 与 auth bootstrap 同一纪律）.
const envVarDatabaseURL = "SCHOOL_DATABASE_URL"

// envLoader 把环境变量读取闭包成登记面 Loader（map 闭包形态留给测试）。
func envLoader(key string) auth.Loader {
	return func() (auth.Secret, bool) {
		v := os.Getenv(key)
		return auth.NewSecret(v), v != ""
	}
}

// credentialRegistry 装配服务端凭证集中登记面（T-W5-007）：全部环境变量
// 注入凭证在此显式登记（name→provider+loader），随后 Validate 做启动期两级
// 校验——auth 类缺失硬失败（阻断启动），LLM 类缺失告警（调用期拒绝）。
// 登记面同时是日志出口统一 mask 层的事实源（注入 middleware 后，错误详情/
// panic 值里的已登记凭证值与敏感键值对落日志前被打码）。
func credentialRegistry() (*auth.CredentialRegistry, []string, error) {
	r := auth.NewCredentialRegistry()
	specs := []auth.CredentialSpec{
		{
			Name:     "signing_key",
			EnvVar:   auth.EnvVarAuthKey,
			Provider: "in-process HMAC signer",
			Class:    auth.ClassAuth,
			Loader:   envLoader(auth.EnvVarAuthKey),
		},
		{
			Name:     "llm_gateway_key",
			EnvVar:   envVarLLMGatewayKey,
			Provider: "litellm-gateway",
			Class:    auth.ClassLLM,
			Loader:   envLoader(envVarLLMGatewayKey),
		},
	}
	for _, spec := range specs {
		if err := r.Register(spec); err != nil {
			return nil, nil, err
		}
	}
	warnings, err := r.Validate()
	if err != nil {
		return nil, nil, err
	}
	return r, warnings, nil
}

func main() {
	addr := os.Getenv("SCHOOL_LISTEN_ADDR")
	if addr == "" {
		addr = ":8080"
	}
	// D9 fail-closed（T-W5-005 验收 #4）：生产模式缺 SCHOOL_AUTH_SECRET 必须
	// 在启动期失败；开发模式的显式密钥在此处落告警日志。Signer 交给
	// api.NewRouter（T-W5-006）：全部业务路由经 middleware.RequireAuth 挂载。
	signer, warnings, err := auth.EnsureSigner(os.Getenv(auth.EnvEnvironment), os.Getenv(auth.EnvVarAuthKey))
	if err != nil {
		log.Fatalf("auth bootstrap failed: %v", err)
	}
	for _, w := range warnings {
		log.Printf("%s", w)
	}
	// T-W5-007 凭证治理：集中登记面 + 两级校验（auth 缺失硬失败/LLM 缺失
	// 告警）+ 注入 middleware 日志出口的统一 mask 层。
	registryCreds, credWarnings, err := credentialRegistry()
	if err != nil {
		log.Fatalf("credential registry failed: %v", err)
	}
	for _, w := range credWarnings {
		log.Printf("%s", w)
	}
	middleware.SetCredentialRegistry(registryCreds)

	// GO-RW-002 会话全链路生产装配（顺序即依赖方向：池 → 账本/事务面 →
	// 评分桥 → 服务 → 路由）。任一环缺失即启动期失败，绝不带病装配.
	pool, err := pgxpool.New(context.Background(), os.Getenv(envVarDatabaseURL))
	if err != nil {
		log.Fatalf("session store bootstrap failed: %v", err)
	}
	defer pool.Close()
	pingCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err := pool.Ping(pingCtx); err != nil {
		log.Fatalf("session store unreachable: %v", err)
	}
	consents := compliance.NewPGStore()
	accounts := session.NewPGStore() // 题序/提交/运行态三面同一 practice_session 账
	txRunner := &poolTxRunner{pool: pool}
	scorerTable, err := newDeterministicScorerTable()
	if err != nil {
		log.Fatalf("scorer registry bootstrap failed: %v", err)
	}
	scoringRunner, err := scoring.NewRunner(scorerTable)
	if err != nil {
		log.Fatalf("scoring runner bootstrap failed: %v", err)
	}
	scorer := &dbResponseScorer{pool: pool, runner: scoringRunner}
	svc, err := session.NewService(session.Deps{
		Consents:    consents,
		Orders:      accounts,
		Submissions: accounts,
		Accounts:    accounts,
		Runner:      txRunner,
		Reader:      pool,
	})
	if err != nil {
		log.Fatalf("session service bootstrap failed: %v", err)
	}

	// 学生只读面 + 内容取证面（审计 #155 收口）：同一池的只读查询服务，
	// 13 条业务路由自此全部业务接线（零 501 生产形态）。Sync 是复习队列
	// 写侧接缝（P0-4）：提交链路触发的事务化同步器。
	reads := api.LearnerReads{
		Reports: report.NewWeaknessQueryService(pool),
		Review:  review.NewDueQueryService(pool),
		Sync:    &txReviewSyncer{pool: pool},
	}
	contentQueries := content.NewContentQueryService(pool)

	srv := &http.Server{
		Addr:    addr,
		Handler: api.NewRouterWithLearnerReads(signer, consents, svc, scorer, contentQueries, reads),
		// 基线超时（骨架级；SLO 细化在 W5-R S2 API 边界加固落地）
		ReadHeaderTimeout: 10e9,
	}
	log.Printf("school listening on %s", addr)
	if err := srv.ListenAndServe(); err != nil {
		// 脱敏：只记错误类名，不向日志倾倒底层细节以外的信息
		log.Fatalf("listen error_class=%T", err)
	}
}
