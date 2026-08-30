package ai

import (
	"context"
	"errors"
	"testing"
)

// fakeCaller 记录收到的出站请求并回放固定内容（断言总线确实路由到目标执行面）.
type recordingCaller struct {
	got   OutboundRequest
	calls int
}

func (c *recordingCaller) Call(_ context.Context, req OutboundRequest) (OutboundResult, error) {
	c.calls++
	c.got = req
	return OutboundResult{Content: "ok", TokenIn: 3, TokenOut: 5}, nil
}

func newBusCallerTestBus(t *testing.T, caller Caller) *Bus {
	t.Helper()
	bus, err := NewBus(RegexRedactor{}, NewMemoryLedger())
	if err != nil {
		t.Fatalf("NewBus: %v", err)
	}
	err = bus.RegisterTarget(Target{
		Name: "t-llm", Modality: ModalityLLM,
		Provider: "fake", Model: "m", ModelVersion: "v1",
		Caller: caller,
	})
	if err != nil {
		t.Fatalf("RegisterTarget: %v", err)
	}
	return bus
}

func TestBusCallerRoundTrip(t *testing.T) {
	rc := &recordingCaller{}
	bus := newBusCallerTestBus(t, rc)
	bc, err := NewBusCaller(bus, "draft_instance")
	if err != nil {
		t.Fatalf("NewBusCaller: %v", err)
	}
	out, err := bc.Call(context.Background(), OutboundRequest{
		Target: "t-llm", Model: "m", Prompt: "hello world",
		MaxTokens: 64, Temperature: 0.5,
	})
	if err != nil {
		t.Fatalf("Call: %v", err)
	}
	if out.Content != "ok" || out.TokenIn != 3 || out.TokenOut != 5 {
		t.Fatalf("出站结果未透传: %+v", out)
	}
	if rc.calls != 1 {
		t.Fatalf("目标执行面调用次数 = %d, want 1", rc.calls)
	}
	if rc.got.Prompt != "hello world" || rc.got.MaxTokens != 64 {
		t.Fatalf("出站请求字段丢失: %+v", rc.got)
	}
}

func TestBusCallerLedgeredUnderTaskName(t *testing.T) {
	ledger := NewMemoryLedger()
	bus, err := NewBus(RegexRedactor{}, ledger)
	if err != nil {
		t.Fatalf("NewBus: %v", err)
	}
	if err := bus.RegisterTarget(Target{
		Name: "t-llm", Modality: ModalityLLM,
		Provider: "fake", Model: "m", ModelVersion: "v1",
		Caller: &recordingCaller{},
	}); err != nil {
		t.Fatalf("RegisterTarget: %v", err)
	}
	bc, err := NewBusCaller(bus, "draft_instance")
	if err != nil {
		t.Fatalf("NewBusCaller: %v", err)
	}
	if _, err := bc.Call(context.Background(), OutboundRequest{Target: "t-llm", Prompt: "hello"}); err != nil {
		t.Fatalf("Call: %v", err)
	}
	snap := ledger.Snapshot()
	if len(snap) != 1 {
		t.Fatalf("台账行数 = %d, want 1", len(snap))
	}
	if snap[0].TaskName != "draft_instance" || snap[0].Status != StatusOK {
		t.Fatalf("台账行任务名/状态不符: %+v", snap[0])
	}
}

func TestBusCallerConstructionFailClosed(t *testing.T) {
	if _, err := NewBusCaller(nil, "t"); err == nil {
		t.Fatal("nil bus 必须构造期拒绝")
	}
	bus := newBusCallerTestBus(t, &recordingCaller{})
	if _, err := NewBusCaller(bus, ""); err == nil {
		t.Fatal("空 task_name 必须构造期拒绝")
	}
}

func TestBusCallerErrorPassthrough(t *testing.T) {
	bus, err := NewBus(RegexRedactor{}, NewMemoryLedger())
	if err != nil {
		t.Fatalf("NewBus: %v", err)
	}
	bc, err := NewBusCaller(bus, "draft_instance")
	if err != nil {
		t.Fatalf("NewBusCaller: %v", err)
	}
	_, err = bc.Call(context.Background(), OutboundRequest{Target: "未注册目标", Prompt: "x"})
	if err == nil {
		t.Fatal("未注册目标必须报错（allowlist 结构保证）")
	}
	if !errors.Is(err, err) {
		t.Fatal("unreachable")
	}
}
