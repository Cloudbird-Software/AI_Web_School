package audio

import (
	"context"
	"errors"
	"strconv"
	"strings"
	"sync"
	"testing"

	"github.com/Cloudbird-Software/AI_Web_School/core/ai"
	"github.com/Cloudbird-Software/AI_Web_School/core/ai/tts"
)

// producer_test.go：音频产线验收。
//   - 单题生产：id 透传（总线 id == 产线 id）、url/content_hash/duration/
//     metadata 全量包装；
//   - D3+D7：PII 不进 id（剥离后文本寻址）、writer 可注入、写失败传导；
//   - 批次：同参合流去重（引擎调用数=同参组数）、任务序保持、失败隔离、
//     字节独立无别名；全程 fake 引擎 hermetic，-race 绿。

// countingEngine 记录合成调用（与冻结实现 MockTTSEngine 同构的确定性占位
// 字节），支持按文本注入失败.
type countingEngine struct {
	mu       sync.Mutex
	texts    []string
	voiceIDs []string
	wpms     []int
	failOn   map[string]error
}

func (e *countingEngine) Synthesize(_ context.Context, text, voiceID string, wpm int) ([]byte, error) {
	e.mu.Lock()
	defer e.mu.Unlock()
	if e.failOn != nil {
		if err, ok := e.failOn[text]; ok {
			return nil, err
		}
	}
	e.texts = append(e.texts, text)
	e.voiceIDs = append(e.voiceIDs, voiceID)
	e.wpms = append(e.wpms, wpm)
	return []byte("audio:" + voiceID + ":" + strconv.Itoa(wpm) + ":" + text), nil
}

func (e *countingEngine) count() int {
	e.mu.Lock()
	defer e.mu.Unlock()
	return len(e.texts)
}

func (e *countingEngine) lastText() string {
	e.mu.Lock()
	defer e.mu.Unlock()
	return e.texts[len(e.texts)-1]
}

// newTestSynth 构造被测 TTS 服务面（fake 引擎 + 总线 + 正则剥离器）.
func newTestSynth(t *testing.T, eng tts.Engine) *tts.Synthesizer {
	t.Helper()
	led := ai.NewMemoryLedger()
	b, err := ai.NewBus(ai.RegexRedactor{}, led)
	if err != nil {
		t.Fatalf("NewBus: %v", err)
	}
	syn, err := tts.NewSynthesizer(b, ai.RegexRedactor{}, eng, tts.Config{})
	if err != nil {
		t.Fatalf("NewSynthesizer: %v", err)
	}
	return syn
}

// newTestProducer 构造被测产线（mock writer 缺省面），返回产线与引擎供断言.
func newTestProducer(t *testing.T, eng *countingEngine) (*Producer, *countingEngine) {
	t.Helper()
	p, err := NewProducer(newTestSynth(t, eng), nil)
	if err != nil {
		t.Fatalf("NewProducer: %v", err)
	}
	return p, eng
}

func TestProduceSingleAsset(t *testing.T) {
	eng := &countingEngine{}
	p, _ := newTestProducer(t, eng)

	asset, err := p.Produce(context.Background(), ProduceRequest{Text: "苹果 banana", GradeBand: tts.BandM})
	if err != nil {
		t.Fatalf("Produce: %v", err)
	}

	if asset.AudioID == "" || len(asset.AudioID) != 64 {
		t.Fatalf("总线 id 必须为完整 64 hex 摘要：got=%q", asset.AudioID)
	}
	wantURL := MockBaseURL + "/" + MockBucket + "/" + asset.AudioID + ".mp3"
	if asset.URL != wantURL {
		t.Fatalf("mock URL 分歧：got=%s want=%s", asset.URL, wantURL)
	}
	if asset.ContentHash != ComputeContentHash(asset.Audio) {
		t.Fatalf("content_hash 与音频字节不一致：%s", asset.ContentHash)
	}
	if len(asset.Audio) == 0 {
		t.Fatal("素材必须携带引擎产出字节")
	}
	if asset.DurationMS <= 0 {
		t.Fatalf("duration_ms 必须为正（估算面）：got=%d", asset.DurationMS)
	}
	// 学段配置注入面（A5）：M=140wpm / 默认音色 female_standard / mock 引擎.
	if asset.GradeBand != tts.BandM || asset.VoiceProfile != "female_standard" {
		t.Fatalf("学段解析分歧：band=%s voice=%s", asset.GradeBand, asset.VoiceProfile)
	}
	md := asset.TTSMetadata
	if md["voice"] != "female_standard" || md["wpm"] != "140" || md["engine"] != "mock" || md["duration_ms"] == "" {
		t.Fatalf("tts_metadata 分歧：%v", md)
	}
	if asset.Text != "苹果 banana" {
		t.Fatalf("溯源 text 分歧：%q", asset.Text)
	}
	if eng.count() != 1 {
		t.Fatalf("单题生产必须恰好一次合成：got=%d", eng.count())
	}
}

func TestProducePIINotInID(t *testing.T) {
	// D3+D7：id 是「说出口的内容」的函数——不同 PII、相同口播内容必得同 id.
	eng := &countingEngine{}
	p, _ := newTestProducer(t, eng)

	a1, err := p.Produce(context.Background(), ProduceRequest{Text: "家长电话13812345678请收听", GradeBand: tts.BandM})
	if err != nil {
		t.Fatalf("Produce: %v", err)
	}
	a2, err := p.Produce(context.Background(), ProduceRequest{Text: "家长电话13987654321请收听", GradeBand: tts.BandM})
	if err != nil {
		t.Fatalf("Produce: %v", err)
	}
	if a1.AudioID != a2.AudioID {
		t.Fatalf("PII 差异不得产生第二份音频（D3）：%s != %s", a1.AudioID, a2.AudioID)
	}
	if strings.Contains(a1.AudioID, "13812345678") {
		t.Fatal("id 含 PII")
	}
}

func TestProduceCustomWriterAndWriteFailure(t *testing.T) {
	eng := &countingEngine{}

	// 注入 writer：URL 由 writer 决定（副作用边界可注入）.
	custom := mapWriterFunc(func(audioID string, _ []byte) (string, error) {
		return "memfs://" + audioID + ".wav", nil
	})
	p2, err := NewProducer(newTestSynth(t, eng), custom)
	if err != nil {
		t.Fatalf("NewProducer: %v", err)
	}
	asset, err := p2.Produce(context.Background(), ProduceRequest{Text: "苹果", GradeBand: tts.BandL})
	if err != nil {
		t.Fatalf("Produce: %v", err)
	}
	if asset.URL != "memfs://"+asset.AudioID+".wav" {
		t.Fatalf("注入 writer URL 分歧：%s", asset.URL)
	}

	// 写失败 fail-loud（含 audio_id 上下文）.
	boom := errors.New("minio down")
	p3, err := NewProducer(newTestSynth(t, eng), mapWriterFunc(func(string, []byte) (string, error) { return "", boom }))
	if err != nil {
		t.Fatalf("NewProducer: %v", err)
	}
	if _, err := p3.Produce(context.Background(), ProduceRequest{Text: "苹果", GradeBand: tts.BandL}); !errors.Is(err, boom) {
		t.Fatalf("写失败必须传导：got=%v", err)
	}
}

// mapWriterFunc 把函数适配成 AudioStorageWriter.
type mapWriterFunc func(audioID string, audio []byte) (string, error)

func (f mapWriterFunc) Write(audioID string, audio []byte) (string, error) { return f(audioID, audio) }

func TestProduceValidation(t *testing.T) {
	if _, err := NewProducer(nil, nil); !errors.Is(err, ErrNilSynthesizer) {
		t.Fatalf("nil synthesizer 必须报 ErrNilSynthesizer：got=%v", err)
	}
	eng := &countingEngine{}
	p, _ := newTestProducer(t, eng)
	if _, err := p.Produce(context.Background(), ProduceRequest{Text: "", GradeBand: tts.BandM}); !errors.Is(err, ErrEmptyProduceText) {
		t.Fatalf("空文本必须报 ErrEmptyProduceText：got=%v", err)
	}
}

func TestProduceBatchDedupOrderAndIsolation(t *testing.T) {
	eng := &countingEngine{}
	p, _ := newTestProducer(t, eng)

	// 5 任务 3 同参组：引擎调用数必须为 3（确定性去重，与任务数无关）.
	jobs := []ProduceJob{
		{Text: "第一题", GradeBand: tts.BandM},
		{Text: "第二题", GradeBand: tts.BandM},
		{Text: "第一题", GradeBand: tts.BandM}, // 与 0 同参
		{Text: "第三题", GradeBand: tts.BandH},
		{Text: "第二题", GradeBand: tts.BandM}, // 与 1 同参
	}
	got := p.ProduceBatch(context.Background(), jobs)

	if got.Status != BatchCompleted {
		t.Fatalf("status=%s，期望 completed", got.Status)
	}
	if got.Total != 5 || got.Succeeded != 5 || got.Failed != 0 || len(got.Failures) != 0 {
		t.Fatalf("计数分歧：%+v", got)
	}
	if eng.count() != 3 {
		t.Fatalf("同参合流去重失效：引擎调用 %d 次，期望 3", eng.count())
	}
	if len(got.Results) != 5 {
		t.Fatalf("结果数 %d，期望 5", len(got.Results))
	}
	// 任务序保持 + 同参任务同 id.
	if got.Results[0].AudioID != got.Results[2].AudioID {
		t.Fatalf("任务 0/2 同参必须同 id：%s != %s", got.Results[0].AudioID, got.Results[2].AudioID)
	}
	if got.Results[1].AudioID != got.Results[4].AudioID {
		t.Fatalf("任务 1/4 同参必须同 id")
	}
	if got.Results[3].AudioID == got.Results[0].AudioID {
		t.Fatal("异参任务不得同 id")
	}
	for i, want := range []string{"第一题", "第二题", "第一题", "第三题", "第二题"} {
		if got.Results[i].Text != want {
			t.Fatalf("任务序错乱：results[%d].Text=%q，期望 %q", i, got.Results[i].Text, want)
		}
	}
	// 字节独立：改写一份不得污染同参兄弟任务（无别名共享）.
	got.Results[0].Audio[0] = 'X'
	got.Results[0].TTSMetadata["voice"] = "hacked"
	if got.Results[2].Audio[0] == 'X' {
		t.Fatal("同参任务的音频字节存在别名共享")
	}
	if got.Results[2].TTSMetadata["voice"] == "hacked" {
		t.Fatal("同参任务的 metadata 存在别名共享")
	}
}

func TestProduceBatchFailureIsolation(t *testing.T) {
	eng := &countingEngine{failOn: map[string]error{"第二题": errors.New("engine exploded")}}
	p, _ := newTestProducer(t, eng)

	got := p.ProduceBatch(context.Background(), []ProduceJob{
		{Text: "第一题", GradeBand: tts.BandM},
		{Text: "第二题", GradeBand: tts.BandM},
		{Text: "第三题", GradeBand: tts.BandM},
	})
	if got.Status != BatchPartial {
		t.Fatalf("status=%s，期望 partial", got.Status)
	}
	if got.Succeeded != 2 || got.Failed != 1 || len(got.Results) != 2 || len(got.Failures) != 1 {
		t.Fatalf("失败隔离分歧：%+v", got)
	}
	f := got.Failures[0]
	if f.JobIndex != 1 || f.Text != "第二题" || !strings.Contains(f.Error, "engine exploded") {
		t.Fatalf("失败记录分歧：%+v", f)
	}
	// 成功结果仍按任务序（0、2）.
	if got.Results[0].Text != "第一题" || got.Results[1].Text != "第三题" {
		t.Fatalf("成功结果序错乱：%q %q", got.Results[0].Text, got.Results[1].Text)
	}
}

func TestProduceBatchEmpty(t *testing.T) {
	eng := &countingEngine{}
	p, _ := newTestProducer(t, eng)
	got := p.ProduceBatch(context.Background(), nil)
	if got.Status != BatchCompleted || got.Total != 0 || len(got.Results) != 0 {
		t.Fatalf("空批次分歧：%+v", got)
	}
	if eng.count() != 0 {
		t.Fatal("空批次不得触发合成")
	}
}
