package main

import "testing"

// TestParseSteps 严格解析回归（#43 High）：负数/0/尾随输入必须拒绝——
// 此前 `up -3` 经 fmt.Sscanf 解析后直接 m.Steps(-3)，迁移方向被静默反转。
func TestParseSteps(t *testing.T) {
	cases := []struct {
		in      string
		want    int
		wantErr bool
	}{
		{"1", 1, false},
		{"3", 3, false},
		{"22", 22, false},
		{"0", 0, true},                    // 0 步无意义，拒绝
		{"-3", 0, true},                   // 负数 = 方向反转攻击，必须拒绝
		{"+3", 0, true},                   // 符号前缀与尾随输入同理拒绝，只认纯正整数
		{"3x", 0, true},                   // 尾随输入（Sscanf 会放过，Atoi 不会）
		{"3.0", 0, true},                  // 非整数
		{"", 0, true},                     // 空串
		{" 3", 0, true},                   // 前导空白（Sscanf 会放过）
		{"99999999999999999999", 0, true}, // 溢出
	}
	for _, c := range cases {
		got, err := parseSteps(c.in)
		if c.wantErr {
			if err == nil {
				t.Errorf("parseSteps(%q) = %d, 期望报错（负数/0/尾随输入必须拒绝）", c.in, got)
			}
			continue
		}
		if err != nil {
			t.Errorf("parseSteps(%q) 意外报错: %v", c.in, err)
			continue
		}
		if got != c.want {
			t.Errorf("parseSteps(%q) = %d, 期望 %d", c.in, got, c.want)
		}
	}
}
