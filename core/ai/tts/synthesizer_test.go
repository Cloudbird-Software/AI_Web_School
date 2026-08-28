package tts

import (
	"context"
	"errors"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"
	"unicode/utf8"

	"github.com/Cloudbird-Software/AI_Web_School/core/ai"
)

// T-W5-015 验收测试：TTS 链路 PII 剥离与台账对齐。
//   - 剥离失败 fail-closed 且引擎零调用（验收 #1/#3；X12 拒绝留账）；
//   - 台账行含总线统一字段 + TTS payload 加性键（验收核心：对齐不扩 schema）；
//   - 缓存键完整内容寻址 + 定容 LRU，同参命中、异参不串音（验收 #2/#3）；
//   - 预算超限/供应商错误（429）传导；-race 绿。
// 全程 caller 注入 fake，不真实出网（零新依赖）。

// ── 测试假件 ─────────────────────────────────────────────────────────

// scriptedRed 按注入函数执行剥离（与总线共用同一实例——装配纪律的测试面）.
type scriptedRed struct {
	fn func(string) (string, []string, error)
}

func (r *scriptedRed) Redact(text string) (string, []string, error) { return r.fn(text) }

// failRed 构造恒失败的剥离器（模拟 ErrRedactUncertain 类不可判定故障）.
func failRed(err error) *scriptedRed {
	return &scriptedRed{fn: func(string) (string, []string, error) { return "", nil, err }}
}

// fakeEngine 记录每次合成入参；默认产出与冻结实现 MockTTSEngine 同构的确定性
// 占位音频（编码 voice_id/wpm/text，便于内容寻址与不串音断言）.
type fakeEngine struct {
	mu       sync.Mutex
	texts    []string
	voiceIDs []string
	wpms     []int
	err      error
}

func (f *fakeEngine) Synthesize(_ context.Context, text, voiceID string, wpm int) ([]byte, error) {
	f.mu.Lock()
	f.texts = append(f.texts, text)
	f.voiceIDs = append(f.voiceIDs, voiceID)
	f.wpms = append(f.wpms, wpm)
	f.mu.Unlock()
	if f.err != nil {
		return nil, f.err
	}
	return []byte("audio:" + voiceID + ":" + strconv.Itoa(wpm) + ":" + text), nil
}

func (f *fakeEngine) count() int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return len(f.texts)
}

func (f *fakeEngine) lastText() string {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.texts[len(f.texts)-1]
}

// newTestClock 确定时钟：每次 now() 前进 5ms（duration_ms 断言面）.
func newTestClock(start time.Time) func() time.Time {
	cur := start
	var mu sync.Mutex
	return func() time.Time {
		mu.Lock()
		defer mu.Unlock()
		cur = cur.Add(5 * time.Millisecond)
		return cur
	}
}

// newSynth 构造被测服务面（fake 引擎 + 内存台账），返回三件供断言.
func newSynth(t *testing.T, red ai.Redactor, eng Engine, mutate func(*Config)) (*Synthesizer, *ai.Bus, *ai.MemoryLedger) {
	t.Helper()
	led := ai.NewMemoryLedger()
	b, err := ai.NewBus(red, led)
	if err != nil {
		t.Fatalf("NewBus: %v", err)
	}
	b.SetClock(newTestClock(time.Date(2026, 8, 28, 10, 0, 0, 0, time.UTC)))
	cfg := Config{}
	if mutate != nil {
		mutate(&cfg)
	}
	s, err := NewSynthesizer(b, red, eng, cfg)
	if err != nil {
		t.Fatalf("NewSynthesizer: %v", err)
	}
	return s, b, led
}

// ── 验收 #1/#3：出站文本已脱敏 + 台账对齐（验收核心）──────────────────

func TestSynthesizeStripsPIIBeforeEngineAndWritesAlignedLedger(t *testing.T) {
	red := ai.RegexRedactor{}
	eng := &fakeEngine{}
	s, _, led := newSynth(t, red, eng, nil)

	raw := "学生张小明朗读课文，家长电话13812345678"
	res, err := s.Synthesize(context.Background(), TTSRequest{
		TaskLevel: ai.L2,
		Text:      raw,
		GradeBand: BandM,
	})
	if err != nil {
		t.Fatalf("Synthesize: %v", err)
	}

	// 供应商收到的文本必须已脱敏（验收 #3）.
	got := eng.lastText()
	sanitized, kinds, rerr := red.Redact(raw)
	if rerr != nil {
		t.Fatal(rerr)
	}
	if got != sanitized {
		t.Fatalf("出站文本与剥离结果不一致: %q vs %q", got, sanitized)
	}
	if strings.Contains(got, "张小明") || strings.Contains(got, "13812345678") {
		t.Fatalf("PII 残留于出站文本: %q", got)
	}
	if !strings.Contains(got, "学生") || !strings.Contains(got, "[PHONE]") {
		t.Fatalf("脱敏标记缺失: %q", got)
	}
	if len(kinds) == 0 || len(res.StrippedKinds) == 0 {
		t.Fatalf("剥离观测面为空: kinds=%v res=%v", kinds, res.StrippedKinds)
	}

	// 内容寻址 id（验收 #2：完整摘要，含音色/语速/学段参数）.
	vc := voiceTable["female_standard"]
	if res.AudioID != audioContentID(sanitized, BandM, vc, 140) {
		t.Fatalf("audio_id 与内容寻址公式不一致: %s", res.AudioID)
	}
	if len(res.AudioID) != 64 {
		t.Fatalf("audio_id 应为完整 sha256 hex（冻结实现截断 32 的修复面）: %s", res.AudioID)
	}
	if res.WPM != 140 || res.Voice != "female_standard" || res.VoiceID != "voice-female-standard" ||
		res.Engine != "mock" || res.GradeBand != BandM {
		t.Fatalf("语音参数解析错误: %+v", res)
	}
	if res.CharCount != utf8.RuneCountInString(sanitized) {
		t.Fatalf("char_count = %d, want %d", res.CharCount, utf8.RuneCountInString(sanitized))
	}
	if want := res.CharCount * 60000 / 140; res.EstimateMS != want {
		t.Fatalf("estimate_ms = %d, want %d", res.EstimateMS, want)
	}
	if res.Cached || res.CallID == "" || len(res.Audio) == 0 {
		t.Fatalf("首次合成形态错误: %+v", res)
	}

	// 台账对齐（验收核心）：恰一行（Bus 内建，不重复落），统一字段 + payload 加性键.
	rows := led.Snapshot()
	if len(rows) != 1 {
		t.Fatalf("台账行数 = %d, want 1（Bus 内建落账）", len(rows))
	}
	e := rows[0]
	if e.Modality != ai.ModalityTTS || e.TaskName != DefaultTaskName || e.TaskLevel != ai.L2 ||
		e.Provider != DefaultProvider || e.Model != DefaultModel || e.ModelVersion != DefaultModelVersion ||
		e.Status != ai.StatusOK || e.CallerName != DefaultTargetName || e.Reason != "" || e.Fallback {
		t.Fatalf("台账统一字段错误: %+v", e)
	}
	if e.PromptHash != ai.HashPrompt(sanitized) || e.PromptHash == ai.HashPrompt(raw) {
		t.Fatalf("prompt_hash 必须是剥离后文本的指纹: %q", e.PromptHash)
	}
	if e.PromptVersion != ai.DefaultPromptVersion {
		t.Fatalf("prompt_version = %q, want %q", e.PromptVersion, ai.DefaultPromptVersion)
	}
	if e.ArtifactRef != res.AudioID {
		t.Fatalf("产物 id 未对齐音频内容寻址 id: %q vs %q", e.ArtifactRef, res.AudioID)
	}
	// TTS 用量口径：token_in=字符数、token_out=音频字节数（EngineCaller 上报）.
	if e.TokenIn != res.CharCount || e.TokenOut != len(res.Audio) {
		t.Fatalf("TTS 用量计量错误: in=%d out=%d", e.TokenIn, e.TokenOut)
	}
	if cost := ai.ComputeCostCNY(DefaultModel, e.TokenIn, e.TokenOut); e.CostCNY != cost {
		t.Fatalf("成本字段未按统一口径计算: %v vs %v", e.CostCNY, cost)
	}
	if e.DurationMS < 0 {
		t.Fatalf("duration_ms 为负: %v", e.DurationMS)
	}
	if e.CreatedAt.IsZero() {
		t.Fatal("台账行缺时间戳")
	}
	if e.Payload[PayloadCharCount] != strconv.Itoa(res.CharCount) {
		t.Fatalf("payload char_count = %q, want %q", e.Payload[PayloadCharCount], strconv.Itoa(res.CharCount))
	}
	if e.Payload[PayloadVoiceFingerprint] != voiceFingerprint(vc, 140) {
		t.Fatalf("payload voice_fingerprint = %q, want %q",
			e.Payload[PayloadVoiceFingerprint], voiceFingerprint(vc, 140))
	}
	if e.Payload[PayloadVoiceFingerprint] == "" || len(e.Payload[PayloadVoiceFingerprint]) != 16 {
		t.Fatalf("语音参数指纹应为 sha256 前 16 hex: %q", e.Payload[PayloadVoiceFingerprint])
	}
}

// ── 验收 #1/#3：剥离失败 fail-closed，零出站（引擎零调用）─────────────

func TestSynthesizeRedactionFailureFailsClosedWithZeroEngineCalls(t *testing.T) {
	red := failRed(ai.ErrRedactUncertain)
	eng := &fakeEngine{}
	s, _, led := newSynth(t, red, eng, nil)

	res, err := s.Synthesize(context.Background(), TTSRequest{
		Text: "学生张三 电话13812345678", GradeBand: BandL,
	})
	if res != nil || !errors.Is(err, ai.ErrRedactionFailed) {
		t.Fatalf("err = %v, want ErrRedactionFailed 链；res=%v", err, res)
	}
	if eng.count() != 0 {
		t.Fatalf("剥离失败仍发生合成 ×%d（零出站被破坏）", eng.count())
	}
	rows := led.Snapshot()
	if len(rows) != 1 || rows[0].Status != ai.StatusRejected || rows[0].Reason != ai.ReasonRedactionFailed {
		t.Fatalf("应有恰一条 rejected/redaction_failed 行: %+v", rows)
	}
	// X12：拒绝也是账面事实，但零固化——原文指纹/产物 id/payload 一概不留.
	if rows[0].PromptHash != "" || rows[0].ArtifactRef != "" || len(rows[0].Payload) != 0 {
		t.Fatalf("rejected 行不得固化指纹/产物 id/payload: %+v", rows[0])
	}
}

func TestSynthesizeInvalidUTF8FailsClosedThroughRealRedactor(t *testing.T) {
	eng := &fakeEngine{}
	s, _, led := newSynth(t, ai.RegexRedactor{}, eng, nil)

	_, err := s.Synthesize(context.Background(), TTSRequest{
		Text: "学生张三\xff\xfe朗读", GradeBand: BandM,
	})
	if !errors.Is(err, ai.ErrRedactionFailed) {
		t.Fatalf("非合法 UTF-8 未被拒绝: %v", err)
	}
	// 总线契约：剥离器底层错误不进错误链（错误文本可能夹带 PII 原文），只有
	// 固定哨兵可达——错误消息同样不得残留原文.
	if strings.Contains(err.Error(), "张三") || strings.Contains(err.Error(), "\xff") {
		t.Fatalf("错误消息泄漏原文: %v", err)
	}
	if eng.count() != 0 {
		t.Fatalf("非法输入仍出站 ×%d", eng.count())
	}
	rows := led.Snapshot()
	if len(rows) != 1 || rows[0].Status != ai.StatusRejected || rows[0].Reason != ai.ReasonRedactionFailed {
		t.Fatalf("缺 rejected 行: %+v", rows)
	}
}

// ── 预算超限 / 供应商错误传导 ────────────────────────────────────────

func TestSynthesizeBudgetExceededPropagates(t *testing.T) {
	eng := &fakeEngine{}
	s, b, led := newSynth(t, ai.RegexRedactor{}, eng, nil)
	b.SetBudget(ai.NewCumulativeBudget(1)) // 任何估算都超限

	res, err := s.Synthesize(context.Background(), TTSRequest{Text: "朗读课文", GradeBand: BandM})
	if res != nil || !errors.Is(err, ai.ErrBudgetExceeded) {
		t.Fatalf("err = %v, want ErrBudgetExceeded 链；res=%v", err, res)
	}
	if eng.count() != 0 {
		t.Fatalf("预算超限仍出站 ×%d", eng.count())
	}
	rows := led.Snapshot()
	if len(rows) != 1 || rows[0].Status != ai.StatusRejected || rows[0].Reason != ai.ReasonBudgetExceeded {
		t.Fatalf("缺 rejected/budget_exceeded 行: %+v", rows)
	}
}

func TestSynthesizeProviderErrorPropagatesAndFailsRow(t *testing.T) {
	engErr := errors.New("tts provider: 429 rate limited")
	eng := &fakeEngine{err: engErr}
	s, _, led := newSynth(t, ai.RegexRedactor{}, eng, nil)

	res, err := s.Synthesize(context.Background(), TTSRequest{Text: "课文内容", GradeBand: BandM})
	if res != nil || !errors.Is(err, engErr) || !strings.Contains(err.Error(), "429") {
		t.Fatalf("供应商错误(429)未传导: res=%v err=%v", res, err)
	}
	rows := led.Snapshot()
	if len(rows) != 1 || rows[0].Status != ai.StatusFailed || rows[0].Reason != ai.ReasonCallerError {
		t.Fatalf("缺 failed/caller_error 行: %+v", rows)
	}
	// 失败不得污染缓存：修复后同参重试应再次出站.
	eng.err = nil
	if _, err := s.Synthesize(context.Background(), TTSRequest{Text: "课文内容", GradeBand: BandM}); err != nil {
		t.Fatalf("失败后重试: %v", err)
	}
	if eng.count() != 2 {
		t.Fatalf("失败调用不得作为缓存命中来源，实际出站 ×%d", eng.count())
	}
}

// ── 验收 #2/#3：缓存——同参命中、异参不串音、定容 LRU ────────────────

func TestCacheSameParamsHitDifferentParamsNoCrossTalk(t *testing.T) {
	eng := &fakeEngine{}
	s, _, _ := newSynth(t, ai.RegexRedactor{}, eng, nil)
	ctx := context.Background()

	r1, err := s.Synthesize(ctx, TTSRequest{Text: "同一段文本", GradeBand: BandM})
	if err != nil {
		t.Fatal(err)
	}
	r2, err := s.Synthesize(ctx, TTSRequest{Text: "同一段文本", GradeBand: BandM})
	if err != nil {
		t.Fatal(err)
	}
	if !r2.Cached || r2.AudioID != r1.AudioID || string(r2.Audio) != string(r1.Audio) || r2.CallID != r1.CallID {
		t.Fatalf("同参数未命中缓存: r1=%+v r2=%+v", r1, r2)
	}
	if eng.count() != 1 {
		t.Fatalf("命中缓存仍出站 ×%d", eng.count())
	}

	// 不同学段（语速不同）→ 不同 id、不同音频，不串音.
	r3, err := s.Synthesize(ctx, TTSRequest{Text: "同一段文本", GradeBand: BandL})
	if err != nil {
		t.Fatal(err)
	}
	if r3.Cached || r3.AudioID == r1.AudioID || string(r3.Audio) == string(r1.Audio) || r3.WPM != 120 {
		t.Fatalf("不同学段发生串音: %+v", r3)
	}
	if eng.count() != 2 {
		t.Fatalf("出站次数 = %d, want 2", eng.count())
	}

	// 同文本同学段、不同音色 → 不同 id.
	r4, err := s.Synthesize(ctx, TTSRequest{Text: "同一段文本", GradeBand: BandM, VoiceProfile: "female_news"})
	if err != nil {
		t.Fatal(err)
	}
	if r4.AudioID == r1.AudioID || r4.VoiceID != "voice-female-news" {
		t.Fatalf("不同音色未区分: %+v", r4)
	}

	// 不同文本（同学段）→ 不同 id.
	r5, err := s.Synthesize(ctx, TTSRequest{Text: "另一段文本", GradeBand: BandM})
	if err != nil {
		t.Fatal(err)
	}
	if r5.AudioID == r1.AudioID || r5.Cached {
		t.Fatalf("不同文本撞 id: %+v", r5)
	}
	if eng.count() != 4 {
		t.Fatalf("出站次数 = %d, want 4", eng.count())
	}
}

func TestCacheCapacityBoundAndLRUEviction(t *testing.T) {
	eng := &fakeEngine{}
	s, _, _ := newSynth(t, ai.RegexRedactor{}, eng, func(c *Config) { c.CacheCapacity = 2 })
	ctx := context.Background()
	sy := func(txt string) *TTSResult {
		t.Helper()
		res, err := s.Synthesize(ctx, TTSRequest{Text: txt, GradeBand: BandM})
		if err != nil {
			t.Fatal(err)
		}
		return res
	}

	sy("甲")
	sy("乙")
	sy("丙") // 容量 2 → 甲被 LRU 淘汰
	if eng.count() != 3 {
		t.Fatalf("出站次数 = %d, want 3", eng.count())
	}
	if res := sy("甲"); res.Cached {
		t.Fatal("被淘汰的甲不得命中（容量上限生效）")
	}
	if eng.count() != 4 {
		t.Fatalf("淘汰后重合成未出站，次数 = %d", eng.count())
	}
	if res := sy("丙"); !res.Cached {
		t.Fatal("近期使用的丙应仍在缓存（LRU 而非 FIFO/随机）")
	}
	if eng.count() != 4 {
		t.Fatalf("丙应命中缓存，出站次数 = %d", eng.count())
	}
}

// ── 并发（-race）：共享服务面多协程合成，账行与出站一一对应 ──────────

func TestConcurrentSynthesizeRaceCleanAndAccounted(t *testing.T) {
	eng := &fakeEngine{}
	s, _, led := newSynth(t, ai.RegexRedactor{}, eng, func(c *Config) { c.CacheCapacity = 8 })
	texts := []string{"文本一学生张三", "文本二李四13812345678", "文本三", "文本四"}

	const n = 32
	var wg sync.WaitGroup
	ids := make([]string, n)
	errs := make(chan error, n)
	for i := 0; i < n; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			res, err := s.Synthesize(context.Background(), TTSRequest{
				Text: texts[i%len(texts)], GradeBand: BandM,
			})
			if err != nil {
				errs <- err
				return
			}
			ids[i] = res.AudioID
		}(i)
	}
	wg.Wait()
	close(errs)
	for err := range errs {
		t.Fatal(err)
	}

	// D3 并发一致：同文本（含剥离后同内容）必得同 id.
	byText := make(map[string]string)
	for i := range ids {
		key := texts[i%len(texts)]
		if prev, ok := byText[key]; ok && prev != ids[i] {
			t.Fatalf("同文本并发合成 id 漂移: %q → %q vs %q", key, prev, ids[i])
		}
		byText[key] = ids[i]
	}
	// 台账全覆盖：每行 ok/tts 且 artifact_ref 对齐内容寻址 id，行数==出站次数.
	rows := led.Snapshot()
	if len(rows) != eng.count() {
		t.Fatalf("台账行数 %d != 出站次数 %d", len(rows), eng.count())
	}
	for _, r := range rows {
		if r.Status != ai.StatusOK || r.Modality != ai.ModalityTTS || r.ArtifactRef == "" {
			t.Fatalf("意外账行: %+v", r)
		}
	}
}

// ── 目标注入与总线准入复用 ───────────────────────────────────────────

func TestNewSynthesizerReusesHTTPSForcingOnTargetRegistration(t *testing.T) {
	led := ai.NewMemoryLedger()
	b, err := ai.NewBus(ai.RegexRedactor{}, led)
	if err != nil {
		t.Fatal(err)
	}
	eng := &fakeEngine{}

	_, err = NewSynthesizer(b, ai.RegexRedactor{}, eng, Config{BaseURL: "http://tts.example.com"})
	if !errors.Is(err, ai.ErrInsecureOutbound) {
		t.Fatalf("TTS 目标 HTTP 出站未被拒: %v", err)
	}
	_, err = NewSynthesizer(b, ai.RegexRedactor{}, eng, Config{BaseURL: "https://user:pw@tts.example.com"})
	if !errors.Is(err, ai.ErrCredentialInURL) {
		t.Fatalf("URL 内嵌凭证未被拒: %v", err)
	}
	s, err := NewSynthesizer(b, ai.RegexRedactor{}, eng, Config{BaseURL: "https://tts.example.com"})
	if err != nil {
		t.Fatalf("合法 https 注册失败: %v", err)
	}
	if _, err := s.Synthesize(context.Background(), TTSRequest{Text: "朗读", GradeBand: BandH}); err != nil {
		t.Fatalf("https 目标合成失败: %v", err)
	}
}

func TestNewSynthesizerRejectsNilPartsAndDuplicateTarget(t *testing.T) {
	red := ai.RegexRedactor{}
	b, err := ai.NewBus(red, ai.NewMemoryLedger())
	if err != nil {
		t.Fatal(err)
	}
	if _, err := NewSynthesizer(b, red, nil, Config{}); !errors.Is(err, ErrInvalidConfig) {
		t.Fatalf("nil engine 未拒: %v", err)
	}
	if _, err := NewSynthesizer(nil, red, &fakeEngine{}, Config{}); !errors.Is(err, ErrInvalidConfig) {
		t.Fatalf("nil bus 未拒: %v", err)
	}
	eng := &fakeEngine{}
	if _, err := NewSynthesizer(b, red, eng, Config{}); err != nil {
		t.Fatal(err)
	}
	// 同名重复注册必须报错而非静默覆盖（allowlist 变更是显式动作）.
	if _, err := NewSynthesizer(b, red, eng, Config{}); !errors.Is(err, ai.ErrDuplicateTarget) {
		t.Fatalf("重复注册未拒: %v", err)
	}
}

func TestUnknownBandOrVoiceRejectedBeforeOutbound(t *testing.T) {
	eng := &fakeEngine{}
	s, _, led := newSynth(t, ai.RegexRedactor{}, eng, nil)
	ctx := context.Background()

	if _, err := s.Synthesize(ctx, TTSRequest{Text: "x", GradeBand: GradeBand("X")}); !errors.Is(err, ErrUnknownBand) {
		t.Fatalf("未知学段未拒: %v", err)
	}
	if _, err := s.Synthesize(ctx, TTSRequest{Text: "x", GradeBand: BandM, VoiceProfile: "no_such_voice"}); !errors.Is(err, ErrUnknownVoice) {
		t.Fatalf("未知音色未拒: %v", err)
	}
	if eng.count() != 0 || len(led.Snapshot()) != 0 {
		t.Fatalf("编码面错误应零出站零账行: 出站×%d 账行%d", eng.count(), len(led.Snapshot()))
	}
}

// 契约面：TTS 总线目标不可绕过 Synthesizer 直连——语音参数经 ctx 装订，
// 直调即 ErrVoiceParamsMissing（fail-closed，留 failed 账行）.
func TestTTSTargetUnreachableWithoutSynthesizer(t *testing.T) {
	eng := &fakeEngine{}
	s, b, led := newSynth(t, ai.RegexRedactor{}, eng, nil)
	_ = s

	_, err := b.Call(context.Background(), ai.Request{Target: DefaultTargetName, TaskName: "direct", Prompt: "直接调用"})
	if !errors.Is(err, ErrVoiceParamsMissing) {
		t.Fatalf("直连未拒: %v", err)
	}
	if eng.count() != 0 {
		t.Fatalf("直连发生了合成 ×%d", eng.count())
	}
	rows := led.Snapshot()
	if len(rows) != 1 || rows[0].Status != ai.StatusFailed || rows[0].Reason != ai.ReasonCallerError {
		t.Fatalf("缺 failed/caller_error 行: %+v", rows)
	}
}

// ── 配置镜像锁定：与冻结 voice_profiles.yaml 逐项对齐 ────────────────

func TestProfilesMatchFrozenVoiceProfilesYAML(t *testing.T) {
	for band, want := range map[GradeBand]struct {
		wpm   int
		voice string
	}{
		BandL: {120, "female_gentle"},
		BandM: {140, "female_standard"},
		BandH: {160, "male_standard"},
	} {
		got, err := resolveBand(band)
		if err != nil {
			t.Fatalf("%s: %v", band, err)
		}
		if got.WPM != want.wpm || got.DefaultVoice != want.voice {
			t.Fatalf("%s = %d/%s, want %d/%s", band, got.WPM, got.DefaultVoice, want.wpm, want.voice)
		}
	}
	if _, err := resolveBand("X"); !errors.Is(err, ErrUnknownBand) {
		t.Fatalf("未知档位未拒: %v", err)
	}
	for name, want := range map[string]struct{ engine, voiceID string }{
		"female_gentle":   {"mock", "voice-female-gentle"},
		"female_standard": {"mock", "voice-female-standard"},
		"male_standard":   {"mock", "voice-male-standard"},
		"female_news":     {"mock", "voice-female-news"},
	} {
		vc, err := resolveVoice(name)
		if err != nil {
			t.Fatalf("%s: %v", name, err)
		}
		if vc.Engine != want.engine || vc.VoiceID != want.voiceID {
			t.Fatalf("%s = %s/%s, want %s/%s", name, vc.Engine, vc.VoiceID, want.engine, want.voiceID)
		}
	}
	if _, err := resolveVoice("ghost"); !errors.Is(err, ErrUnknownVoice) {
		t.Fatalf("未知音色未拒: %v", err)
	}
}

// 引擎绑定漂移 fail-loud：音色绑定的引擎与目标引擎不一致即拒绝.
func TestEngineMismatchFailsLoud(t *testing.T) {
	eng := &fakeEngine{}
	s, _, _ := newSynth(t, ai.RegexRedactor{}, eng, func(c *Config) { c.Model = "azure-tts" })
	_, err := s.Synthesize(context.Background(), TTSRequest{Text: "x", GradeBand: BandM})
	if !errors.Is(err, ErrEngineMismatch) {
		t.Fatalf("引擎漂移未拒: %v", err)
	}
	if eng.count() != 0 {
		t.Fatalf("引擎漂移仍出站 ×%d", eng.count())
	}
}

// 编译期锚定：EngineCaller 必须兑现总线 Caller 契约（装配直通的假设防线）.
var _ ai.Caller = EngineCaller{}
