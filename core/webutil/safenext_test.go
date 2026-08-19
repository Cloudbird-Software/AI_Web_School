package webutil

import (
	"testing"
)

// 表驱动测试（GO 基线）+ 原生 fuzz（BRIEF 技术基线：Go 原生 fuzz 进 gate）。
// 种子语料在常规 `go test` 中即被执行；`go test -fuzz=FuzzSafeNext` 进入
// 覆盖引导模式（T-W5-031 验收 #2 的可执行实证）。

func TestSafeNext(t *testing.T) {
	cases := []struct {
		name string
		in   string
		want string
	}{
		{"站内相对路径", "/items", "/items"},
		{"带查询参数", "/templates/new?x=1", "/templates/new?x=1"},
		{"https 绝对 URL", "https://evil.example/phish", DefaultNext},
		{"协议相对跳转", "//evil.example", DefaultNext},
		{"http 绝对 URL", "http://evil.example", DefaultNext},
		{"相对路径", "relative/path", DefaultNext},
		{"空串", "", DefaultNext},
		{"反斜杠变体", "\\\\evil.example", DefaultNext},
		{"混合分隔符", "/\\evil.example", "/\\evil.example"}, // 单斜杠开头+反斜杠：站内非标准路径，保守放行（不构成协议跳转）
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := SafeNext(tc.in); got != tc.want {
				t.Errorf("SafeNext(%q) = %q, want %q", tc.in, got, tc.want)
			}
		})
	}
}

// FuzzSafeNext 不变式：输出要么等于输入，要么等于 DefaultNext；
// 且输出永远不以 "//" 开头（杜绝协议相对开放跳转）。
func FuzzSafeNext(f *testing.F) {
	seeds := []string{
		"/items", "//evil.example", "https://evil.example", "",
		"relative", "/templates/new?x=1", "\\/evil", "/a//b",
		"javascript:alert(1)", "/\\" + "evil.example",
	}
	for _, s := range seeds {
		f.Add(s)
	}
	f.Fuzz(func(t *testing.T, in string) {
		got := SafeNext(in)
		if got != in && got != DefaultNext {
			t.Fatalf("输出必须是输入或回落值：in=%q got=%q", in, got)
		}
		if len(got) >= 2 && got[0] == '/' && got[1] == '/' {
			t.Fatalf("输出不得为协议相对跳转：in=%q got=%q", in, got)
		}
	})
}
