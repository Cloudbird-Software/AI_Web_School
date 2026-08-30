package audio

import (
	"context"
	"errors"
	"fmt"
	"strconv"
	"sync"

	"github.com/Cloudbird-Software/AI_Web_School/core/ai/tts"
)

// producer.go 承载 TTS 音频产线编排（冻结实现 src/core/audio/producer.py
// T-W4-022 的 Go 重锚定，架构 v2 §4.6）。
//
// 为什么 audio_id 直接透传 TTS 总线结果：D3 内容寻址——相同文本+相同配置得
// 相同 id。core/ai/tts.Synthesizer 已按修复面公式（剥离后文本|学段|音色|
// voice_id|语速|引擎，完整摘要）计算 AudioID，产线直接透传，保证「总线 id ==
// 产线 id == 存储寻址」三位一体，码内可回溯（架构 §4.6）。冻结实现以第二套
// 公式重算做防御断言；Go 侧公式单点收敛于 tts 包（其截断/PII 键缺陷已修复），
// 双实现互验由 content_addressing 黄金测试承担——产线降级为 id 非空的
// 结构性校验（总线契约失败即 fail-loud）。
//
// 为什么 writer 可注入：对象存储是副作用（写 MinIO/本地），单元测试用
// MockAudioStorageWriter 返回确定性 URL，不触达真实存储；生产替换为 MinIO
// 适配器。批次/去重/缓存语义：单题合成经 Synthesizer 内容寻址 LRU（同参
// 命中零出站零新账）；批次入口对同参任务显式合流（确定性去重）。

// 哨兵错误：调用方按 errors.Is 分支处理.
var (
	// ErrNilSynthesizer 表示构造 Producer 未注入 TTS 服务面.
	ErrNilSynthesizer = errors.New("audio: synthesizer 不可为 nil")
	// ErrEmptyAudioID 表示 TTS 总线返回空音频 id（总线契约破坏，fail-loud）.
	ErrEmptyAudioID = errors.New("audio: TTS 总线返回空音频 id")
	// ErrEmptyProduceText 表示待合成文本为空（冻结实现 pydantic min_length 面）.
	ErrEmptyProduceText = errors.New("audio: text 不能为空")
)

// 音频素材对象（冻结实现 AudioAsset / 验收 #1）.
type AudioAsset struct {
	// AudioID 内容寻址 id（D3，与 TTS 总线 AudioID 一致）.
	AudioID string
	// URL 对象存储可访问 URL（消费层播放/二维码/点读的源头）.
	URL string
	// ContentHash 音频字节流哈希（存储去重与完整性校验）.
	ContentHash string
	// DurationMS 音频时长（毫秒，TTS 总线估算）.
	DurationMS int
	// TTSMetadata TTS 合成元数据（voice/voice_id/engine/wpm/duration_ms/
	// char_count）。冻结实现键 text_length（原文长度）在 Go 侧为 char_count
	//（剥离后文本 rune 数）——D3：id 与元数据的「内容」口径是剥离后文本.
	TTSMetadata map[string]string
	// Text / VoiceProfile / GradeBand 溯源字段（便于校验门与组卷引用）.
	Text         string
	VoiceProfile string
	GradeBand    tts.GradeBand
	// Audio 引擎实际产出的字节（消费层点读/播放需要；存储后可置空以释放内存）.
	Audio []byte
}

// AudioStorageWriter 是音频对象存储写入契约（冻结实现 AudioStorageWriter
// Protocol；生产替换为 MinIO/S3 适配器）。
//
// Write 写入音频字节，返回可访问 URL——内容寻址：同 audio_id 幂等.
type AudioStorageWriter interface {
	Write(audioID string, audio []byte) (string, error)
}

// Mock 存储写入器常量（冻结实现 MockAudioStorageWriter：URL 设计
// {BASE_URL}/{BUCKET}/{audio_id}.mp3——audio_id 是内容寻址 id，同 id 同 URL
// 幂等，便于消费层按 id 定位音频）.
const (
	MockBucket  = "audio-listening"
	MockBaseURL = "http://localhost:9000"
)

// MockAudioStorageWriter 桩存储写入器：不触达真实存储，返回确定性 URL
// （验收 #4：mock）。真实存储写入由生产适配器负责（bytes 入参保留契约形态）.
type MockAudioStorageWriter struct{}

// Write 实现 AudioStorageWriter（mock 不读字节，仅按 id 生成 URL）.
func (MockAudioStorageWriter) Write(audioID string, _ []byte) (string, error) {
	return MockBaseURL + "/" + MockBucket + "/" + audioID + ".mp3", nil
}

// ProduceRequest 是单题生产请求（冻结实现 produce_audio 入参对齐）。
// Text 应已剥离 PII 的说法在 Go 侧更弱：TTSRequest.Text 可含 PII，总线
// fail-closed 剥离（D7）——本面不感知 PII 语义。VoiceProfile 空 → 学段默认
// 音色（由 TTS 总线解析）.
type ProduceRequest struct {
	Text         string
	VoiceProfile string
	GradeBand    tts.GradeBand
}

// Producer 是音频产线服务面：TTS 合成（经 Synthesizer 总线）+ 对象存储包装
// + 内容寻址版本化。构造后并发安全（Synthesizer 内置缓存互斥锁）.
type Producer struct {
	syn    *tts.Synthesizer
	writer AudioStorageWriter
}

// NewProducer 构造产线。writer 为 nil → MockAudioStorageWriter（验收 #4：
// 单元测试 hermetic 默认面）；syn 必填.
func NewProducer(syn *tts.Synthesizer, writer AudioStorageWriter) (*Producer, error) {
	if syn == nil {
		return nil, ErrNilSynthesizer
	}
	if writer == nil {
		writer = MockAudioStorageWriter{}
	}
	return &Producer{syn: syn, writer: writer}, nil
}

// Produce 合成单个音频素材（验收 #1/#2）：
// TTS 总线合成 → id 非空结构校验 → 存储写入（幂等）→ AudioAsset 包装
// （content_hash/duration_ms/tts_metadata/溯源字段）。
func (p *Producer) Produce(ctx context.Context, req ProduceRequest) (*AudioAsset, error) {
	if req.Text == "" {
		return nil, ErrEmptyProduceText
	}
	res, err := p.syn.Synthesize(ctx, tts.TTSRequest{
		Text:         req.Text,
		GradeBand:    req.GradeBand,
		VoiceProfile: req.VoiceProfile,
	})
	if err != nil {
		return nil, err
	}
	if res.AudioID == "" {
		return nil, ErrEmptyAudioID
	}
	url, err := p.writer.Write(res.AudioID, res.Audio)
	if err != nil {
		return nil, fmt.Errorf("audio: 音频存储写入失败（audio_id=%s）: %w", res.AudioID, err)
	}
	return newAudioAsset(req.Text, res, url), nil
}

// newAudioAsset 从 TTS 结果包装素材（audio/metadata 深拷贝——调用方改写不得
// 交叉污染）.
func newAudioAsset(text string, res *tts.TTSResult, url string) *AudioAsset {
	return &AudioAsset{
		AudioID:     res.AudioID,
		URL:         url,
		ContentHash: ComputeContentHash(res.Audio),
		DurationMS:  res.EstimateMS,
		TTSMetadata: map[string]string{
			"voice":       res.Voice,
			"voice_id":    res.VoiceID,
			"engine":      res.Engine,
			"wpm":         strconv.Itoa(res.WPM),
			"duration_ms": strconv.Itoa(res.EstimateMS),
			"char_count":  strconv.Itoa(res.CharCount),
		},
		Text:         text,
		VoiceProfile: res.Voice,
		GradeBand:    res.GradeBand,
		Audio:        cloneAudioBytes(res.Audio),
	}
}

func cloneAudioBytes(b []byte) []byte {
	out := make([]byte, len(b))
	copy(out, b)
	return out
}

// ProduceJob 是批量生产任务单元（冻结实现 AudioProduceJob）。
// VoiceProfile 空 → 学段默认音色.
type ProduceJob struct {
	Text         string
	VoiceProfile string
	GradeBand    tts.GradeBand
}

// 批次状态值域（冻结实现 status Literal）.
const (
	BatchCompleted = "completed" // 全部成功
	BatchPartial   = "partial"   // 部分失败
)

// BatchFailure 是单个任务失败记录（不中断整批，记录后继续）.
type BatchFailure struct {
	JobIndex int
	Text     string
	Error    string
}

// BatchResult 是批量生产结果：任务状态 + 成功产物列表 + 失败列表.
// Results 按任务序排列（仅成功者）；Failures 按任务序排列.
type BatchResult struct {
	Status    string
	Results   []*AudioAsset
	Failures  []BatchFailure
	Total     int
	Succeeded int
	Failed    int
}

// ProduceBatch 并发批量生产音频素材（验收 #3；冻结实现 produce_audio_batch
// 的 asyncio.gather 对齐）。
//
// 去重语义（对齐并显式化）：冻结实现依赖 TTS 缓存隐式去重——并发 gather 下
// 同参任务存在重复合成竞态；Go 产线在批次入口按 (text, voice, band) 分组
// 合流，每组只合成/写入一次（引擎调用数 = 同参组数，确定性去重），组内各
// 任务获得字节独立的 AudioAsset（无别名共享）。任一任务失败不中断整批，
// 记录到 Failures（同参组整体失败 → 组内各任务各记一条）。
func (p *Producer) ProduceBatch(ctx context.Context, jobs []ProduceJob) *BatchResult {
	out := &BatchResult{Total: len(jobs)}
	if len(jobs) == 0 {
		out.Status = BatchCompleted
		return out
	}

	type batchKey struct {
		text  string
		voice string
		band  tts.GradeBand
	}
	firstSeen := make([]batchKey, 0, len(jobs))
	groups := make(map[batchKey][]int, len(jobs))
	for i, j := range jobs {
		k := batchKey{text: j.Text, voice: j.VoiceProfile, band: j.GradeBand}
		if _, ok := groups[k]; !ok {
			firstSeen = append(firstSeen, k)
		}
		groups[k] = append(groups[k], i)
	}

	// 每个同参组一次生产（并发；Synthesizer 并发契约见 tts 包）.
	type groupOutcome struct {
		asset *AudioAsset
		err   error
	}
	slot := make([]groupOutcome, len(firstSeen))
	var wg sync.WaitGroup
	for gi, k := range firstSeen {
		wg.Add(1)
		go func(gi int, k batchKey) {
			defer wg.Done()
			asset, err := p.Produce(ctx, ProduceRequest{Text: k.text, VoiceProfile: k.voice, GradeBand: k.band})
			slot[gi] = groupOutcome{asset: asset, err: err}
		}(gi, k)
	}
	wg.Wait()

	// 展开回任务槽（字节独立拷贝），再按任务序产出 results/failures.
	perJob := make([]*AudioAsset, len(jobs))
	jobErr := make([]error, len(jobs))
	for gi, k := range firstSeen {
		oc := slot[gi]
		for _, ji := range groups[k] {
			if oc.err != nil {
				jobErr[ji] = oc.err
				continue
			}
			perJob[ji] = cloneAudioAsset(oc.asset)
		}
	}
	out.Results = make([]*AudioAsset, 0, len(jobs))
	for i := range jobs {
		if err := jobErr[i]; err != nil {
			out.Failures = append(out.Failures, BatchFailure{JobIndex: i, Text: jobs[i].Text, Error: err.Error()})
			continue
		}
		out.Results = append(out.Results, perJob[i])
	}
	out.Succeeded = len(out.Results)
	out.Failed = len(out.Failures)
	if out.Failed == 0 {
		out.Status = BatchCompleted
	} else {
		out.Status = BatchPartial
	}
	return out
}

// cloneAudioAsset 深拷贝素材（bytes/metadata 独立——批次组内各任务无别名）.
func cloneAudioAsset(a *AudioAsset) *AudioAsset {
	md := make(map[string]string, len(a.TTSMetadata))
	for k, v := range a.TTSMetadata {
		md[k] = v
	}
	return &AudioAsset{
		AudioID:      a.AudioID,
		URL:          a.URL,
		ContentHash:  a.ContentHash,
		DurationMS:   a.DurationMS,
		TTSMetadata:  md,
		Text:         a.Text,
		VoiceProfile: a.VoiceProfile,
		GradeBand:    a.GradeBand,
		Audio:        cloneAudioBytes(a.Audio),
	}
}
