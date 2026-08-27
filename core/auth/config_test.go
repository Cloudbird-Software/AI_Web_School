package auth

// 密钥解析 fail-closed 语义的测试（T-W5-005 验收 #4）。
// 原则：任何路径下都不允许"空/固定密钥也能跑通"；告警文案只进 warnings
// 返回值，绝不回显密钥本体。

import (
	"bytes"
	"strings"
	"testing"
	"time"
)

const devKey = "dev-only-secret-key-for-t-w5-005-tests"

// TestResolveSecretMatrix 四象限：生产缺钥=拒绝启动；生产有钥=静默；
// 开发两种形态都可用但必须产生告警（调用方落启动日志）。
func TestResolveSecretMatrix(t *testing.T) {
	cases := []struct {
		name        string
		environment string
		configured  string
		wantErr     bool
		wantWarning bool
	}{
		{"生产缺钥", EnvironmentProduction, "", true, false},
		{"生产有钥", EnvironmentProduction, devKey + "-long-enough", false, false},
		{"开发缺钥", "development", "", false, true},
		{"开发环境未设置", "", "", false, true},
		{"开发显式密钥", "development", devKey, false, true},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			secret, warnings, err := ResolveSecret(tc.environment, tc.configured)
			if tc.wantErr {
				if err == nil {
					t.Fatal("应拒绝（fail-closed），得到 nil 错误")
				}
				// 错误信息可提及变量名，但绝不能回显已配置的密钥值。
				if tc.configured != "" && strings.Contains(err.Error(), tc.configured) {
					t.Fatalf("错误信息不得包含密钥本体: %q", err.Error())
				}
				return
			}
			if err != nil {
				t.Fatalf("不应失败: %v", err)
			}
			if len(secret) < minSecretLen {
				t.Fatalf("解析出的密钥短于安全下限: %d 字节", len(secret))
			}
			if tc.wantWarning != (len(warnings) > 0) {
				t.Fatalf("warnings = %v, want 存在=%v", warnings, tc.wantWarning)
			}
			// 开发显式密钥必须原样透传（调用方明确选择了它，不能被替换）。
			if tc.configured != "" && !bytes.Equal(secret, []byte(tc.configured)) {
				t.Fatal("显式配置的开发密钥未被采用")
			}
		})
	}

	t.Run("生产缺钥错误指向配置项", func(t *testing.T) {
		_, _, err := ResolveSecret(EnvironmentProduction, "")
		if !strings.Contains(err.Error(), EnvVarAuthKey) || !strings.Contains(err.Error(), "fail-closed") {
			t.Fatalf("错误信息应指明缺失的环境变量与 fail-closed 策略: %v", err)
		}
	})
}

// TestResolveSecretDevEphemeralUnique 两次缺钥解析生成不同临时密钥：
// 它的价值在隔离不同进程，而非跨进程共享。
func TestResolveSecretDevEphemeralUnique(t *testing.T) {
	a, wa, errA := ResolveSecret("development", "")
	b, wb, errB := ResolveSecret("development", "")
	if errA != nil || errB != nil {
		t.Fatalf("开发缺钥不应失败: %v / %v", errA, errB)
	}
	if len(wa) == 0 || len(wb) == 0 {
		t.Fatalf("两条路径都必须产生告警: %v / %v", wa, wb)
	}
	if bytes.Equal(a, b) {
		t.Fatal("进程内随机密钥不应相同")
	}
}

// TestEnsureSigner 组合层的可用性：能签能验、错误语义不丢失。
func TestEnsureSigner(t *testing.T) {
	s, warnings, err := EnsureSigner("development", devKey)
	if err != nil {
		t.Fatalf("装配失败: %v", err)
	}
	if len(warnings) == 0 {
		t.Fatal("开发显式密钥必须有告警")
	}
	token, err := s.Issue(studentPrincipal(), time.Minute)
	if err != nil {
		t.Fatalf("签发失败: %v", err)
	}
	if _, err := s.Verify(token); err != nil {
		t.Fatalf("校验失败: %v", err)
	}
	// 生产缺钥错误穿透 EnsureSigner，不被吞掉重试。
	if _, _, err := EnsureSigner(EnvironmentProduction, ""); err == nil {
		t.Fatal("生产缺钥必须阻断装配")
	}
}
