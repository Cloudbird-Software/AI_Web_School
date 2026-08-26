package ai

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

// 总线 fail-closed 语义与台账全覆盖测试（T-W5-014 验收 #1/#2/#4）。
// PG 台账的运行时行为不在本地宣称覆盖（无 Docker/PG，留 CI migrate-go-check
// 与 W6 装配验收）；编译期锚定见 ledger_pg.go。

// ── 测试假件：Caller / Redactor / Ledger / 时钟 / id ─────────────────

// fakeCaller 记录每次出站请求（含收到的 ctx），按脚本回放结果或错误.
type fakeCaller struct {
	mu    sync.Mutex
	calls []OutboundRequest
	ctxs  []context.Context
	resp  OutboundResult
	err   error
}

func (f *fakeCaller) Call(ctx context.Context, req OutboundRequest) (OutboundResult, error) {
	f.mu.Lock()
	f.calls = append(f.calls, req)
	f.ctxs = append(f.ctxs, ctx)
	f.mu.Unlock()
	if f.err != nil {
		return OutboundResult{}, f.err
	}
	return f.resp, nil
}

func (f *fakeCaller) count() int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return len(f.calls)
}

func (f *fakeCaller) lastCtx() context.Context {
	f.mu.Lock()
	defer f.mu.Unlock()
	if n := len(f.ctxs); n > 0 {
		return f.ctxs[n-1]
	}
	return nil
}

var errCallerDown = errors.New("fake caller: 供应商不可达")

// scriptedRedactor 按注入函数执行剥离并记录输入.
type scriptedRedactor struct {
	fn   func(string) (string, []string, error)
	seen []string
	mu   sync.Mutex
}

func (r *scriptedRedactor) Redact(text string) (string, []string, error) {
	r.mu.Lock()
	r.seen = append(r.seen, text)
	r.mu.Unlock()
	return r.fn(text)
}

func passthroughRedactor() *scriptedRedactor {
	return &scriptedRedactor{fn: func(s string) (string, []string, error) { return s, nil, nil }}
}

// flakyLedger 按 status 注入写败故障的台账包装.
type flakyLedger struct {
	inner   *MemoryLedger
	failOn  map[CallStatus]error
	mu      sync.Mutex
	attempt []LedgerEntry
}

func (f *flakyLedger) Record(ctx context.Context, e LedgerEntry) error {
	f.mu.Lock()
	f.attempt = append(f.attempt, e)
	_, inject := f.failOn[e.Status]
	f.mu.Unlock()
	if inject {
		return errors.New("injected ledger outage")
	}
	return f.inner.Record(ctx, e)
}

func (f *flakyLedger) ByArtifact(ctx context.Context, ref string) ([]LedgerEntry, error) {
	return f.inner.ByArtifact(ctx, ref)
}

func (f *flakyLedger) attemptsOn(status CallStatus) int {
	f.mu.Lock()
	defer f.mu.Unlock()
	n := 0
	for _, e := range f.attempt {
		if e.Status == status {
			n++
		}
	}
	return n
}

// deterministic clock: 每次 now() 前进 5ms.
func newClock(start time.Time) func() time.Time {
	cur := start
	var mu sync.Mutex
	return func() time.Time {
		mu.Lock()
		defer mu.Unlock()
		cur = cur.Add(5 * time.Millisecond)
		return cur
	}
}

const testTarget = "deepseek-main"

// newTestBus 构造带注册目标的测试总线；caller 决定出站脚本.
func newTestBus(t *testing.T, red Redactor, led Ledger, caller Caller) *Bus {
	t.Helper()
	b, err := NewBus(red, led)
	if err != nil {
		t.Fatalf("NewBus: %v", err)
	}
	b.SetClock(newClock(time.Date(2026, 8, 27, 12, 0, 0, 0, time.UTC)))
	var seq atomic.Uint64
	b.SetIDGen(func() string {
		return fmt.Sprintf("call-%06d", seq.Add(1))
	})
	if err := b.RegisterTarget(Target{
		Name:         testTarget,
		Modality:     ModalityLLM,
		Provider:     "deepseek",
		Model:        "deepseek-chat",
		ModelVersion: "chat-2026-08",
		Caller:       caller,
		MaxTokens:    512,
		Temperature:  0.3,
	}); err != nil {
		t.Fatalf("RegisterTarget: %v", err)
	}
	return b
}

func draftRequest(prompt string) Request {
	return Request{
		Target:      testTarget,
		TaskLevel:   L2,
		TaskName:    "draft_passage",
		ArtifactRef: "item_revision_1",
		Prompt:      prompt,
	}
}

// ── 验收 #2：总线内统一落账 + 全要素台账行 ───────────────────────────

func TestCallHappyPathWritesCompleteLedgerRow(t *testing.T) {
	red := RegexRedactor{}
	caller := &fakeCaller{resp: OutboundResult{
		Content:  "一篇语篇正文",
		TokenIn:  120,
		TokenOut: 80,
	}}
	led := NewMemoryLedger()
	b := newTestBus(t, red, led, caller)

	raw := "学生张小明做口算练习"
	resp, err := b.Call(context.Background(), draftRequest(raw))
	if err != nil {
		t.Fatalf("Call: %v", err)
	}
	if resp.Content != "一篇语篇正文" || resp.CallID == "" {
		t.Fatalf("响应不完整: %+v", resp)
	}

	rows := led.Snapshot()
	if len(rows) != 1 {
		t.Fatalf("台账行数 = %d, want 1（一次调用恰一行）", len(rows))
	}
	e := rows[0]
	sanitized, _, _ := red.Redact(raw)
	want := LedgerEntry{
		CallID:        resp.CallID,
		Modality:      ModalityLLM,
		TaskLevel:     L2,
		TaskName:      "draft_passage",
		Provider:      "deepseek",
		Model:         "deepseek-chat",
		ModelVersion:  "chat-2026-08",
		PromptHash:    HashPrompt(sanitized),
		PromptVersion: DefaultPromptVersion,
		TokenIn:       120,
		TokenOut:      80,
		CostCNY:       ComputeCostCNY("deepseek-chat", 120, 80),
		Status:        StatusOK,
		ArtifactRef:   "item_revision_1",
		CallerName:    testTarget,
	}
	if e.TaskName != want.TaskName || e.Modality != want.Modality ||
		e.TaskLevel != want.TaskLevel || e.Provider != want.Provider ||
		e.Model != want.Model || e.ModelVersion != want.ModelVersion ||
		e.PromptHash != want.PromptHash || e.PromptVersion != want.PromptVersion ||
		e.TokenIn != want.TokenIn || e.TokenOut != want.TokenOut ||
		e.CostCNY != want.CostCNY || e.Status != want.Status ||
		e.ArtifactRef != want.ArtifactRef || e.CallerName != want.CallerName ||
		e.CallID != want.CallID {
		t.Fatalf("台账行缺要素或取值错误:\n got %+v\nwant %+v", e, want)
	}
	if e.DurationMS < 0 {
		t.Fatalf("duration_ms 为负: %v", e.DurationMS)
	}
	// 出站文本必须是剥离后的文本（原文不出总线）.
	if got := caller.calls[0].Prompt; strings.Contains(got, "张小明") {
		t.Fatalf("未剥离文本到达出站面: %q", got)
	}
}

func TestByArtifactAccumulatesLifecycleCost(t *testing.T) {
	caller := &fakeCaller{resp: OutboundResult{Content: "x", TokenIn: 10, TokenOut: 10}}
	led := NewMemoryLedger()
	b := newTestBus(t, RegexRedactor{}, led, caller)

	for i := 0; i < 3; i++ {
		if _, err := b.Call(context.Background(), draftRequest("第"+fmt.Sprint(i)+"稿")); err != nil {
			t.Fatal(err)
		}
	}
	rows, err := led.ByArtifact(context.Background(), "item_revision_1")
	if err != nil {
		t.Fatal(err)
	}
	if len(rows) != 3 {
		t.Fatalf("归集行数 = %d, want 3", len(rows))
	}
	var total float64
	for _, r := range rows {
		total += r.CostCNY
	}
	if want := 3 * ComputeCostCNY("deepseek-chat", 10, 10); total != want {
		t.Fatalf("累计成本 = %v, want %v", total, want)
	}
}

// ── 验收 #1/#4 fail-closed 三路径 ─────────────────────────────────────

func TestRedactionFailureRejectsWithoutOutboundAndLeavesRejectedRow(t *testing.T) {
	phone := "13812345678"
	red := &scriptedRedactor{fn: func(string) (string, []string, error) {
		// 模拟剥离器异常时把原文 PII 带进错误的行为——总线必须完全吞掉该文本
		return "", nil, fmt.Errorf("regex engine exploded on %s", phone)
	}}
	caller := &fakeCaller{resp: OutboundResult{Content: "不该出现"}}
	led := NewMemoryLedger()
	b := newTestBus(t, red, led, caller)

	resp, err := b.Call(context.Background(), draftRequest("学生张小明 电话 "+phone))
	if !errors.Is(err, ErrRedactionFailed) {
		t.Fatalf("err = %v, want ErrRedactionFailed 链", err)
	}
	if resp != nil {
		t.Fatalf("拒绝路径不得产出响应: %+v", resp)
	}
	// 错误链路不含 PII 原文（D7/X3：异常消息无泄漏）.
	if strings.Contains(err.Error(), phone) {
		t.Fatalf("错误消息泄漏原文: %v", err)
	}
	if caller.count() != 0 {
		t.Fatalf("拒绝后仍发生了出站调用 ×%d", caller.count())
	}
	rows := led.Snapshot()
	if len(rows) != 1 || rows[0].Status != StatusRejected ||
		rows[0].Reason != ReasonRedactionFailed {
		t.Fatalf("应有恰一条 rejected/redaction_failed 行: %+v", rows)
	}
	// 失败原因不含原文 PII——连原始 prompt 的哈希都不固化.
	if rows[0].PromptHash != "" {
		t.Fatalf("rejected 行不得固化原始 prompt 指纹: %q", rows[0].PromptHash)
	}
	if rows[0].TokenIn != 0 || rows[0].TokenOut != 0 || rows[0].CostCNY != 0 {
		t.Fatalf("零出站的 rejected 行不应有计量: %+v", rows[0])
	}
}

func TestBudgetExceededRejectsWithoutOutbound(t *testing.T) {
	caller := &fakeCaller{resp: OutboundResult{Content: "c", TokenIn: 30, TokenOut: 20}}
	led := NewMemoryLedger()
	b := newTestBus(t, passthroughRedactor(), led, caller)

	smallMax := func(p string) Request {
		req := draftRequest(p)
		req.MaxTokens = 10 // 预算判定里的输出侧估算取该显式上限而非目标缺省 512
		return req
	}
	b.SetBudget(NewCumulativeBudget(60)) // 第一次实付 30+20=50 打到只剩 10

	if _, err := b.Call(context.Background(), smallMax("第一")); err != nil {
		t.Fatalf("首次调用应放行: %v", err)
	}
	resp, err := b.Call(context.Background(), smallMax("第二"))
	if !errors.Is(err, ErrBudgetExceeded) || resp != nil {
		t.Fatalf("超限未拒: resp=%v err=%v", resp, err)
	}
	if got := caller.count(); got != 1 {
		t.Fatalf("超限调用不应出站，实际 ×%d", got)
	}
	rows := led.Snapshot()
	if len(rows) != 2 || rows[1].Status != StatusRejected || rows[1].Reason != ReasonBudgetExceeded {
		t.Fatalf("缺 rejected/budget_exceeded 行: %+v", rows)
	}
}

func TestLedgerWriteFailureFailsTheCallAndDiscardsContent(t *testing.T) {
	caller := &fakeCaller{resp: OutboundResult{Content: "机密产物", TokenIn: 10, TokenOut: 5}}
	led := &flakyLedger{inner: NewMemoryLedger(), failOn: map[CallStatus]error{StatusOK: errors.New("outage")}}
	b := newTestBus(t, passthroughRedactor(), led, caller)

	resp, err := b.Call(context.Background(), draftRequest("写一篇短文"))
	if !errors.Is(err, ErrLedgerWrite) {
		t.Fatalf("err = %v, want ErrLedgerWrite 链", err)
	}
	if resp != nil {
		t.Fatalf("账写不上还交付了产物: %+v", resp)
	}
	// 该次内容在台账上无 ok 行可查——交付被整体放弃（不允许「先调用后补账」
	// 的另一半即「账崩了也照常给货」）.
	if got := led.attemptsOn(StatusOK); got != 1 {
		t.Fatalf("ok 行写入尝试 = %d, want 1（同步落账而非异步补账）", got)
	}
	if len(led.inner.Snapshot()) != 0 {
		t.Fatalf("注入故障下不应有成功行落地")
	}
}

func TestUnknownTargetNoOutboundNoLedgerRow(t *testing.T) {
	red := passthroughRedactor()
	led := NewMemoryLedger()
	b, err := NewBus(red, led)
	if err != nil {
		t.Fatal(err)
	}
	req := draftRequest("x")
	req.Target = "ghost-provider"
	resp, err := b.Call(context.Background(), req)
	if !errors.Is(err, ErrUnknownTarget) || resp != nil {
		t.Fatalf("err=%v resp=%v", err, resp)
	}
	// 原文连剥离器都不曾经过（allowlist 门在最前），台账零行.
	if len(red.seen) != 0 {
		t.Fatalf("未知目标不应进剥离门: %v", red.seen)
	}
	if n := len(led.Snapshot()); n != 0 {
		t.Fatalf("未知目标不应有台账行: %d", n)
	}
}

func TestCallerErrorRecordsFailedRowNotOK(t *testing.T) {
	caller := &fakeCaller{err: errCallerDown}
	led := NewMemoryLedger()
	b := newTestBus(t, passthroughRedactor(), led, caller)

	resp, err := b.Call(context.Background(), draftRequest("x"))
	if err == nil || !strings.Contains(err.Error(), errCallerDown.Error()) {
		t.Fatalf("供应商错误应上抛: %v", err)
	}
	if resp != nil {
		t.Fatalf("失败不得有响应: %+v", resp)
	}
	rows := led.Snapshot()
	if len(rows) != 1 || rows[0].Status != StatusFailed || rows[0].Reason != ReasonCallerError {
		t.Fatalf("failed 行缺失或形态错: %+v", rows)
	}
	if rows[0].TokenIn != 0 || rows[0].CostCNY != 0 {
		t.Fatalf("无 usage 上报的 failed 行计量应为零值兜底路径不虚计: %+v", rows[0])
	}
}

// ── 验收 #3：出站加固（HTTPS 强制 / 凭证拦截 / allowlist 管理）────────

func TestRegisterTargetRejectsPlainHTTPAndCredentials(t *testing.T) {
	led := NewMemoryLedger()
	b, _ := NewBus(passthroughRedactor(), led)
	base := Target{
		Name: testTarget, Modality: ModalityLLM, Provider: "p",
		Model: "m", ModelVersion: "v", Caller: &fakeCaller{},
	}

	httpT := base
	httpT.BaseURL = "http://api.example.com/v1"
	if err := b.RegisterTarget(httpT); !errors.Is(err, ErrInsecureOutbound) {
		t.Fatalf("HTTP base_url 未被拒: %v", err)
	}
	credT := base
	credT.BaseURL = "https://user:hunter2@api.example.com"
	if err := b.RegisterTarget(credT); !errors.Is(err, ErrCredentialInURL) {
		t.Fatalf("URL 内嵌凭证未被拒: %v", err)
	}
	dup := base
	dup.BaseURL = "https://api.example.com"
	if err := b.RegisterTarget(dup); err != nil {
		t.Fatalf("合法 https 注册失败: %v", err)
	}
	if err := b.RegisterTarget(dup); !errors.Is(err, ErrDuplicateTarget) {
		t.Fatalf("重复注册应报错: %v", err)
	}
}

func TestRegisterTargetValidatesEssentialFields(t *testing.T) {
	b, _ := NewBus(passthroughRedactor(), NewMemoryLedger())
	noModel := Target{Name: "x", Modality: ModalityLLM, Provider: "p", ModelVersion: "v", Caller: &fakeCaller{}}
	if err := b.RegisterTarget(noModel); !errors.Is(err, ErrInvalidTarget) {
		t.Fatalf("缺 model 应拒: %v", err)
	}
	badModality := noModel
	badModality.Modality = "video"
	if err := b.RegisterTarget(badModality); !errors.Is(err, ErrInvalidTarget) {
		t.Fatalf("modality 越域应拒: %v", err)
	}
	noRedactor, err := NewBus(nil, NewMemoryLedger())
	if err == nil || noRedactor != nil {
		t.Fatalf("无剥离器构造必须失败（fail-closed 从构造期开始）: %v", err)
	}
}

func TestTimeoutIsBoundOntoCallerContext(t *testing.T) {
	caller := &fakeCaller{resp: OutboundResult{Content: "c"}}
	led := NewMemoryLedger()
	b, _ := NewBus(passthroughRedactor(), led)
	if err := b.RegisterTarget(Target{
		Name: testTarget, Modality: ModalityLLM, Provider: "p",
		Model: "m", ModelVersion: "v", Caller: caller, Timeout: 50 * time.Millisecond,
	}); err != nil {
		t.Fatal(err)
	}
	if _, err := b.Call(context.Background(), draftRequest("x")); err != nil {
		t.Fatal(err)
	}
	dl, ok := caller.lastCtx().Deadline()
	if !ok || time.Until(dl) > 50*time.Millisecond+5*time.Second {
		t.Fatalf("目标默认时限未装订到出站 ctx: deadline=%v ok=%v", dl, ok)
	}

	// 调用方自带更紧截止时不放宽.
	tight, cancel := context.WithTimeout(context.Background(), 2*time.Millisecond)
	defer cancel()
	if _, err := b.Call(tight, draftRequest("y")); err != nil && !errors.Is(err, context.DeadlineExceeded) {
		t.Fatalf("紧截止应在 canceled/超时语义下自然失败: %v", err)
	}
	gotDL, okGot := caller.lastCtx().Deadline()
	tightDL, okTight := tight.Deadline()
	if !okGot || !okTight || !gotDL.Equal(tightDL) {
		t.Fatalf("更紧的父截止被放宽: %v(%v) vs %v(%v)", gotDL, okGot, tightDL, okTight)
	}
}

// ── 并发安全（-race）：共享总线多协程调用，账行与出站一一对应 ──────────

func TestConcurrentCallsRaceCleanAndFullyAccounted(t *testing.T) {
	caller := &fakeCaller{resp: OutboundResult{Content: "c", TokenIn: 7, TokenOut: 7}}
	led := NewMemoryLedger()
	b := newTestBus(t, RegexRedactor{}, led, caller)
	b.SetBudget(NewCumulativeBudget(100000))

	const n = 64
	var wg sync.WaitGroup
	errs := make(chan error, n)
	for i := 0; i < n; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			resp, err := b.Call(context.Background(),
				Request{Target: testTarget, TaskLevel: L1, TaskName: "bulk", ArtifactRef: fmt.Sprintf("rev-%02d", i), Prompt: "并发用例"})
			if err != nil {
				errs <- err
				return
			}
			if resp.CallID == "" {
				errs <- errors.New("空 call_id")
			}
		}(i)
	}
	wg.Wait()
	close(errs)
	for err := range errs {
		t.Fatal(err)
	}
	if got := caller.count(); got != n {
		t.Fatalf("出站次数 = %d, want %d", got, n)
	}
	rows := led.Snapshot()
	if len(rows) != n {
		t.Fatalf("台账行数 = %d, want %d（全覆盖，无丢行无重复）", len(rows), n)
	}
	ids := make(map[string]struct{}, n)
	for _, r := range rows {
		ids[r.CallID] = struct{}{}
		if r.Status != StatusOK {
			t.Fatalf("意外状态行: %+v", r)
		}
	}
	if len(ids) != n {
		t.Fatalf("call_id 有碰撞: %d 唯一 / %d 行", len(ids), n)
	}
	if used := b.budget.(*CumulativeBudget).Used(); used != int64(n*14) {
		t.Fatalf("预算回填 = %d, want %d", used, n*14)
	}
}

func TestDoubleFailureSurfacesBothSentinels(t *testing.T) {
	// 剥离失败 + 台账同刻故障：两个哨兵都必须可达——拒绝是主语义，台账故障
	// 不可静默吞掉（errcheck 纪律在错误链路上的对应要求）。
	red := &scriptedRedactor{fn: func(string) (string, []string, error) {
		return "", nil, errors.New("redactor outage")
	}}
	led := &flakyLedger{inner: NewMemoryLedger(), failOn: map[CallStatus]error{
		StatusRejected: errors.New("outage"),
	}}
	b := newTestBus(t, red, led, &fakeCaller{})

	_, err := b.Call(context.Background(), draftRequest("x"))
	if !errors.Is(err, ErrRedactionFailed) || !errors.Is(err, ErrLedgerWrite) {
		t.Fatalf("双哨兵应同时可达: %v", err)
	}
	if callerDown := led.attemptsOn(StatusRejected); callerDown != 1 {
		t.Fatalf("rejected 行写入尝试 = %d, want 1", callerDown)
	}
}
