package ai

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"errors"
	"fmt"
	"net/url"
	"sync"
	"time"
)

// ── 哨兵错误：调用方按 errors.Is 分支处理，不用字符串匹配 ──────────────
//
// X12 fail-closed 的对偶面：拒绝必须可区分、可归因；同时错误文本是审计资产
// 也是泄漏面——本组哨兵与包装文本由总线常量构造，禁止把剥离器的原始 error
// 文本拼进返回值与台账（D7/X3：PII 与凭证不进日志、异常消息、台账）。
var (
	// ErrUnknownTarget 表示请求的出站目标未注册（allowlist 强制，验收 #2）。
	ErrUnknownTarget = errors.New("ai/bus: 出站目标未注册（仅允许显式注册的出站目标）")

	// ErrInsecureOutbound 表示注册目标的 base_url 非 HTTPS（验收 #3/#4：
	// 出站强制 HTTPS，明文 HTTP 出站一律拒绝，无配置开关）。
	ErrInsecureOutbound = errors.New("ai/bus: 出站 base_url 必须为 https://")

	// ErrCredentialInURL 表示注册目标 URL 内嵌了用户名/密码 userinfo——
	// 凭证不得进总线（X3；caller_name 之外的一切凭证固化路径都被拒）。
	ErrCredentialInURL = errors.New("ai/bus: 出站 URL 禁止内嵌凭证（userinfo）")

	// ErrRedactionFailed 表示 PII 剥离器失败，调用已拒绝且无出站请求
	// （D7/X12：剥离失败=调用失败，不存在降级放行开关——冻结实现里的
	// bypass_pii_filter 参数与其 fail-open 路径在本重锚定中被删除而非移植）。
	ErrRedactionFailed = errors.New("ai/bus: PII 剥离失败，调用已拒绝（fail-closed）")

	// ErrBudgetExceeded 表示预算门拒绝了本次新调用（W6 硬顶的前置骨架）。
	ErrBudgetExceeded = errors.New("ai/bus: 预算超限，拒绝新调用")

	// ErrLedgerWrite 表示台账写入失败。语义（本卡设计核心）：产物同时被丢弃、
	// 整次调用按失败向上抛——不允许「先出站后补账」，也不允许「账写不上仍照常
	// 交付」；任何到达调用方的内容必有台账行可查（D10 全覆盖的收口语义）。
	ErrLedgerWrite = errors.New("ai/bus: 台账写入失败，调用按失败处置（产物已丢弃）")

	// ErrInvalidRequest 表示请求缺必填字段（task_name/target 等 D10 台账要素）。
	ErrInvalidRequest = errors.New("ai/bus: 请求缺必填字段")

	// ErrDuplicateTarget 表示重复注册同名出站目标（allowlist 变更是显式动作，
	// 静默覆盖会让审计口径漂移）。
	ErrDuplicateTarget = errors.New("ai/bus: 出站目标已注册")

	// ErrInvalidTarget 表示目标或总线构造参数非法（空模型标识等）。
	ErrInvalidTarget = errors.New("ai/bus: 目标参数非法")
)

// Modality 是任务类型四值域（ADR 附录 A：LLM/TTS/嵌入/ASR），与 0026 迁移的
// ck_ai_call_ledger_modality_domain CHECK 一致.
type Modality string

// 任务类型域（0026 CHECK 同源，越域即 DB 拒绝）.
const (
	ModalityLLM       Modality = "llm"
	ModalityTTS       Modality = "tts"
	ModalityEmbedding Modality = "embedding"
	ModalityASR       Modality = "asr"
)

// TaskLevel 是路由档位 L0–L3（架构 v2 §4.8；冻结实现 policy.yaml 同词表）。
// 空串落库为 NULL（0026 允许）：前置门在路由完成前拒绝时档位未定.
type TaskLevel string

// 路由档位（与 ck_ai_call_ledger_task_level_domain CHECK 一致）.
const (
	L0 TaskLevel = "L0"
	L1 TaskLevel = "L1"
	L2 TaskLevel = "L2"
	L3 TaskLevel = "L3"
)

// 台账 reason 短码：固定小写枚举词，禁入底层 error 文本（X3/D7 泄漏面收口）.
const (
	ReasonRedactionFailed = "redaction_failed"
	ReasonBudgetExceeded  = "budget_exceeded"
	ReasonCallerError     = "caller_error"
)

// DefaultPromptVersion 是 prompt 版本的缺省值（冻结实现 schemas.LedgerEntry 对齐）.
const DefaultPromptVersion = "v1"

// validModalities/taskLevels 为固定顺序词表（越域报错用，避免 map 遍历乱序）.
var (
	validModalities = []Modality{ModalityLLM, ModalityTTS, ModalityEmbedding, ModalityASR}
	validTaskLevels = []TaskLevel{L0, L1, L2, L3}
)

// ValidModality 报告 modality 是否在四值域内.
func ValidModality(m Modality) bool {
	for _, v := range validModalities {
		if m == v {
			return true
		}
	}
	return false
}

// ValidTaskLevel 报告档位是否合法（空串合法=未完成路由即被拒的留痕行）.
func ValidTaskLevel(l TaskLevel) bool {
	if l == "" {
		return true
	}
	for _, v := range validTaskLevels {
		if l == v {
			return true
		}
	}
	return false
}

// Caller 是一次生成式调用的出站执行面抽象。生产装配方把 baml_client 函数
// （如 GenerateDraftInstance）包装成本接口注入；测试注入 fake。总线只认本
// 接口而不 import baml_client——BAML 函数签名演进被隔离在装配层（W6 接线）.
//
// 实现契约：
//   - 遵守传入 ctx 的取消/超时（总线会按 Target.Timeout 注入更紧截止时间）；
//   - Prompt 已经过总线剥离，实现方不得再引入未剥离文本；
//   - TokenIn/TokenOut 可上报真实 usage；留零则由 TokenCounter 兜底计数；
//   - 错误文本不得含凭证与 prompt 原文（总线将原样透传给运维，X3/D7）；
//   - Fallback=true 表示实际走了备用通道（冻结实现 AIResult.fallback 对齐）。
type Caller interface {
	Call(ctx context.Context, req OutboundRequest) (OutboundResult, error)
}

// OutboundRequest 是交给出站执行面的已审请求（只含剥离后文本）.
type OutboundRequest struct {
	// Target 是 allowlist 中注册的目标名（多路 Caller 复用一个执行面时辨向）.
	Target string
	// Model 是该目标命中的模型标识（D10 模型标识）.
	Model string
	// Prompt 是剥离后的出站文本；原文在任何字段中不存在.
	Prompt string
	// MaxTokens/Temperature 为该目标的路由参数（冻结实现 policy.yaml 字段对齐）.
	MaxTokens   int
	Temperature float64
}

// OutboundResult 是出站执行面的统一返回（冻结实现 AIResult 语义对齐）.
type OutboundResult struct {
	Content  string
	TokenIn  int
	TokenOut int
	// Fallback 是否走了备用供应商通道（L3 双供应商预案消费此标志）.
	Fallback bool
}

// Redactor 是 PII 剥离钩子（D7）。返回 error 即 fail-closed：总线拒绝调用、
// 不发出站请求，只以固定短码 ReasonRedactionFailed 落账（不含原文与错误细节
// 文本）。错误细节的诊断责任在剥离器实现侧自行投递受控观测通道，不经总线.
type Redactor interface {
	Redact(text string) (sanitized string, stripped []string, err error)
}

// Target 描述一个显式注册的出站目标：allowlist 的事实源。「全仓没有绕过总线的
// 直连调用」（验收 #2）由此获得结构保证——未注册的名字在总线上不可达.
type Target struct {
	Name     string
	Modality Modality
	Provider string
	// Model/ModelVersion：D10 要求的两个模型维度（标识与版本分开留痕）.
	Model        string
	ModelVersion string
	// Caller 执行面（生产=baml_client 包装器；测试=fake）.
	Caller Caller
	// BaseURL 为空表示非 HTTP 直连通道（BAML 托管/进程内）；非空必须 https://，
	// 且禁止内嵌 userinfo 凭证（validate 静态强制）。本卡不发起真实网络请求
	// （HTTP client 由装配方经 Caller 注入），这里守住的是准入校验面.
	BaseURL string
	// Timeout 为单次调用的默认时限；0 表示沿用调用方 ctx 自带截止时间.
	Timeout time.Duration
	// MaxTokens/Temperature 是该目标的缺省路由参数（Request.MaxTokens 可覆盖）.
	MaxTokens   int
	Temperature float64
}

// validate 做准入静态校验：HTTPS 强制 + 凭证拦截 + 词表与必填检查（验收 #3/#4）.
func (t Target) validate() error {
	if t.Name == "" || t.Caller == nil {
		return fmt.Errorf("%w: name/caller 不能为空", ErrInvalidTarget)
	}
	if !ValidModality(t.Modality) {
		return fmt.Errorf("%w: modality %q 越域", ErrInvalidTarget, t.Modality)
	}
	if t.Provider == "" || t.Model == "" || t.ModelVersion == "" {
		return fmt.Errorf("%w: provider/model/model_version 必填（D10 台账五要素）", ErrInvalidTarget)
	}
	if t.Timeout < 0 || t.MaxTokens < 0 {
		return fmt.Errorf("%w: timeout/max_tokens 不可为负", ErrInvalidTarget)
	}
	if t.BaseURL != "" {
		u, err := url.Parse(t.BaseURL)
		if err != nil {
			return fmt.Errorf("%w: base_url 解析失败", ErrInvalidTarget)
		}
		if u.User != nil {
			return ErrCredentialInURL
		}
		if u.Scheme != "https" {
			// 无开关硬拒绝（X12）：http:// 及一切明文 scheme 均不可达
			return fmt.Errorf("%w: 方案 %q 不被允许", ErrInsecureOutbound, u.Scheme)
		}
	}
	return nil
}

// Request 是业务方向总线发起的一次生成式调用请求（冻结实现 ai_call 入参对齐；
// 冻结实现的 bypass_pii_filter 参数不再存在——见 ErrRedactionFailed 注释）.
type Request struct {
	// Target 引用已注册的出站目标名.
	Target    string
	TaskLevel TaskLevel
	// TaskName 业务任务名（draft_passage/score…），D10 台账必填要素.
	TaskName string
	// ArtifactRef 关联产物 id（item_revision_id 等；单题成本归集键）.
	ArtifactRef string
	// PromptVersion 空→DefaultPromptVersion.
	PromptVersion string
	// Prompt 原始文本（可能含 PII；只在总线内存态存在，绝不入账出库）.
	Prompt string
	// MaxTokens 覆盖目标缺省路由参数；0 取目标缺省.
	MaxTokens int
	// Payload 为附加台账键（加性键值对，原样入账行 Payload，构造后不得再改写）：
	// 只允许确定性非敏感键值（字符数/参数指纹等），PII 与凭证禁止入内（D7/X3）.
	Payload map[string]string
}

// Response 是一次成功调用的交付结果（必然已有 ok 台账行对应）.
type Response struct {
	CallID       string
	Content      string
	Model        string
	ModelVersion string
	Provider     string
	TokenIn      int
	TokenOut     int
	CostCNY      float64
	DurationMS   float64
	Fallback     bool
	// StrippedKinds 是剥离器报告的 PII 类型列表（进程内观测面；不入账）.
	StrippedKinds []string
}

// Bus 是全系统唯一的生成式调用入口（宪法 D10/A5；T-W5-014 Go 落地）。
//
// Call 内固定的调用序列（无旁路）：allowlist 目标解析 → context 超时装订 →
// PII 剥离门 → 预算门 → 出站 → 同步落账 → 交付。三类合规失败（剥离失败/
// 预算超限/台账写败）都以 fail-closed 收口并各留一行 rejected 台账；目标未注册
// 因拿不到台账元数据而直接报错不留行（先于任何出站面发生）。
//
// 并发契约：targets 表受读写锁保护，Call 与 Register/Unregister 可并发；
// Ledger/Budget 的并发安全由实现负责（MemoryLedger/CumulativeBudget 内置锁）；
// redactor/ledger/budget/counter/now/idgen 构造后只读。
type Bus struct {
	mu      sync.RWMutex
	targets map[string]Target

	// redactor/ledger 必填——缺失即拒绝构造：不过 PII 门、不落账的总线本身
	// 就是违宪产物（D7/D10/X12），从构造期堵死.
	redactor Redactor
	ledger   Ledger
	// budget 为 nil 时表示尚未配置限额（W6 接线点）.
	budget Budget
	// counter 兜底 token 计数（nil→SimpleTokenCounter）；now/idgen 供测试注入.
	counter TokenCounter
	now     func() time.Time
	idgen   func() string
}

// NewBus 构造总线；redactor 与 ledger 缺失直接报错.
func NewBus(redactor Redactor, ledger Ledger) (*Bus, error) {
	if redactor == nil {
		return nil, fmt.Errorf("%w: redactor 未注入", ErrInvalidTarget)
	}
	if ledger == nil {
		return nil, fmt.Errorf("%w: ledger 未注入", ErrInvalidTarget)
	}
	return &Bus{
		targets:  make(map[string]Target),
		redactor: redactor,
		ledger:   ledger,
		now:      time.Now,
		idgen:    newRandomID,
	}, nil
}

// SetClock/SetIDGen/SetCounter 是测试注入点（生产留零值即可）；counter 控制
// 出站面未上报 usage 时的兜底 token 计法.
func (b *Bus) SetClock(now func() time.Time)   { b.now = now }
func (b *Bus) SetIDGen(gen func() string)      { b.idgen = gen }
func (b *Bus) SetCounter(counter TokenCounter) { b.counter = counter }
func (b *Bus) SetBudget(budget Budget)         { b.budget = budget }

// RegisterTarget 注册出站目标（allowlist 注入点）。同名重复注册报错而不覆盖：
// allowlist 变更必须显式的 Unregister→Register 两步（审计可读性优先于便利性）.
func (b *Bus) RegisterTarget(t Target) error {
	if err := t.validate(); err != nil {
		return err
	}
	b.mu.Lock()
	defer b.mu.Unlock()
	if _, exists := b.targets[t.Name]; exists {
		return fmt.Errorf("%w: %s", ErrDuplicateTarget, t.Name)
	}
	b.targets[t.Name] = t
	return nil
}

// UnregisterTarget 注销出站目标（配对 RegisterTarget，供 allowlist 显式变更）.
func (b *Bus) UnregisterTarget(name string) {
	b.mu.Lock()
	defer b.mu.Unlock()
	delete(b.targets, name)
}

// Call 实现 D10 总线语义（完整序列见 Bus 类型注释）.
func (b *Bus) Call(ctx context.Context, req Request) (*Response, error) {
	// 0) 请求要素校验：target/task_name 是台账必填列（D10「缺一不可」的应用门）.
	if req.Target == "" || req.TaskName == "" {
		return nil, fmt.Errorf("%w: target/task_name 必填", ErrInvalidRequest)
	}

	// 1) allowlist 目标解析（未注册=结构不可达）。此类编码错误发生在任何
	//    元数据可得以前，无法构成有意义的台账行，直接报错.
	b.mu.RLock()
	t, ok := b.targets[req.Target]
	b.mu.RUnlock()
	if !ok {
		return nil, fmt.Errorf("%w: %q", ErrUnknownTarget, req.Target)
	}

	callID := b.idgen()
	start := b.now()

	// 2) PII 剥离门（D7）：唯一放行条件是剥离成功；原文不出总线。
	sanitized, stripped, err := b.redactor.Redact(req.Prompt)
	if err != nil {
		// 底层错误文本不进返回值/台账（可能夹带 PII 原文），只留固定短码
		// rejected 行作为账面事实（验收 #1：台账记录失败原因不含原文 PII）；
		// 连 PromptHash 都不产生——未剥离的原始文本零固化。rejected 行写入
		// 也可能失败：join 上抛而非静默（errcheck 纪律=台账故障必须可观测）.
		rej := b.entry(callID, t, req, start, StatusRejected, ReasonRedactionFailed)
		rej.DurationMS = msSince(b.now, start)
		rerr := fmt.Errorf("%w: target=%s", ErrRedactionFailed, t.Name)
		if lerr := b.writeLedger(ctx, rej); lerr != nil {
			return nil, errors.Join(rerr, lerr)
		}
		return nil, rerr
	}

	// 3) 预算门：估算消耗须落在剩余额度内才放行（W6 硬顶前置骨架，非合规
	//    门——预算哨兵错误原样 Join 保留给调用方判别）.
	if b.budget != nil {
		maxOut := req.MaxTokens
		if maxOut == 0 {
			maxOut = t.MaxTokens
		}
		if berr := b.budget.Allow(ctx, UsageEstimate{
			InputTokens:     b.countOf().Count(sanitized),
			MaxOutputTokens: maxOut,
		}); berr != nil {
			rej := b.entry(callID, t, req, start, StatusRejected, ReasonBudgetExceeded)
			rej.DurationMS = msSince(b.now, start)
			gateErr := errors.Join(fmt.Errorf("%w: target=%s", ErrBudgetExceeded, t.Name), berr)
			if lerr := b.writeLedger(ctx, rej); lerr != nil {
				return nil, errors.Join(gateErr, lerr)
			}
			return nil, gateErr
		}
	}

	// 4) 出站：装订单次时限（调用方已有更紧截止时间时不放宽）.
	cctx, cancel := b.withTimeout(ctx, t.Timeout)
	defer cancel()

	out, cerr := t.Caller.Call(cctx, OutboundRequest{
		Target:      t.Name,
		Model:       t.Model,
		Prompt:      sanitized,
		MaxTokens:   orDefault(req.MaxTokens, t.MaxTokens),
		Temperature: t.Temperature,
	})
	durMS := msSince(b.now, start)

	// 5) 出站失败：failed 行 + 上抛（供应商错误文本可入异常链路——其契约禁止
	//    含凭证/prompt 原文；但一律不入台账 reason，短码收口）.
	if cerr != nil {
		fail := b.entry(callID, t, req, start, StatusFailed, ReasonCallerError)
		fail.DurationMS = durMS
		if lerr := b.writeLedger(ctx, fail); lerr != nil {
			return nil, errors.Join(fmt.Errorf("ai/bus: target=%s 出站失败: %w", t.Name, cerr), lerr)
		}
		return nil, fmt.Errorf("ai/bus: target=%s 出站失败: %w", t.Name, cerr)
	}

	// 6) 计量与计价：优先采信执行面上报的真实 usage，缺失走兜底计数器
	//    （冻结实现 StubClient 以 prompt 长度记 token 的兜底语义同构）.
	tokenIn := out.TokenIn
	if tokenIn <= 0 {
		tokenIn = b.countOf().Count(sanitized)
	}
	tokenOut := out.TokenOut
	if tokenOut <= 0 && out.Content != "" {
		tokenOut = b.countOf().Count(out.Content)
	}
	cost := ComputeCostCNY(t.Model, tokenIn, tokenOut)

	// 出站已实付：无论台账写入成败，真实用量都先回填预算账面（容量口径不因
	// 后续合规处置而失真；rejected/failed 无 token 实付，不回填）.
	if b.budget != nil {
		b.budget.Observe(tokenIn, tokenOut)
	}

	// 7) 同步落账（在交付之前）：写败=整次调用失败+产物丢弃，杜绝
	//    「先调用后补账」与「未账化交付」（本卡 fail-closed 语义第 3 条）.
	entry := b.entry(callID, t, req, start, StatusOK, "")
	entry.PromptHash = HashPrompt(sanitized)
	entry.Fallback = out.Fallback
	entry.TokenIn = tokenIn
	entry.TokenOut = tokenOut
	entry.CostCNY = cost
	entry.DurationMS = durMS
	if lerr := b.writeLedger(ctx, entry); lerr != nil {
		return nil, lerr // 内容永不交付：见 ErrLedgerWrite 注释
	}

	return &Response{
		CallID:        callID,
		Content:       out.Content,
		Model:         t.Model,
		ModelVersion:  t.ModelVersion,
		Provider:      t.Provider,
		TokenIn:       tokenIn,
		TokenOut:      tokenOut,
		CostCNY:       cost,
		DurationMS:    durMS,
		Fallback:      out.Fallback,
		StrippedKinds: stripped,
	}, nil
}

// entry 组装一条台账基行（status/reason/计量列随路径填充）。
// 刻意不让 PromptHash 在此产生：ok 行在第 7 步填 sanitized 的哈希；rejected
// 行（剥离失败）prompt_hash 留空串——原始未经剥离的文本连哈希都不固化，
// 使「失败原因不含原文 PII」在存储层无歧义.
func (b *Bus) entry(callID string, t Target, req Request, start time.Time, status CallStatus, reason string) LedgerEntry {
	pv := req.PromptVersion
	if pv == "" {
		pv = DefaultPromptVersion
	}
	return LedgerEntry{
		CallID:        callID,
		Modality:      t.Modality,
		TaskLevel:     req.TaskLevel,
		TaskName:      req.TaskName,
		Provider:      t.Provider,
		Model:         t.Model,
		ModelVersion:  t.ModelVersion,
		PromptHash:    "",
		PromptVersion: pv,
		Status:        status,
		Reason:        reason,
		ArtifactRef:   req.ArtifactRef,
		CallerName:    t.Name,
		CreatedAt:     start.UTC(),
		Payload:       req.Payload,
	}
}

// writeLedger 落账并标注失败来源（包装统一指向 ErrLedgerWrite 哨兵链）.
func (b *Bus) writeLedger(ctx context.Context, e LedgerEntry) error {
	if err := b.ledger.Record(ctx, e); err != nil {
		return fmt.Errorf("%w: call_id=%s status=%s: %v", ErrLedgerWrite, e.CallID, e.Status, err)
	}
	return nil
}

// withTimeout 按需装订默认时限：仅有默认时限且比父 ctx 更宽裕时才派生.
func (b *Bus) withTimeout(ctx context.Context, def time.Duration) (context.Context, func()) {
	if def <= 0 {
		return ctx, func() {}
	}
	if dl, ok := ctx.Deadline(); ok && dl.Sub(time.Now()) <= def {
		return ctx, func() {}
	}
	return context.WithTimeout(ctx, def)
}

func (b *Bus) countOf() TokenCounter {
	if b.counter == nil {
		return SimpleTokenCounter{}
	}
	return b.counter
}

func orDefault(v, def int) int {
	if v == 0 {
		return def
	}
	return v
}

// msSince 计算 start 到 now 的毫秒数（浮点毫秒，冻结实现 duration_ms 口径）.
func msSince(now func() time.Time, start time.Time) float64 {
	return float64(now().Sub(start)) / float64(time.Millisecond)
}

// newRandomID 生成调用 id：crypto/rand 16 字节 hex（32 位，ULID 位阶）.
func newRandomID() string {
	var buf [16]byte
	if _, err := rand.Read(buf[:]); err != nil {
		// crypto/rand 失败是平台级故障（go1.24+ 该 API 失败即 panic 语义），
		// 宁可炸也不产出可碰撞的台账主键.
		panic("ai/bus: crypto/rand 不可用: " + err.Error())
	}
	return hex.EncodeToString(buf[:])
}
