package validators

import (
	"encoding/json"
	"math"
	"strings"
	"testing"
)

// 表驱动：规范化序列化的确定性——键序/空白/转义差异必须折叠为同一摘要；
// 语义不同的内容必须得到不同摘要；歧义输入 fail-closed 报错。
func TestContentDigestCanonical(t *testing.T) {
	tests := []struct {
		name    string
		a       any
		b       any  // nil = 只测 a 自身格式，不做两两比对
		same    bool // a 与 b 的摘要是否应相同
		wantErr bool // a 是否应计算失败
	}{
		{
			name: "同内容_不同键序与空白",
			a:    mustJSON(t, `{"stem":"二分之一加四分之一","options":{"A":"3/4","B":"1/4"},"answer":"A"}`),
			b: mustJSON(t, `{
				"answer" : "A" ,
				"options" : { "B": "1/4", "A": "3/4" },
				"stem": "二分之一加四分之一"
			}`),
			same: true,
		},
		{
			name: "unicode转义与原文等价",
			a:    mustJSON(t, `{"stem":"中位 数"}`),
			b:    mustJSON(t, `{"stem":"\u4e2d\u4f4d \u6570"}`),
			same: true,
		},
		{
			name: "数组保序_顺序不同即不同内容",
			a:    mustJSON(t, `{"steps":["a","b"]}`),
			b:    mustJSON(t, `{"steps":["b","a"]}`),
			same: false,
		},
		{
			name: "仅参数不同的合法变式不误判为相同",
			a:    mustJSON(t, `{"op":"+","operands":[1,2]}`),
			b:    mustJSON(t, `{"op":"+","operands":[2,3]}`),
			same: false,
		},
		{
			name: "整型与浮点同值折叠为同一摘要",
			a:    map[string]any{"x": 10},
			b:    map[string]any{"x": 10.0},
			same: true,
		},
		{
			name: "int64大值保持精确",
			a:    map[string]any{"id": int64(9007199254740993)},
			b:    map[string]any{"id": int64(9007199254740993)},
			same: true,
		},
		{
			name:    "非法UTF8拒绝",
			a:       map[string]any{"s": "\xff\xfe"},
			wantErr: true,
		},
		{
			name:    "NaN拒绝",
			a:       map[string]any{"x": math.NaN()},
			wantErr: true,
		},
		{
			name:    "Inf拒绝",
			a:       []any{math.Inf(1)},
			wantErr: true,
		},
		{
			name:    "不支持类型拒绝",
			a:       map[string]any{"f": func() {}},
			wantErr: true,
		},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			da, err := ContentDigest(tt.a)
			if tt.wantErr {
				if err == nil {
					t.Fatalf("期望报错，实际 digest=%s", da)
				}
				return
			}
			if err != nil {
				t.Fatalf("a 计算失败: %v", err)
			}
			if !strings.HasPrefix(da, DigestPrefix) || len(da) != len(DigestPrefix)+64 {
				t.Fatalf("digest 格式不符: %q", da)
			}
			da2, err := ContentDigest(tt.a)
			if err != nil || da2 != da {
				t.Fatalf("同一内容重复计算不稳定: %s vs %s (err=%v)", da, da2, err)
			}
			if tt.b == nil {
				return
			}
			db, err := ContentDigest(tt.b)
			if err != nil {
				t.Fatalf("b 计算失败: %v", err)
			}
			if (da == db) != tt.same {
				t.Fatalf("判等同语义不符: same=%v 但 a=%s b=%s", tt.same, da, db)
			}
		})
	}
}

// CanonicalJSON 的文本形态锚定：紧凑分隔、键升序、UTF-8 直出。
func TestCanonicalJSONShape(t *testing.T) {
	got, err := CanonicalJSON(mustJSON(t, `{"b":1,"a":{"y":[true,null],"x":"中"}}`))
	if err != nil {
		t.Fatalf("CanonicalJSON 失败: %v", err)
	}
	const want = `{"a":{"x":"中","y":[true,null]},"b":1}`
	if got != want {
		t.Fatalf("规范化形态漂移:\n got=%s\nwant=%s", got, want)
	}
}

func mustJSON(t *testing.T, s string) map[string]any {
	t.Helper()
	var v any
	if err := json.Unmarshal([]byte(s), &v); err != nil {
		t.Fatalf("fixture 解码失败: %v", err)
	}
	return v.(map[string]any)
}
