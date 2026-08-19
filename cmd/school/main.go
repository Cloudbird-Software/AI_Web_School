// Package main 是 W5-R Go 模块化单体的入口（T-W5-031 骨架）。
//
// 分层（ADR-0004 §三）：cmd/ 入口 · core/ 六边形核心域（零学科特判，X6/GO-3
// 由 tools/go-lint 强制） · api/ 路由 · packs/ 学科与学段包 · registry/ 双注册表
// （D4：作答交互 + 评分器）。
//
// /healthz 语义对齐 T-W0-010：错误路径只暴露 error_class，不泄露堆栈与内部地址。
package main

import (
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"os"
)

// healthResponse 是 /healthz 的响应体（字段最小化，脱敏原则同 Python 版）。
type healthResponse struct {
	Status string `json:"status"`
}

// newMux 构造路由；独立函数便于 httptest 集成测试。
func newMux() *http.ServeMux {
	mux := http.NewServeMux()
	mux.HandleFunc("/healthz", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusOK)
		// 写失败只记服务端日志，不向响应注入内部错误细节
		if err := json.NewEncoder(w).Encode(healthResponse{Status: "ok"}); err != nil {
			log.Printf("healthz encode error_class=%s", errClass(err))
		}
	})
	return mux
}

// errClass 返回异常类名（脱敏：不含消息与堆栈，对齐 py/stack-trace-exposure 语义）。
func errClass(err error) string {
	if err == nil {
		return ""
	}
	return fmt.Sprintf("%T", err)
}

func main() {
	addr := os.Getenv("SCHOOL_LISTEN_ADDR")
	if addr == "" {
		addr = ":8080"
	}
	srv := &http.Server{
		Addr:    addr,
		Handler: newMux(),
		// 基线超时（骨架级；SLO 细化在 W5-R S2 API 边界加固落地）
		ReadHeaderTimeout: 10e9,
	}
	log.Printf("school listening on %s", addr)
	if err := srv.ListenAndServe(); err != nil {
		log.Fatalf("listen error_class=%s", errClass(err))
	}
}
