package tts

import (
	"fmt"
)

// 学段档位与音色配置（冻结实现 src/core/ai/tts/voice_profiles.yaml
// contract_version 1.0.0 的 Go 侧镜像，宪法 A5：学段参数经配置注入，
// 核心域不 import 学段包）。
//
// 为什么 Go 侧镜像常量而非解析 yaml：标准库无 yaml，零新依赖纪律（验收 #5）
// 下以表驱动常量承载同一事实源；漂移风险由 TestProfilesMatchFrozenYAML
// 逐项断言 wpm/default_voice/voice_id/engine 锁定——改 yaml 必须同步改表
// （两源纪律与迁移双源同理念）。

// GradeBand 是学段档位三值域（冻结实现 Literal["L","M","H"] 对齐）.
type GradeBand string

// 学段档位（L=1-2 年级 / M=3-4 / H=5-6）.
const (
	BandL GradeBand = "L"
	BandM GradeBand = "M"
	BandH GradeBand = "H"
)

// bandConfig 是单学段的语速与默认音色（yaml grade_bands 条目对齐）.
type bandConfig struct {
	WPM          int
	DefaultVoice string
}

// voiceConfig 是单音色的引擎绑定（yaml voices 条目对齐）.
type voiceConfig struct {
	Name    string
	Engine  string
	VoiceID string
}

// bandTable 学段语速档：L 慢速 120 / M 中速 140 / H 常速 160 wpm.
var bandTable = map[GradeBand]bandConfig{
	BandL: {WPM: 120, DefaultVoice: "female_gentle"},
	BandM: {WPM: 140, DefaultVoice: "female_standard"},
	BandH: {WPM: 160, DefaultVoice: "male_standard"},
}

// voiceTable 音色定义（首年全 mock 引擎，生产替换真实 TTS 适配器）.
var voiceTable = map[string]voiceConfig{
	"female_gentle":   {Name: "female_gentle", Engine: "mock", VoiceID: "voice-female-gentle"},
	"female_standard": {Name: "female_standard", Engine: "mock", VoiceID: "voice-female-standard"},
	"male_standard":   {Name: "male_standard", Engine: "mock", VoiceID: "voice-male-standard"},
	"female_news":     {Name: "female_news", Engine: "mock", VoiceID: "voice-female-news"},
}

// resolveBand 解析学段语速档；未知档位报 ErrUnknownBand（编码面错误，
// 发生在任何出站元数据可得之前——与总线 ErrUnknownTarget 同语义零账行）.
func resolveBand(b GradeBand) (bandConfig, error) {
	cfg, ok := bandTable[b]
	if !ok {
		return bandConfig{}, fmt.Errorf("%w: %q", ErrUnknownBand, b)
	}
	return cfg, nil
}

// resolveVoice 解析音色配置；未知音色报 ErrUnknownVoice.
func resolveVoice(name string) (voiceConfig, error) {
	cfg, ok := voiceTable[name]
	if !ok {
		return voiceConfig{}, fmt.Errorf("%w: %q", ErrUnknownVoice, name)
	}
	return cfg, nil
}
