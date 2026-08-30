package audio

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"strings"

	"github.com/Cloudbird-Software/AI_Web_School/core/ai/tts"
	"github.com/Cloudbird-Software/AI_Web_School/core/gate/validators"
)

// listening.go 承载听力端到端数据面（冻结实现
// src/core/audio/listening_e2e.py T-W4-026 的 Go 重锚定，架构 v2 §4.6，
// E2E-4 承载卡）。
//
// 完整链路（数据面收敛）：
//  1. TTS 产线合成音频（Producer.ProduceBatch，D3 内容寻址）；
//  2. 音频过门（AudioGate 端口：语速适龄 + 发音质量）——未过门阻断组卷（D2：
//     未过门产物不得入库/组卷，fail-loud 不静默放行）；
//  3. 绑定音频到听力题 → 时间轴（题组→音频序列→累计起止偏移）；
//  4. 渲染产物：静态卷=签名 URL（QR 贴码载荷），在线卷=播放器 URL（限次播放）；
//  5. 端到端 digest（可复现指纹）。
//
// 与冻结实现的显式偏离（如实声明）：
//   - 组卷 overlay 编排不在本面：core/assembly 已有 ApplyListeningOverlay
//     独立移植（占比 30–40%/置卷首/testlet），本数据面不重复编排；冻结实现
//     硬编码 kp_codes=["eng.listen"] 属学科语义（X6 违例面），Go 数据面不感知
//     学科——「英语」由调用方传入 texts 决定；
//   - 音频过门验证器以 AudioGate 端口注入（冻结实现内联注册 platform 验证器
//     的装配面收敛到端口；验证器实现属 gate 域装配，不进核心域）；
//   - 静态卷产物承载签名 URL，QR SVG 位图是 render.GenerateQRSVG 骨架面
//     （零新依赖约束）——贴码位留白不静默降级，接线后经 GenerateQR 补齐；
//   - pipeline_digest 仅承担 Go 侧可复现指纹（passed 以 "true"/"false" 格式；
//     冻结实现为 "True"/"False"）——Go 侧音频 id 公式已显式偏离冻结实现，
//     跨语言字节互验不适用于端到端摘要。
//
// 宪法 A5/X6：不 import 学科包/学段包。宪法 D7：texts 应已剥离 PII（总线
// fail-closed 兜底），本包不感知 PII 语义。

// 哨兵错误.
var (
	// ErrGateBlocked 表示音频未过门阻断组卷（errors.As 可取
	// *ListeningGateError 明细；D2）.
	ErrGateBlocked = errors.New("audio: 音频未过门，阻断组卷")
	// ErrNilProducer 表示未注入音频产线.
	ErrNilProducer = errors.New("audio: producer 不可为 nil")
	// ErrNilGate 表示未注入音频过门端口.
	ErrNilGate = errors.New("audio: audio gate 不可为 nil")
	// ErrEmptyItemSpecs 表示听力题规格为空.
	ErrEmptyItemSpecs = errors.New("audio: item_specs 不能为空")
	// ErrInvalidSpec 表示题规格字段非法.
	ErrInvalidSpec = errors.New("audio: item_spec 非法")
	// ErrPipelineParam 表示流水线选项字段非法.
	ErrPipelineParam = errors.New("audio: pipeline 选项非法")
)

// 渲染模式（冻结实现 render_mode 值域）与产物类型.
const (
	RenderStatic RenderMode = "static" // 静态卷：QR 贴码（签名 URL 载荷）
	RenderOnline RenderMode = "online" // 在线卷：播放器 URL（限次播放）
)

// RenderMode 是渲染模式（static/online）.
type RenderMode string

// 渲染产物类型值.
const (
	ArtifactQRCode    = "qr_code"
	ArtifactPlayerURL = "player_url"
)

// ItemSpec 是听力题规格（pipeline 输入）.
type ItemSpec struct {
	// ItemVersionID 题版本 id（D3 内容寻址）.
	ItemVersionID string
	// Text 待合成文本（应已剥离 PII，D7）.
	Text string
	// VoiceProfile 音色名（空 → 学段默认）.
	VoiceProfile string
}

// validate 构造期校验（冻结实现 pydantic min_length=1 面）.
func (s ItemSpec) validate() error {
	if s.ItemVersionID == "" {
		return fmt.Errorf("%w: item_version_id 不能为空", ErrInvalidSpec)
	}
	if s.Text == "" {
		return fmt.Errorf("%w: text 不能为空（item_version_id=%s）", ErrInvalidSpec, s.ItemVersionID)
	}
	return nil
}

// GateCheck 是单个验证器的过门结果（冻结实现 ValidatorResult 的数据面收敛）.
type GateCheck struct {
	// ValidatorID 验证器标识（如 audio_age_check / audio_quality）.
	ValidatorID string
	// Verdict 判定三值（pass/fail/review，与 gate_run.verdict 词表一致）.
	Verdict validators.Verdict
	// Evidence 证据（判定依据的结构化载体）.
	Evidence map[string]string
}

// GateValidationResult 是单个音频过门结果.
type GateValidationResult struct {
	AudioID      string
	AgeCheck     GateCheck
	QualityCheck GateCheck
	Passed       bool
}

// AudioGate 是听力端到端的音频过门端口（D2）：实现方注入语速适龄
// （AudioAgeCheck）与发音质量（AudioQuality）两个 blocking 验证器的编排——
// 两者皆 pass 才 Passed=true.
type AudioGate interface {
	Validate(ctx context.Context, asset *AudioAsset) (GateValidationResult, error)
}

// ListeningGateError 是音频未过门的结构化错误（D2 阻断）.
type ListeningGateError struct {
	AudioID     string
	ValidatorID string
	Verdict     validators.Verdict
	Evidence    map[string]string
}

// Error 实现 error.
func (e *ListeningGateError) Error() string {
	return fmt.Sprintf("音频 %q 未过门：%s verdict=%s", e.AudioID, e.ValidatorID, e.Verdict)
}

// Is 使 errors.Is(err, ErrGateBlocked) 成立.
func (e *ListeningGateError) Is(target error) bool { return target == ErrGateBlocked }

// ListeningPaperItem 是听力卷单题（含音频绑定 + 过门证据）.
type ListeningPaperItem struct {
	ItemVersionID string
	Audio         *AudioAsset
	Gate          GateValidationResult
}

// TimelineEntry 是听力时间轴上的单个音频区间（毫秒）.
type TimelineEntry struct {
	// Seq 卷面题序（0-based）.
	Seq int
	// ItemVersionID / AudioID 题与音频的绑定标识.
	ItemVersionID string
	AudioID       string
	// StartMS / EndMS 累计起止偏移（前一音频结束即后一音频开始）.
	StartMS int
	EndMS   int
}

// BuildTimeline 构造听力时间轴（题组→音频序列→时间轴）：按卷面题序累计
// 偏移，时长取 TTS 估算（AudioAsset.DurationMS，冻结实现 duration_ms 同源）。
// 纯函数：输入为空返回空时间轴.
func BuildTimeline(items []ListeningPaperItem) []TimelineEntry {
	out := make([]TimelineEntry, 0, len(items))
	cursor := 0
	for i, it := range items {
		end := cursor + it.Audio.DurationMS
		out = append(out, TimelineEntry{
			Seq:           i,
			ItemVersionID: it.ItemVersionID,
			AudioID:       it.Audio.AudioID,
			StartMS:       cursor,
			EndMS:         end,
		})
		cursor = end
	}
	return out
}

// RenderArtifact 是渲染产物：QR 码（静态卷）或播放器 URL（在线卷）.
type RenderArtifact struct {
	ItemVersionID string
	AudioID       string
	// ArtifactType 产物类型（ArtifactQRCode / ArtifactPlayerURL）.
	ArtifactType string
	// QRSVG QR 码 SVG（骨架期静态卷为空——贴码位留白，见包注释；不静默降级）.
	QRSVG string
	// SignedURL 静态卷签名 URL（QR 码载荷面，全量实现）.
	SignedURL string
	// PlayerURL / PlayCount 在线卷播放器 URL 与首次播放累计次数.
	PlayerURL string
	PlayCount int
}

// ListeningPipelineResult 是听力端到端流水线结果（验收 #1 的数据面收敛）.
type ListeningPipelineResult struct {
	// AudioAssets TTS 产出的全部音频素材.
	AudioAssets []*AudioAsset
	// GateResults 每个音频的过门验证结果.
	GateResults []GateValidationResult
	// PaperItems 绑定音频的听力题（已过门）.
	PaperItems []ListeningPaperItem
	// Timeline 听力时间轴（题组→音频序列→累计起止偏移）.
	Timeline []TimelineEntry
	// RenderArtifacts 渲染产物（QR/player）.
	RenderArtifacts []RenderArtifact
	// RenderMode 渲染模式（static/online）.
	RenderMode RenderMode
	// PipelineDigest 端到端可复现指纹（Go 侧语义，见文件头偏离声明）.
	PipelineDigest string
}

// PipelineOptions 是流水线选项（冻结实现 run_listening_pipeline 关键字参数
// 的数据面收敛；overlay 相关参数不在本面）.
type PipelineOptions struct {
	// GradeBand 学段 L/M/H（决定语速与默认音色）.
	GradeBand tts.GradeBand
	// RenderMode 渲染模式（空 → RenderStatic，冻结默认）.
	RenderMode RenderMode
	// QRSecret QR 签名密钥（静态卷必填）.
	QRSecret string
	// PaperID 卷 id（QR 签名绑定 + online 播放 session；必填）.
	PaperID string
	// PlayStore 在线卷播放计数存储（nil → 进程内新建，冻结实现同语义）.
	PlayStore PlayCountStore
}

// RunListeningPipeline 运行听力端到端数据面（验收 #1/#2/#3）：
// TTS 合成 → 音频过门（未过门阻断，D2）→ 绑定题目 → 时间轴 → 渲染产物 →
// digest。任一音频生产失败即整体失败（流水线是整体交付，冻结实现异常中止
// 同语义）；任一音频未过门即阻断（ListeningGateError）.
func RunListeningPipeline(ctx context.Context, prod *Producer, gate AudioGate, specs []ItemSpec, opts PipelineOptions) (*ListeningPipelineResult, error) {
	if prod == nil {
		return nil, ErrNilProducer
	}
	if gate == nil {
		return nil, ErrNilGate
	}
	if len(specs) == 0 {
		return nil, ErrEmptyItemSpecs
	}
	if opts.PaperID == "" {
		return nil, fmt.Errorf("%w: paper_id 不能为空", ErrPipelineParam)
	}
	mode := opts.RenderMode
	if mode == "" {
		mode = RenderStatic
	}
	switch mode {
	case RenderStatic, RenderOnline:
	default:
		return nil, fmt.Errorf("%w: render_mode %q 越域（仅 static/online）", ErrPipelineParam, mode)
	}
	for i, s := range specs {
		if err := s.validate(); err != nil {
			return nil, fmt.Errorf("%w: specs[%d]: %w", ErrPipelineParam, i, err)
		}
	}

	// ── 1. TTS 合成（批次去重：同参任务合流，产线内容寻址）──
	jobs := make([]ProduceJob, len(specs))
	for i, s := range specs {
		jobs[i] = ProduceJob{Text: s.Text, VoiceProfile: s.VoiceProfile, GradeBand: opts.GradeBand}
	}
	batch := prod.ProduceBatch(ctx, jobs)
	if len(batch.Failures) > 0 {
		f := batch.Failures[0]
		return nil, fmt.Errorf("audio: 听力音频生产失败（%d/%d，首例 job_index=%d）: %s",
			batch.Failed, batch.Total, f.JobIndex, f.Error)
	}
	assets := batch.Results // 无失败时与 specs 一一对应且同序

	// ── 2. 音频过门（D2：未过门阻断，不静默放行）──
	gateResults := make([]GateValidationResult, 0, len(assets))
	for _, asset := range assets {
		g, err := gate.Validate(ctx, asset)
		if err != nil {
			return nil, fmt.Errorf("audio: 音频过门校验失败（audio_id=%s）: %w", asset.AudioID, err)
		}
		if !g.Passed {
			failed := g.AgeCheck
			if failed.Verdict == validators.VerdictPass {
				failed = g.QualityCheck
			}
			return nil, &ListeningGateError{
				AudioID:     asset.AudioID,
				ValidatorID: failed.ValidatorID,
				Verdict:     failed.Verdict,
				Evidence:    failed.Evidence,
			}
		}
		gateResults = append(gateResults, g)
	}

	// ── 3. 绑定音频到题目（specs/assets/gates 同序 zip）──
	items := make([]ListeningPaperItem, len(specs))
	for i := range specs {
		items[i] = ListeningPaperItem{
			ItemVersionID: specs[i].ItemVersionID,
			Audio:         assets[i],
			Gate:          gateResults[i],
		}
	}

	// ── 4. 时间轴（题组→音频序列→累计起止偏移）──
	timeline := BuildTimeline(items)

	// ── 5. 渲染产物 ──
	artifacts, err := buildRenderArtifacts(items, opts, mode)
	if err != nil {
		return nil, err
	}

	// ── 6. 端到端 digest ──
	var b strings.Builder
	for i, it := range items {
		if i > 0 {
			b.WriteByte('|')
		}
		fmt.Fprintf(&b, "%s:%s:%t", it.ItemVersionID, it.Audio.AudioID, it.Gate.Passed)
	}
	sum := sha256.Sum256([]byte(b.String()))

	return &ListeningPipelineResult{
		AudioAssets:     assets,
		GateResults:     gateResults,
		PaperItems:      items,
		Timeline:        timeline,
		RenderArtifacts: artifacts,
		RenderMode:      mode,
		PipelineDigest:  hex.EncodeToString(sum[:]),
	}, nil
}

// buildRenderArtifacts 为每道听力题生成渲染产物：
// static → 签名 URL（QR 贴码载荷；SVG 位图为 render 骨架面，留白如实声明）；
// online → 播放器 URL（首次 play，session=e2e-{paper_id}，冻结实现同语义）.
func buildRenderArtifacts(items []ListeningPaperItem, opts PipelineOptions, mode RenderMode) ([]RenderArtifact, error) {
	store := opts.PlayStore
	if store == nil {
		store = NewInMemoryPlayCountStore()
	}
	out := make([]RenderArtifact, 0, len(items))
	for _, it := range items {
		switch mode {
		case RenderStatic:
			u, err := GenerateSignedURL(it.Audio.AudioID, opts.PaperID, QROptions{Secret: opts.QRSecret})
			if err != nil {
				return nil, err
			}
			out = append(out, RenderArtifact{
				ItemVersionID: it.ItemVersionID,
				AudioID:       it.Audio.AudioID,
				ArtifactType:  ArtifactQRCode,
				SignedURL:     u.SignedURL,
			})
		case RenderOnline:
			r, err := Play(it.Audio.AudioID, "e2e-"+opts.PaperID, it.Audio.URL, store, MaxPlays)
			if err != nil {
				return nil, fmt.Errorf("audio: 在线卷首次播放失败（audio_id=%s）: %w", it.Audio.AudioID, err)
			}
			out = append(out, RenderArtifact{
				ItemVersionID: it.ItemVersionID,
				AudioID:       it.Audio.AudioID,
				ArtifactType:  ArtifactPlayerURL,
				PlayerURL:     r.URL,
				PlayCount:     r.PlayCount,
			})
		}
	}
	return out, nil
}
