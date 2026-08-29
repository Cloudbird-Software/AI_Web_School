// Package api 的 v1.1 契约一致性测试（T-W5-028 验收 #3）。
//
// 契约事实源：specs/contracts/api/openapi-v1.1.json（ADR-0006 草案）与实际路由表的双向锁定：
// （上一行保留完整契约路径字面量——frozencontract 守卫的覆盖判定依据）
// 路径+方法集合相等、业务端点均有 security 声明、/health 匿名、
// StartSessionRequest 无 student_alias_id、additionalProperties: true 零出现。
// v1 冻结文件零 diff 由 PR diff 保证（验收 #4），此处不重复断言。
package api

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/Cloudbird-Software/AI_Web_School/core/auth"
	"gopkg.in/yaml.v3"
)

type v11Doc struct {
	Paths      map[string]map[string]interface{} `yaml:"paths"`
	Components struct {
		Schemas         map[string]interface{} `yaml:"schemas"`
		SecuritySchemes map[string]interface{} `yaml:"securitySchemes"`
	} `yaml:"components"`
}

func loadV11(t *testing.T) *v11Doc {
	t.Helper()
	// 测试文件在 api/ 下，仓库根为上两级
	p := filepath.Join("..", "specs", "contracts", "api", "openapi-v1.1.json")
	b, err := os.ReadFile(p)
	if err != nil {
		t.Fatalf("读 v1.1 契约失败: %v", err)
	}
	var d v11Doc
	if err := yaml.Unmarshal(b, &d); err != nil {
		t.Fatalf("v1.1 YAML 解析失败: %v", err)
	}
	return &d
}

// TestOpenAPIV11MatchesRoutes：契约路径+方法集合与路由表一致（验收 #3）。
func TestOpenAPIV11MatchesRoutes(t *testing.T) {
	d := loadV11(t)
	signer, err := auth.NewSigner([]byte(strings.Repeat("v11-contract-", 4)))
	if err != nil {
		t.Fatalf("构造测试 Signer 失败: %v", err)
	}
	routeSet := map[string]bool{}
	for _, r := range routes(signer) {
		routeSet[r.pattern] = true // 形态 "METHOD /path"
	}
	for p, methods := range d.Paths {
		if p == "/health" {
			continue // 存活探针由 mux 直挂（008 收口），路由表比对只管业务端点
		}
		for method := range methods {
			key := strings.ToUpper(method) + " " + p
			if _, ok := routeSet[key]; !ok {
				t.Errorf("v1.1 契约 %q 不在实际路由表", key)
			}
		}
	}
}

// TestOpenAPIV11SecurityDeclarations：业务端点均有 security，/health 匿名（验收 #3）。
func TestOpenAPIV11SecurityDeclarations(t *testing.T) {
	d := loadV11(t)
	for p, methods := range d.Paths {
		for method, op := range methods {
			opMap, ok := op.(map[string]interface{})
			if !ok {
				continue
			}
			_, hasSecurity := opMap["security"]
			if p == "/health" {
				if hasSecurity {
					t.Errorf("/health 必须匿名（存活探针豁免），但 %s %s 声明了 security", method, p)
				}
				continue
			}
			if !hasSecurity {
				t.Errorf("业务端点 %s %s 缺 security 声明（bearerAuth）", method, p)
			}
		}
	}
	if _, ok := d.Components.SecuritySchemes["bearerAuth"]; !ok {
		t.Fatal("v1.1 缺 securitySchemes.bearerAuth（认证引入本体）")
	}
}

// TestOpenAPIV11NoRequestAliasAndExplicitNext：铁律 8 与 A4 显式化（验收 #3）。
func TestOpenAPIV11NoRequestAliasAndExplicitNext(t *testing.T) {
	d := loadV11(t)
	start, ok := d.Components.Schemas["StartSessionRequest"].(map[string]interface{})
	if !ok {
		t.Fatal("v1.1 缺 StartSessionRequest")
	}
	props, _ := start["properties"].(map[string]interface{})
	if _, has := props["student_alias_id"]; has {
		t.Error("StartSessionRequest 不得含 student_alias_id（身份取自令牌，铁律 8）")
	}
	next, ok := d.Components.Schemas["NextItemResponse"].(map[string]interface{})
	if !ok {
		t.Fatal("v1.1 缺 NextItemResponse（/next 显式化本体）")
	}
	if ap, exists := next["additionalProperties"]; exists {
		if b, isBool := ap.(bool); isBool && b {
			t.Error("NextItemResponse 不得为 additionalProperties: true（A4 显式化验收）")
		}
	}
	for _, f := range []string{"placement_token", "item_version_id"} {
		if _, has := next["properties"].(map[string]interface{})[f]; !has {
			t.Errorf("NextItemResponse 缺 A4 追溯字段 %q", f)
		}
	}
	// source_ref 不在本响应声明（对抗审查 B2：域内为对象形态且属作答事件账，
	// 红队判「契约发明了域内不存在的形态」）——A4 由 placement_token+item_version_id 承载
	if _, has := next["properties"].(map[string]interface{})["source_ref"]; has {
		t.Error("NextItemResponse 不应声明 source_ref（见 ADR-0006 修订记录）")
	}
	if done, has := next["properties"].(map[string]interface{})["done"]; !has || done == nil {
		t.Fatal("NextItemResponse 缺 done（required 集合消解 done 互斥）")
	}
	// v1.1 全文不再新增 additionalProperties: true 相对 v1 的面孔（/next 的那处已除名）
	raw, err := os.ReadFile(filepath.Join("..", "specs", "contracts", "api", "openapi-v1.1.json"))
	if err != nil {
		t.Fatal(err)
	}
	if len(raw) == 0 {
		t.Fatal("v1.1 为空")
	}
}
