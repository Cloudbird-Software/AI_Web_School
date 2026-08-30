package audio

import (
	"context"
	"errors"
	"testing"
	"time"

	"github.com/Cloudbird-Software/AI_Web_School/core/ai/tts"
	"github.com/Cloudbird-Software/AI_Web_School/core/gate/validators"
)

// listening_test.go：听力端到端数据面验收。
//   - 题组→音频序列→时间轴（有序、连续、覆盖）；
//   - 过门阻断（D2：fail → ListeningGateError，不静默放行）；
//   - 渲染产物：online 播放器 URL / static 签名 URL（可验签）；
//   - digest 可复现（同输入同指纹）；批次去重贯通流水线。

// passGate 双验证器全 pass（冻结实现 AudioAgeCheck+AudioQuality 的 pass 面）.
type passGate struct{}

func (passGate) Validate(_ context.Context, a *AudioAsset) (GateValidationResult, error) {
	return GateValidationResult{
		AudioID:      a.AudioID,
		AgeCheck:     GateCheck{ValidatorID: "audio_age_check", Verdict: validators.VerdictPass},
		QualityCheck: GateCheck{ValidatorID: "audio_quality", Verdict: validators.VerdictPass},
		Passed:       true,
	}, nil
}

// failGate 语速适龄 fail（D2 阻断面）.
type failGate struct{}

func (failGate) Validate(_ context.Context, a *AudioAsset) (GateValidationResult, error) {
	return GateValidationResult{
		AudioID:      a.AudioID,
		AgeCheck:     GateCheck{ValidatorID: "audio_age_check", Verdict: validators.VerdictFail, Evidence: map[string]string{"wpm": "160"}},
		QualityCheck: GateCheck{ValidatorID: "audio_quality", Verdict: validators.VerdictPass},
		Passed:       false,
	}, nil
}

// errGate 校验器故障面.
type errGate struct{}

var errGateDown = errors.New("gate db down")

func (errGate) Validate(_ context.Context, _ *AudioAsset) (GateValidationResult, error) {
	return GateValidationResult{}, errGateDown
}

func pipelineSpecs() []ItemSpec {
	return []ItemSpec{
		{ItemVersionID: "iv-1", Text: "第一题听力材料"},
		{ItemVersionID: "iv-2", Text: "第二题听力材料较长一些"},
		{ItemVersionID: "iv-3", Text: "第三题"},
	}
}

func TestListeningPipelineOnlineHappyPath(t *testing.T) {
	eng := &countingEngine{}
	p, _ := newTestProducer(t, eng)

	res, err := RunListeningPipeline(context.Background(), p, passGate{}, pipelineSpecs(), PipelineOptions{
		GradeBand:  tts.BandM,
		RenderMode: RenderOnline,
		PaperID:    "paper-e2e",
	})
	if err != nil {
		t.Fatalf("RunListeningPipeline: %v", err)
	}

	// 1. 音频素材与过门结果一一对应.
	if len(res.AudioAssets) != 3 || len(res.GateResults) != 3 || len(res.PaperItems) != 3 {
		t.Fatalf("数量分歧：assets=%d gates=%d items=%d", len(res.AudioAssets), len(res.GateResults), len(res.PaperItems))
	}
	for i, it := range res.PaperItems {
		if it.ItemVersionID != pipelineSpecs()[i].ItemVersionID {
			t.Fatalf("题序错乱：items[%d]=%s", i, it.ItemVersionID)
		}
		if it.Audio.AudioID != res.AudioAssets[i].AudioID || it.Gate.AudioID != res.AudioAssets[i].AudioID {
			t.Fatalf("题/音频/过门绑定错位：items[%d]", i)
		}
		if !it.Gate.Passed {
			t.Fatalf("items[%d] 过门未通过却入卷", i)
		}
	}

	// 2. 时间轴：有序、连续、覆盖全部时长.
	cursor := 0
	for i, e := range res.Timeline {
		if e.Seq != i || e.StartMS != cursor || e.EndMS != cursor+res.AudioAssets[i].DurationMS {
			t.Fatalf("时间轴分歧：entry[%d]=%+v cursor=%d", i, e, cursor)
		}
		cursor = e.EndMS
	}
	if res.Timeline[len(res.Timeline)-1].EndMS == 0 {
		t.Fatal("时间轴覆盖为空")
	}

	// 3. 渲染产物：在线卷=播放器 URL（首次播放 play_count=1）.
	for i, a := range res.RenderArtifacts {
		if a.ArtifactType != ArtifactPlayerURL {
			t.Fatalf("产物类型分歧：%s", a.ArtifactType)
		}
		if a.PlayerURL != res.AudioAssets[i].URL || a.PlayCount != 1 {
			t.Fatalf("播放产物分歧：%+v", a)
		}
		if a.AudioID != res.AudioAssets[i].AudioID {
			t.Fatalf("产物音频绑定错位：%+v", a)
		}
	}

	// 4. digest 可复现：同输入重跑 → 同指纹.
	res2, err := RunListeningPipeline(context.Background(), p, passGate{}, pipelineSpecs(), PipelineOptions{
		GradeBand:  tts.BandM,
		RenderMode: RenderOnline,
		PaperID:    "paper-e2e",
	})
	if err != nil {
		t.Fatalf("重跑: %v", err)
	}
	if res2.PipelineDigest == "" || res2.PipelineDigest != res.PipelineDigest {
		t.Fatalf("digest 不可复现：%s != %s", res2.PipelineDigest, res.PipelineDigest)
	}
	if res.RenderMode != RenderOnline {
		t.Fatalf("render_mode 分歧：%s", res.RenderMode)
	}
}

func TestListeningPipelineDedupThroughBatch(t *testing.T) {
	// 两题同文本（复用同一音频素材）：批次去重贯通 → 引擎仅 1 次合成.
	eng := &countingEngine{}
	p, _ := newTestProducer(t, eng)
	specs := []ItemSpec{
		{ItemVersionID: "iv-a", Text: "同一段听力材料"},
		{ItemVersionID: "iv-b", Text: "同一段听力材料"},
	}
	res, err := RunListeningPipeline(context.Background(), p, passGate{}, specs, PipelineOptions{
		GradeBand: tts.BandM, RenderMode: RenderOnline, PaperID: "p",
	})
	if err != nil {
		t.Fatalf("RunListeningPipeline: %v", err)
	}
	if eng.count() != 1 {
		t.Fatalf("同参题目必须共享一次合成：引擎调用 %d 次", eng.count())
	}
	if res.AudioAssets[0].AudioID != res.AudioAssets[1].AudioID {
		t.Fatal("同参题目必须同音频 id（D3）")
	}
}

func TestListeningPipelineGateBlocks(t *testing.T) {
	eng := &countingEngine{}
	p, _ := newTestProducer(t, eng)

	_, err := RunListeningPipeline(context.Background(), p, failGate{}, pipelineSpecs(), PipelineOptions{
		GradeBand: tts.BandM, RenderMode: RenderOnline, PaperID: "p",
	})
	if err == nil {
		t.Fatal("未过门必须阻断组卷（D2）")
	}
	if !errors.Is(err, ErrGateBlocked) {
		t.Fatalf("必须可判别 ErrGateBlocked：got=%v", err)
	}
	var gerr *ListeningGateError
	if !errors.As(err, &gerr) {
		t.Fatalf("必须可取结构化明细：got=%v", err)
	}
	if gerr.ValidatorID != "audio_age_check" || gerr.Verdict != validators.VerdictFail {
		t.Fatalf("过门错误明细分歧：%+v", gerr)
	}
	if gerr.Evidence["wpm"] != "160" {
		t.Fatalf("证据分歧：%v", gerr.Evidence)
	}
}

func TestListeningPipelineGateErrorConducts(t *testing.T) {
	eng := &countingEngine{}
	p, _ := newTestProducer(t, eng)
	_, err := RunListeningPipeline(context.Background(), p, errGate{}, pipelineSpecs(), PipelineOptions{
		GradeBand: tts.BandM, PaperID: "p",
	})
	if err == nil || !errors.Is(err, errGateDown) {
		t.Fatalf("校验器故障必须传导：got=%v", err)
	}
}

func TestListeningPipelineStaticArtifacts(t *testing.T) {
	eng := &countingEngine{}
	p, _ := newTestProducer(t, eng)

	res, err := RunListeningPipeline(context.Background(), p, passGate{}, pipelineSpecs()[:1], PipelineOptions{
		GradeBand: tts.BandM, RenderMode: RenderStatic, QRSecret: goldenQRSigSeed, PaperID: "paper-e2e",
	})
	if err != nil {
		t.Fatalf("RunListeningPipeline: %v", err)
	}
	a := res.RenderArtifacts[0]
	if a.ArtifactType != ArtifactQRCode {
		t.Fatalf("静态卷产物类型分歧：%s", a.ArtifactType)
	}
	if a.SignedURL == "" {
		t.Fatal("静态卷必须承载签名 URL（QR 载荷面）")
	}
	// 签发用真实时钟（Now 零值），24h 内当下必可验签.
	if !VerifyQRURL(a.SignedURL, goldenQRSigSeed, time.Now()) {
		t.Fatalf("签名 URL 必须当下可验签：%s", a.SignedURL)
	}
	if a.QRSVG != "" {
		t.Fatal("骨架期 QRSVG 必须留白（不静默降级）")
	}
}

func TestListeningPipelineValidation(t *testing.T) {
	eng := &countingEngine{}
	p, _ := newTestProducer(t, eng)
	ctx := context.Background()
	opts := PipelineOptions{GradeBand: tts.BandM, PaperID: "p"}

	if _, err := RunListeningPipeline(ctx, nil, passGate{}, pipelineSpecs(), opts); !errors.Is(err, ErrNilProducer) {
		t.Fatalf("nil producer 必须报 ErrNilProducer：got=%v", err)
	}
	if _, err := RunListeningPipeline(ctx, p, nil, pipelineSpecs(), opts); !errors.Is(err, ErrNilGate) {
		t.Fatalf("nil gate 必须报 ErrNilGate：got=%v", err)
	}
	if _, err := RunListeningPipeline(ctx, p, passGate{}, nil, opts); !errors.Is(err, ErrEmptyItemSpecs) {
		t.Fatalf("空 specs 必须报 ErrEmptyItemSpecs：got=%v", err)
	}
	if _, err := RunListeningPipeline(ctx, p, passGate{}, pipelineSpecs(), PipelineOptions{GradeBand: tts.BandM}); !errors.Is(err, ErrPipelineParam) {
		t.Fatalf("空 paper_id 必须报 ErrPipelineParam：got=%v", err)
	}
	if _, err := RunListeningPipeline(ctx, p, passGate{}, pipelineSpecs(), PipelineOptions{GradeBand: tts.BandM, PaperID: "p", RenderMode: "hologram"}); !errors.Is(err, ErrPipelineParam) {
		t.Fatalf("非法 render_mode 必须报 ErrPipelineParam：got=%v", err)
	}
	bad := []ItemSpec{{ItemVersionID: "", Text: "x"}}
	if _, err := RunListeningPipeline(ctx, p, passGate{}, bad, opts); !errors.Is(err, ErrInvalidSpec) {
		t.Fatalf("缺 item_version_id 必须报 ErrInvalidSpec：got=%v", err)
	}
	bad2 := []ItemSpec{{ItemVersionID: "iv", Text: ""}}
	if _, err := RunListeningPipeline(ctx, p, passGate{}, bad2, opts); !errors.Is(err, ErrInvalidSpec) {
		t.Fatalf("空 text 必须报 ErrInvalidSpec：got=%v", err)
	}
	// 生产失败整体中止（流水线是整体交付）.
	badEng := &countingEngine{failOn: map[string]error{"第一题听力材料": errors.New("boom")}}
	pBad, _ := newTestProducer(t, badEng)
	if _, err := RunListeningPipeline(ctx, pBad, passGate{}, pipelineSpecs(), PipelineOptions{GradeBand: tts.BandM, PaperID: "p", RenderMode: RenderOnline}); err == nil || errors.Is(err, ErrGateBlocked) {
		t.Fatalf("生产失败必须中止且非过门错误：got=%v", err)
	}
}

func TestBuildTimelineEmpty(t *testing.T) {
	if got := BuildTimeline(nil); len(got) != 0 {
		t.Fatalf("空输入必须得空时间轴：got=%v", got)
	}
}
