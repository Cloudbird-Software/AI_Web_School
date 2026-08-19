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
		// T-W0-011：权威段位置的反斜杠被 WHATWG 归一化为 "/"，"/\evil"
		// 等价 "//evil"（跨域）——必须拦截（原断言放行是安全缺陷，此处收紧）。
		{"权威位反斜杠伪形", "/\\evil.example", DefaultNext},
		{"单根路径", "/", "/"},
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
// 且输出永远不以 "//" 或 "/\" 开头（杜绝协议相对与 WHATWG 反斜杠归一化
// 伪形的开放跳转，T-W0-011）。
func FuzzSafeNext(f *testing.F) {
	seeds := []string{
		"/items", "//evil.example", "https://evil.example", "",
		"relative", "/templates/new?x=1", "\\/evil", "/a//b",
		"javascript:alert(1)", "/\\" + "evil.example", "/",
	}
	for _, s := range seeds {
		f.Add(s)
	}
	f.Fuzz(func(t *testing.T, in string) {
		got := SafeNext(in)
		if got != in && got != DefaultNext {
			t.Fatalf("输出必须是输入或回落值：in=%q got=%q", in, got)
		}
		if len(got) >= 2 && got[0] == '/' && (got[1] == '/' || got[1] == '\\') {
			t.Fatalf("输出不得为权威位伪形跳转：in=%q got=%q", in, got)
		}
	})
}
