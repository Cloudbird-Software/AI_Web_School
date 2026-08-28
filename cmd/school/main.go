// Package main 是 W5-R Go 模块化单体的入口（T-W5-031 骨架）。
//
// 分层（ADR-0004 §三）：cmd/ 入口 · core/ 六边形核心域（零学科特判，X6/GO-3
// 由 tools/go-lint 强制） · api/ 路由 · packs/ 学科与学段包 · registry/ 双注册表
// （D4：作答交互 + 评分器）。
//
// main 只做装配：读环境、构造 server、挂 api.NewRouter()。
package main

import (
	"log"
	"net/http"
	"os"

	"github.com/Cloudbird-Software/AI_Web_School/api"
	"github.com/Cloudbird-Software/AI_Web_School/api/middleware"
	"github.com/Cloudbird-Software/AI_Web_School/core/auth"
)

// envVarLLMGatewayKey 是 LLM 网关角色 key 的环境变量名（docs/secrets.md §2：
// 业务进程只持角色虚拟 key，供应商真实 key 不出网关）。凭证属于 ClassLLM：
// 缺失不阻断启动，但调用期被显式拒绝。
const envVarLLMGatewayKey = "LITELLM_ROLE_KEY"

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
	registry, credWarnings, err := credentialRegistry()
	if err != nil {
		log.Fatalf("credential registry failed: %v", err)
	}
	for _, w := range credWarnings {
		log.Printf("%s", w)
	}
	middleware.SetCredentialRegistry(registry)
	srv := &http.Server{
		Addr:    addr,
		Handler: api.NewRouter(signer),
		// 基线超时（骨架级；SLO 细化在 W5-R S2 API 边界加固落地）
		ReadHeaderTimeout: 10e9,
	}
	log.Printf("school listening on %s", addr)
	if err := srv.ListenAndServe(); err != nil {
		// 脱敏：只记错误类名，不向日志倾倒底层细节以外的信息
		log.Fatalf("listen error_class=%T", err)
	}
}
