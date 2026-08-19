package main

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
)

// T-W5-031 验收 #4：healthz 端点 200 + 最小字段，不泄露内部信息。
func TestHealthz(t *testing.T) {
	srv := httptest.NewServer(newMux())
	defer srv.Close()

	resp, err := http.Get(srv.URL + "/healthz")
	if err != nil {
		t.Fatalf("请求失败: %v", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("status = %d, want 200", resp.StatusCode)
	}
	var body map[string]string
	if err := json.NewDecoder(resp.Body).Decode(&body); err != nil {
		t.Fatalf("响应必须是 JSON: %v", err)
	}
	if body["status"] != "ok" {
		t.Fatalf("status 字段 = %q, want ok", body["status"])
	}
	if len(body) != 1 {
		t.Fatalf("响应字段必须最小化（仅 status），得到 %v", body)
	}
}
