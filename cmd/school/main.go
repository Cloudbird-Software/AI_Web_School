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
	"github.com/Cloudbird-Software/AI_Web_School/core/auth"
)

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
