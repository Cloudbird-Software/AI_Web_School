package api

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"

	"go.uber.org/goleak"
)

// GO-5（T-W5-033）：api 是服务生命周期的边界层，TestMain 兜底检测
// goroutine 泄漏（httptest 清理不净即红）。后续包若自身拉起 goroutine，
// 同样在各自 TestMain 挂 goleak。
func TestMain(m *testing.M) {
	goleak.VerifyTestMain(m)
}

// T-W5-031 验收 #4：healthz 端点 200 + 最小字段，不泄露内部信息。
// T-W5-006 起 NewRouter 需装配 Signer（路由全量接认证；healthz 白名单仍匿名）。
func TestHealthz(t *testing.T) {
	srv := httptest.NewServer(NewRouter(newAPIFixture(t).signer))
	defer srv.Close()

	resp, err := http.Get(srv.URL + "/healthz")
	if err != nil {
		t.Fatalf("请求失败: %v", err)
	}
	defer func() { _ = resp.Body.Close() }()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("status = %d, want 200", resp.StatusCode)
	}
	if ct := resp.Header.Get("Content-Type"); ct != "application/json" {
		t.Fatalf("Content-Type = %q, want application/json", ct)
	}
	var body map[string]any
	if err := json.NewDecoder(resp.Body).Decode(&body); err != nil {
		t.Fatalf("响应必须是 JSON: %v", err)
	}
	if body["status"] != "ok" {
		t.Fatalf("status 字段 = %v, want ok", body["status"])
	}
	if len(body) != 1 {
		t.Fatalf("响应字段必须最小化（仅 status），得到 %v", body)
	}
}

// 未知路径不被 healthz 吞掉：默认 404（路由骨架行为基线，后续错误映射
// 统一在 T-W5-008 API 边界加固落地）。
func TestUnknownPath(t *testing.T) {
	srv := httptest.NewServer(NewRouter(newAPIFixture(t).signer))
	defer srv.Close()

	resp, err := http.Get(srv.URL + "/no-such-path")
	if err != nil {
		t.Fatalf("请求失败: %v", err)
	}
	defer func() { _ = resp.Body.Close() }()
	if resp.StatusCode != http.StatusNotFound {
		t.Fatalf("status = %d, want 404", resp.StatusCode)
	}
}
