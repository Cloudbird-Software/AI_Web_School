package ai

import (
	"context"
	"fmt"
	"sync"
)

// UsageEstimate 是预算门的估算输入：一次调用预计的输入 token 与输出上限.
// InputTokens 由 TokenCounter 对剥离后文本计得；MaxOutputTokens 取路由参数
// max_tokens（供应商硬上限，真实消耗不会更高——估算取的是有界上确界）.
type UsageEstimate struct {
	InputTokens     int
	MaxOutputTokens int
}

// Budget 是预算门契约（W6 成本核算/硬顶的前置骨架）。
//
// 语义边界：预算是容量门而非合规门（X12 的 fail-closed 针对 PII 授权类合规；
// 超限拒绝新调用是 D10 成本可控面的工程表达）。返回的哨兵错误由总线 Join
// 上抛，调用方可 errors.Is(ErrBudgetExceeded) 与具体实现错误并判.
type Budget interface {
	// Allow 判定一次新调用是否放行；非 nil 即拒绝（总线零出站 + 落 rejected 行）.
	Allow(ctx context.Context, est UsageEstimate) error
	// Observe 回填一次成功调用的真实实付 token（失败/rejected 无 token 实付，
	// 不回填；估算口径见 UsageEstimate——输出侧按路由上限取有界上确界）.
	Observe(inputTokens, outputTokens int)
}

// CumulativeBudget 是 Budget 的进程内实现：累计 token 消耗对硬顶 limit 的
// 乐观记账——Allow 以「已用+本次估算 ≤ 硬顶」判定；Observe 回填真实用量。
// 并发安全内置互斥锁；限额 ≤0 视为未配置（恒放行），与 Bus.budget==nil 的
// 「未配置」语义收敛到同一处判断，避免两套空值语义.
type CumulativeBudget struct {
	mu    sync.Mutex
	limit int64
	used  int64
}

// NewCumulativeBudget 构造以 token 为单位的累计预算；limitTokens<=0 时退化为
// 放行门（显式表达「未配置限额」而非静默 0 额度）.
func NewCumulativeBudget(limitTokens int64) *CumulativeBudget {
	return &CumulativeBudget{limit: limitTokens}
}

// Allow 实现 Budget：超过剩余额度即拒绝，附差额诊断（数值本身不敏感）.
func (c *CumulativeBudget) Allow(_ context.Context, est UsageEstimate) error {
	if c == nil || c.Limit() <= 0 {
		return nil
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	projected := c.used + int64(est.InputTokens) + int64(est.MaxOutputTokens)
	if projected > c.limit {
		return fmt.Errorf("ai/budget: 预算 %d token 不足以覆盖本次估算 %d（已用 %d）",
			c.limit, est.InputTokens+est.MaxOutputTokens, c.used)
	}
	return nil
}

// Observe 实现 Budget：回填真实实付 token（负值钳为 0，防上游统计脏数据扣穿账面）.
func (c *CumulativeBudget) Observe(inputTokens, outputTokens int) {
	if c == nil {
		return
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	if inputTokens > 0 {
		c.used += int64(inputTokens)
	}
	if outputTokens > 0 {
		c.used += int64(outputTokens)
	}
}

// Limit 返回配置的硬顶（0 表示未配置=不限额）.
func (c *CumulativeBudget) Limit() int64 {
	if c == nil {
		return 0
	}
	return c.limit
}

// Used 返回当前累计消耗（观测面；测试断言用）.
func (c *CumulativeBudget) Used() int64 {
	if c == nil {
		return 0
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.used
}
