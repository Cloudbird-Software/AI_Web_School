package tts

import (
	"container/list"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"strconv"
	"sync"
	"unicode/utf8"

	"github.com/Cloudbird-Software/AI_Web_School/core/ai"
)

// 哨兵错误：调用方按 errors.Is 分支处理，不用字符串匹配。
var (
	// ErrUnknownBand 表示学段档位不在 L/M/H 词表内（编码面错误，零出站零账行）.
	ErrUnknownBand = errors.New("ai/tts: 未知学段档位（仅允许 L/M/H）")

	// ErrUnknownVoice 表示音色名不在 voice_profiles 配置内.
	ErrUnknownVoice = errors.New("ai/tts: 未知音色配置")

	// ErrEngineMismatch 表示音色绑定的引擎与 TTS 目标引擎不一致（配置漂移
	// fail-loud：一个总线目标只绑定一个引擎，混用引擎的音色须另开目标）.
	ErrEngineMismatch = errors.New("ai/tts: 音色引擎与目标引擎不一致")

	// ErrVoiceParamsMissing 表示 TTS 目标收到了不带语音参数的调用——总线目标
	// 只可经 Synthesizer 到达（语音参数经调用 ctx 装订），直连总线即契约违规.
	ErrVoiceParamsMissing = errors.New("ai/tts: 语音参数缺失（TTS 目标只能经 Synthesizer 调用）")

	// ErrInvalidConfig 表示 NewSynthesizer 构造参数非法（nil 部件等）.
	ErrInvalidConfig = errors.New("ai/tts: 构造参数非法")
)

// 台账 payload 加性键名（ai.LedgerEntry.Payload 的 TTS 契约面；TTS 特有字段
// 以加性键入账，不扩 ai_call_ledger 列——对齐不扩 schema）.
const (
	// PayloadCharCount 是剥离后出站文本的字符数（rune 计；冻结实现元数据
	// text_length 对齐——说出口的内容才有计量意义）.
	PayloadCharCount = "char_count"
	// PayloadVoiceFingerprint 是语音参数指纹（sha256 前 16 hex，覆盖
	// 引擎/音色名/voice_id/语速——台账 payload 保持紧凑且参数集演进时稳定）.
	PayloadVoiceFingerprint = "voice_fingerprint"
)

// 构造缺省值（Config 零值语义）.
const (
	DefaultTargetName    = "tts"
	DefaultProvider      = "mock"
	DefaultModel         = "mock" // TTS 的「模型」即合成引擎（D10 模型标识）
	DefaultModelVersion  = "v1"
	DefaultTaskName      = "tts_synthesize"
	DefaultCacheCapacity = 256
)

// Engine 是 TTS 出站合成契约（冻结实现 TTSEngine Protocol 对齐）：生产装配方
// 注入真实适配器（Azure/火山引擎等），测试注入 fake。实现契约与总线 Caller
// 同款纪律——只接收总线剥离后的文本，实现方不得再引入未剥离文本。
type Engine interface {
	Synthesize(ctx context.Context, text string, voiceID string, wpm int) ([]byte, error)
}

// voiceParams 是一次合成的语音参数（Synthesizer 解析学段/音色后经调用 ctx
// 装订给 EngineCaller；不经 OutboundRequest——总线统一请求面只认文本与路由
// 参数，modality 特有参数不污染 LLM 请求 schema）.
type voiceParams struct {
	voiceID string
	wpm     int
}

// ctxKey 是未导出的空结构键：包外无法伪造语音参数（契约面收在包内）.
type ctxKey struct{}

func withVoiceParams(ctx context.Context, vp voiceParams) context.Context {
	return context.WithValue(ctx, ctxKey{}, vp)
}

func voiceParamsFrom(ctx context.Context) (voiceParams, bool) {
	vp, ok := ctx.Value(ctxKey{}).(voiceParams)
	return vp, ok
}

// EngineCaller 把 Engine 适配成总线 Caller（ModalityTTS 目标的出站执行面）。
//
// 用量口径：TTS 无 token 语义，按真实规模计量——token_in = 剥离后字符数、
// token_out = 音频字节数（台账/预算需要非零计量面；对音频字节跑词法兜底
// 计数器反而是噪声）。音频字节经 OutboundResult.Content 字节保真承载
// （Go 的 string 可承载任意字节序列）.
type EngineCaller struct {
	Eng Engine
}

// Call 实现 ai.Caller.
func (c EngineCaller) Call(ctx context.Context, req ai.OutboundRequest) (ai.OutboundResult, error) {
	vp, ok := voiceParamsFrom(ctx)
	if !ok {
		return ai.OutboundResult{}, ErrVoiceParamsMissing
	}
	audio, err := c.Eng.Synthesize(ctx, req.Prompt, vp.voiceID, vp.wpm)
	if err != nil {
		return ai.OutboundResult{}, err
	}
	return ai.OutboundResult{
		Content:  string(audio),
		TokenIn:  utf8.RuneCountInString(req.Prompt),
		TokenOut: len(audio),
	}, nil
}

// Config 是 NewSynthesizer 的装配配置；零值合法（全缺省）.
type Config struct {
	// TargetName 总线目标名（空→DefaultTargetName；重复注册由总线拒）.
	TargetName string
	// Provider/Model/ModelVersion 是 D10 台账三要素（空→mock/v1；TTS 的模型
	// 即合成引擎，音色/语速走 payload 指纹）.
	Provider     string
	Model        string
	ModelVersion string
	// TaskName 台账任务名（空→DefaultTaskName）.
	TaskName string
	// BaseURL 为空表示进程内引擎通道；非空必须 https（总线准入强制，复用）.
	BaseURL string
	// CacheCapacity 缓存容量上限（<=0→DefaultCacheCapacity；LRU 淘汰）.
	CacheCapacity int
}

// TTSRequest 是业务方向 TTS 服务面发起的合成请求（冻结实现 tts_synthesize
// 入参对齐）。Text 为原始文本、可含 PII——原文只在总线内存态存在，出站面
// 收到的必然是剥离后文本（D7，fail-closed 在总线内）.
type TTSRequest struct {
	// TaskLevel 路由档位（空串=NULL，总线同语义）.
	TaskLevel ai.TaskLevel
	// TaskName 台账任务名（空→构造缺省）.
	TaskName string
	// PromptVersion prompt/模板版本（空→总线缺省 v1）.
	PromptVersion string
	// Text 待合成原始文本.
	Text string
	// GradeBand 学段档位 L/M/H（决定语速与默认音色）.
	GradeBand GradeBand
	// VoiceProfile 音色名（空→学段默认音色）.
	VoiceProfile string
}

// TTSResult 是一次合成的交付结果（冻结实现 TTSResult.metadata 字段族对齐）.
type TTSResult struct {
	// CallID 溯源到产出该音频的台账行（缓存命中时为首次合成的调用 id）.
	CallID string
	// AudioID 音频产物内容寻址 id（完整 sha256 hex；缓存键 == 台账 artifact_ref）.
	AudioID string
	// Audio 音频字节流（mock 引擎返回占位字节，生产返回 mp3/wav）.
	Audio []byte
	// 语音参数解析结果（学段配置注入面，A5）.
	GradeBand GradeBand
	Voice     string
	VoiceID   string
	Engine    string
	WPM       int
	// CharCount 剥离后出站文本字符数；EstimateMS 估算音频时长
	// （字符数×60000/wpm，冻结实现同式；真实时长由引擎决定）.
	CharCount  int
	EstimateMS int
	// Cached 是否缓存命中（命中零出站、零新账）.
	Cached bool
	// StrippedKinds 首次合成时总线剥离器报告的 PII 类型（观测面，不入账）.
	StrippedKinds []string
}

// Synthesizer 是 TTS 合成统一服务面：学段→语速/音色解析（A5）、总线调用
// （ModalityTTS，PII 剥离/预算/落账全在总线内）、内容寻址缓存（D3+LRU）。
// 构造后并发安全（缓存内置互斥锁；总线并发契约见 ai.Bus）.
type Synthesizer struct {
	bus      *ai.Bus
	red      ai.Redactor
	target   string
	taskName string
	engine   string
	cache    *lruCache
}

// NewSynthesizer 构造服务面并在总线上注册 ModalityTTS 目标（出站执行面为
// EngineCaller；BaseURL 非空时 https 强制由总线准入复用）。redactor 必须与
// 总线剥离器同实现（装配纪律见包注释）.
func NewSynthesizer(bus *ai.Bus, red ai.Redactor, eng Engine, cfg Config) (*Synthesizer, error) {
	if bus == nil || red == nil || eng == nil {
		return nil, fmt.Errorf("%w: bus/redactor/engine 不可为 nil", ErrInvalidConfig)
	}
	name := orDefaultStr(cfg.TargetName, DefaultTargetName)
	taskName := orDefaultStr(cfg.TaskName, DefaultTaskName)
	provider := orDefaultStr(cfg.Provider, DefaultProvider)
	model := orDefaultStr(cfg.Model, DefaultModel)
	version := orDefaultStr(cfg.ModelVersion, DefaultModelVersion)
	capacity := cfg.CacheCapacity
	if capacity <= 0 {
		capacity = DefaultCacheCapacity
	}
	// 准入校验（词表/https/凭证拦截/重名）全部复用总线——本包不另设第二套.
	if err := bus.RegisterTarget(ai.Target{
		Name:         name,
		Modality:     ai.ModalityTTS,
		Provider:     provider,
		Model:        model,
		ModelVersion: version,
		Caller:       EngineCaller{Eng: eng},
		BaseURL:      cfg.BaseURL,
	}); err != nil {
		return nil, err
	}
	return &Synthesizer{
		bus:      bus,
		red:      red,
		target:   name,
		taskName: taskName,
		engine:   model,
		cache:    newLRU(capacity),
	}, nil
}

// Synthesize 实现 TTS 合成统一入口（D3/D7/D10/X12）：
//
//  0. 学段/音色解析——编码面错误（同 ErrUnknownTarget 语义：发生在任何出站
//     元数据可得之前）零出站零账行；
//  1. 缓存键派生剥离——与总线同一确定性剥离器。为什么在服务面也剥：缓存键与
//     音频 id 必须是「说出口的内容」的函数，PII 差异不得产生第二份音频（D3）。
//     派生失败不在此拒绝——拒绝留账是总线的职责（X12 拒绝不留暗数），原文交
//     总线由其落 rejected 行并 fail-closed；
//  2. 内容寻址 id + 缓存查询——命中即零出站零新账（合成事实已由首次调用的
//     台账行覆盖，不重复落），且不消耗预算（无出站实付）；
//  3. 总线调用——剥离门/预算门/出站/同步落账全部在总线内；ArtifactRef =
//     音频内容寻址 id（总线 id == 台账产物 id == 缓存键），TTS 特有字段走
//     payload 加性键（char_count / voice_fingerprint，对齐不扩 schema）。
func (s *Synthesizer) Synthesize(ctx context.Context, req TTSRequest) (*TTSResult, error) {
	bandCfg, err := resolveBand(req.GradeBand)
	if err != nil {
		return nil, err
	}
	voiceName := bandCfg.DefaultVoice
	if req.VoiceProfile != "" {
		voiceName = req.VoiceProfile
	}
	vc, err := resolveVoice(voiceName)
	if err != nil {
		return nil, err
	}
	if vc.Engine != s.engine {
		return nil, fmt.Errorf("%w: 音色 %s 绑定引擎 %q，目标引擎为 %q", ErrEngineMismatch, vc.Name, vc.Engine, s.engine)
	}
	taskName := req.TaskName
	if taskName == "" {
		taskName = s.taskName
	}

	// 1) 键派生剥离（纯函数，零出站）.
	sanitized, _, redErr := s.red.Redact(req.Text)
	if redErr != nil {
		// 原文交总线：由总线落 rejected/redaction_failed 行并 fail-closed.
		if _, callErr := s.bus.Call(ctx, ai.Request{
			Target:        s.target,
			TaskLevel:     req.TaskLevel,
			TaskName:      taskName,
			PromptVersion: req.PromptVersion,
			Prompt:        req.Text,
		}); callErr != nil {
			return nil, callErr
		}
		// 可达即装配错配（总线剥离器与键派生剥离器实现不一致，总线放行了一
		// 次本面无法确认已剥离的调用）：产物按未审拒付——X12 无降级开关.
		return nil, fmt.Errorf("%w: 键派生剥离与总线剥离器实现不一致", ai.ErrRedactionFailed)
	}

	// 2) 内容寻址 id（D3）与缓存命中.
	audioID := audioContentID(sanitized, req.GradeBand, vc, bandCfg.WPM)
	charN := utf8.RuneCountInString(sanitized)
	if hit, ok := s.cache.get(audioID); ok {
		return hit.result(audioID, true), nil
	}

	// 3) 总线调用（ModalityTTS）：本服务面零台账写面。语音参数经调用 ctx
	//    装订给出站执行面（EngineCaller 解出；不经 OutboundRequest）.
	resp, err := s.bus.Call(withVoiceParams(ctx, voiceParams{voiceID: vc.VoiceID, wpm: bandCfg.WPM}), ai.Request{
		Target:        s.target,
		TaskLevel:     req.TaskLevel,
		TaskName:      taskName,
		PromptVersion: req.PromptVersion,
		Prompt:        req.Text,
		ArtifactRef:   audioID,
		Payload: map[string]string{
			PayloadCharCount:        strconv.Itoa(charN),
			PayloadVoiceFingerprint: voiceFingerprint(vc, bandCfg.WPM),
		},
	})
	if err != nil {
		return nil, err
	}

	entry := cacheEntry{
		callID:   resp.CallID,
		audio:    []byte(resp.Content),
		stripped: resp.StrippedKinds,
		voice:    vc,
		wpm:      bandCfg.WPM,
		band:     req.GradeBand,
		charN:    charN,
		estMS:    estimateMS(charN, bandCfg.WPM),
	}
	s.cache.put(audioID, entry)
	return entry.result(audioID, false), nil
}

// audioContentID 是音频产物内容寻址 id（D3）：相同剥离后文本+相同语音配置必得
// 相同 id。与冻结实现 compute_content_id 同公式族（text|voice|wpm|engine），
// 两处修复（本卡验收 #2）：① 完整 64 hex 摘要——截断 32 hex 是任务卡点名的
// 碰撞风险；② 键含学段档位与 voice_id（验收要求含音色/语速/学段参数）.
func audioContentID(sanitized string, band GradeBand, vc voiceConfig, wpm int) string {
	payload := sanitized + "|" + string(band) + "|" + vc.Name + "|" + vc.VoiceID + "|" + strconv.Itoa(wpm) + "|" + vc.Engine
	sum := sha256.Sum256([]byte(payload))
	return hex.EncodeToString(sum[:])
}

// voiceFingerprint 是语音参数指纹（台账 payload 加性键）：sha256 前 16 hex，
// 覆盖引擎/音色名/voice_id/语速。字段序固定（非 map 遍历），同参数必同指纹.
func voiceFingerprint(vc voiceConfig, wpm int) string {
	payload := "engine=" + vc.Engine + "|voice=" + vc.Name + "|voice_id=" + vc.VoiceID + "|wpm=" + strconv.Itoa(wpm)
	sum := sha256.Sum256([]byte(payload))
	return hex.EncodeToString(sum[:8])
}

// estimateMS 与冻结实现 estimate_duration_ms 同式：字符数×60000/wpm（中文
// 每字一「词」的简化估算，仅用于元数据与成本估算）.
func estimateMS(charN, wpm int) int {
	return charN * 60000 / wpm
}

func orDefaultStr(v, def string) string {
	if v == "" {
		return def
	}
	return v
}

// cacheEntry 是缓存的最小内容单元（重建 TTSResult 所需的全部字段）.
type cacheEntry struct {
	callID   string
	audio    []byte
	stripped []string
	voice    voiceConfig
	wpm      int
	band     GradeBand
	charN    int
	estMS    int
}

// result 重建交付结果；音频字节按值拷贝交付（调用方改写不得污染缓存）.
func (e cacheEntry) result(audioID string, cached bool) *TTSResult {
	return &TTSResult{
		CallID:        e.callID,
		AudioID:       audioID,
		Audio:         cloneBytes(e.audio),
		GradeBand:     e.band,
		Voice:         e.voice.Name,
		VoiceID:       e.voice.VoiceID,
		Engine:        e.voice.Engine,
		WPM:           e.wpm,
		CharCount:     e.charN,
		EstimateMS:    e.estMS,
		Cached:        cached,
		StrippedKinds: e.stripped,
	}
}

func cloneBytes(b []byte) []byte {
	out := make([]byte, len(b))
	copy(out, b)
	return out
}

// lruCache 是定容 LRU（container/list + map）：本卡对冻结实现缓存风险的修复
// 面——完整摘要键（键即 audioContentID）+ 容量上限 + LRU 淘汰（冻结实现为
// 进程级 dict：键截断 32 hex 且无界增长）。并发安全内置互斥锁.
type lruCache struct {
	cap  int
	mu   sync.Mutex
	ll   *list.List // 队首=最近使用
	ents map[string]*list.Element
}

type lruItem struct {
	key string
	val cacheEntry
}

func newLRU(capacity int) *lruCache {
	return &lruCache{cap: capacity, ll: list.New(), ents: make(map[string]*list.Element, capacity)}
}

func (c *lruCache) get(key string) (cacheEntry, bool) {
	c.mu.Lock()
	defer c.mu.Unlock()
	el, ok := c.ents[key]
	if !ok {
		return cacheEntry{}, false
	}
	c.ll.MoveToFront(el)
	item := el.Value.(lruItem)
	item.val.audio = cloneBytes(item.val.audio)
	return item.val, true
}

func (c *lruCache) put(key string, e cacheEntry) {
	c.mu.Lock()
	defer c.mu.Unlock()
	e.audio = cloneBytes(e.audio)
	if el, ok := c.ents[key]; ok {
		el.Value = lruItem{key: key, val: e}
		c.ll.MoveToFront(el)
		return
	}
	c.ents[key] = c.ll.PushFront(lruItem{key: key, val: e})
	if len(c.ents) > c.cap {
		if oldest := c.ll.Back(); oldest != nil {
			item := oldest.Value.(lruItem)
			delete(c.ents, item.key)
			c.ll.Remove(oldest)
		}
	}
}
