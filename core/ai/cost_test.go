package ai

import (
	"context"
	"errors"
	"regexp"
	"testing"
)

// 计价/哈希/计数与预算门的纯函数面测试（冻结实现 ledger.py compute_cost_cny
// 与 hash_prompt 的口径对齐；成本归集 W6 消费这两处一致性）.

func TestComputeCostCNYKnownAndUnknown(t *testing.T) {
	cases := []struct {
		model             string
		tokenIn, tokenOut int
		want              float64
	}{
		{"deepseek-chat", 1000, 1000, 0.003},
		{"deepseek-reasoner", 500, 250, 0.006},
		{"gpt-4o", 2000, 0, 0.245},
		{"unknown-model", 9999, 9999, 0},
	}
	for _, tc := range cases {
		if got := ComputeCostCNY(tc.model, tc.tokenIn, tc.tokenOut); got != tc.want {
			t.Fatalf("ComputeCostCNY(%s,%d,%d) = %v, want %v", tc.model, tc.tokenIn, tc.tokenOut, got, tc.want)
		}
	}
}

func TestHashPromptIsSha256Prefix16Hex(t *testing.T) {
	h := HashPrompt("任何文本")
	if len(h) != 16 {
		t.Fatalf("hash 长度 = %d, want 16", len(h))
	}
	if !regexp.MustCompile(`^[0-9a-f]{16}$`).MatchString(h) {
		t.Fatalf("非 hex: %q", h)
	}
	if h != HashPrompt("任何文本") || h == HashPrompt("其他文本") {
		t.Fatal("哈希不稳定或恒等")
	}
}

func TestSimpleTokenCounterDeterministic(t *testing.T) {
	c := SimpleTokenCounter{}
	text := "口算练习 apple pie 42"
	got := c.Count(text)
	if got != c.Count(text) || got <= 0 {
		t.Fatalf("计数不确定或为空: %d", got)
	}
	// CJK 按字、ASCII 连续串按词：口算练习=4 字 + apple/pie/42=3 词 = 7
	if got != 7 {
		t.Fatalf("Count = %d, want 7", got)
	}
}

func TestCumulativeBudgetGate(t *testing.T) {
	b := NewCumulativeBudget(100)
	ctx := context.Background()

	if err := b.Allow(ctx, UsageEstimate{InputTokens: 30, MaxOutputTokens: 50}); err != nil {
		t.Fatalf("首次放行被拒: %v", err)
	}
	b.Observe(40, 40) // 实付 80，剩 20

	err := b.Allow(ctx, UsageEstimate{InputTokens: 15, MaxOutputTokens: 20})
	if !errors.Is(err, ErrBudgetExceeded) && err == nil {
		t.Fatal("超限未拒")
	}

	if b.Used() != 80 {
		t.Fatalf("Used = %d, want 80", b.Used())
	}

	// 未配置限额（<=0）= 不设门
	open := NewCumulativeBudget(0)
	if err := open.Allow(ctx, UsageEstimate{InputTokens: 1 << 20}); err != nil {
		t.Fatalf("未配置限额不应拦: %v", err)
	}
}
