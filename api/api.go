// Package api 是 W5-R 的路由骨架（T-W5-031）：HTTP 边界装配点。
//
// 职责边界（ADR-0004 §三）：api 只做协议层（路由/错误映射/中间件），
// 业务语义全部下沉 core/；api 不出现任何学科概念（X6 同样适用于本层：
// 学科差异经 registry 的条目参数表达，不经 if-subject 分支表达）。
//
// /healthz 语义对齐 T-W0-010：成功 200 {"status":"ok"}；异常路径只暴露
// error_class，堆栈与内部地址仅进服务端日志。
package api

import (
	"encoding/json"
	"fmt"
	"log"
	"net/http"
)

// healthResponse 是 /healthz 的响应体（字段最小化，脱敏原则同 Python 版）。
type healthResponse struct {
	Status string `json:"status"`
}

// errClass 返回异常类名（脱敏：不含消息与堆栈，对齐 py/stack-trace-exposure 语义）。
func errClass(err error) string {
	if err == nil {
		return ""
	}
	return fmt.Sprintf("%T", err)
}

// NewRouter 构造路由；独立函数便于 httptest 集成测试。
func NewRouter() *http.ServeMux {
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
