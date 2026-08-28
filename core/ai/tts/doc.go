// Package tts 承载 TTS 合成统一服务面（架构 v2 §4.6/§4.8；T-W5-015 的 Go
// 重锚定，冻结实现 src/core/ai/tts/router.py 的 tts_synthesize 与
// src/core/audio/producer.py 的合成链收敛于此）。
//
// 职责边界：
//   - Synthesizer.Synthesize 是 TTS 出站唯一服务面：TTSRequest 经 Bus.Call
//     （ModalityTTS）——PII 剥离（D7，fail-closed）、预算门、出站与台账落账
//     全部在总线内完成；本包不重复落账、不持有第二条出站路径；
//   - 学段→语速/默认音色经 profiles.go 注入（宪法 A5：不 import 学段包），
//     与冻结实现 voice_profiles.yaml 逐项对齐并由测试锁定；
//   - 音频产物 id = 完整内容寻址摘要（D3，含音色/voice_id/语速/学段/引擎）——
//     冻结实现截断 32 hex 的碰撞风险在本包修复；同一 id 兼任缓存键与台账
//     artifact_ref（总线 id == 台账产物 id == 缓存键，三位一体）；
//   - 缓存为定容 LRU（冻结实现进程级 dict 无界增长风险的修复面）；
//   - 台账对齐（验收核心）：TTS 行含总线统一字段（模型/耗时/成本/产物 id）+
//     TTS 特有 payload 加性键（char_count / voice_fingerprint），对齐不扩
//     schema（ai_call_ledger JSONB payload 加性键，见 ai.LedgerEntry.Payload）。
//
// 装配纪律：NewSynthesizer 注入的 redactor 必须与目标总线的剥离器同实现
// （生产两侧皆 ai.RegexRedactor）。键派生与出站剥离口径分裂属装配错配，此时
// Synthesizer 按 fail-closed 拒绝交付（X12 无降级开关）；缓存命中路径零出站、
// 零新账（合成事实已由首次调用的台账行覆盖）。
//
// 音频门（发音正确/语速适龄）与对象存储产线不在本包（W6 装配面）；本包只
// 保证「剥离后文本出站 + 产物 id 台账可回溯」。
package tts
