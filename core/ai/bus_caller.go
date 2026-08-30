package ai

import (
	"context"
	"fmt"
)

// BusCaller 把总线适配回 Caller 接口：业务侧（如 packs 生成器）只认 Caller，
// 而生产语义要求每次生成式调用都经总线（D10 台账/预算/剥离三闸全时态在案）。
// 装配方用本适配器把「经总线的调用面」注入只依赖 Caller 的消费方——
// 「全仓没有绕过总线的直连调用」由此对 Caller 消费方同样成立。
//
// 分层：本类型不 import baml_client（总线纪律不变）；真正的出站执行仍由
// 装配层包一层（internal/bamlai）注册成 Target.Caller，本类型只负责把
// Caller 形状的调用翻译成 Request 形状的总线调用。
type BusCaller struct {
	bus      *Bus
	taskName string
	// promptVersion 空→总线 DefaultPromptVersion.
	promptVersion string
}

// NewBusCaller 构造。taskName 是 D10 台账任务名，必填；bus 为 nil 或
// taskName 为空属装配编程错误（构造期拒绝，不留半残调用面）.
func NewBusCaller(bus *Bus, taskName string) (*BusCaller, error) {
	if bus == nil {
		return nil, fmt.Errorf("bus caller: bus 未注入")
	}
	if taskName == "" {
		return nil, fmt.Errorf("bus caller: task_name 未指定（D10 台账必填）")
	}
	return &BusCaller{bus: bus, taskName: taskName}, nil
}

// SetPromptVersion 显式声明 prompt 版本（空→总线缺省）。
func (c *BusCaller) SetPromptVersion(v string) { c.promptVersion = v }

// Call 实现 Caller：OutboundRequest 的 Target/Prompt/MaxTokens 直译为总线
// Request；Model/Temperature 属目标路由面（总线按 Target 缺省取），不经
// Caller 侧覆写——路由参数唯一事实源在 Target 注册处。
func (c *BusCaller) Call(ctx context.Context, req OutboundRequest) (OutboundResult, error) {
	resp, err := c.bus.Call(ctx, Request{
		Target:        req.Target,
		TaskName:      c.taskName,
		PromptVersion: c.promptVersion,
		Prompt:        req.Prompt,
		MaxTokens:     req.MaxTokens,
	})
	if err != nil {
		return OutboundResult{}, err
	}
	return OutboundResult{
		Content:  resp.Content,
		TokenIn:  resp.TokenIn,
		TokenOut: resp.TokenOut,
		Fallback: resp.Fallback,
	}, nil
}
