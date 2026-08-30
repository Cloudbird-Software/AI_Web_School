// Package audio 承载音频资产域（T-W4-022/024/026 的 Go 重锚定，冻结实现
// src/core/audio/，架构 v2 §4.6 / S5）——TTS（core/ai/tts）下游的音频资产
// 管理面：内容寻址、点读时间轴、音频产线、卷面贴码、听力端到端数据面、
// 限次播放。
//
// 职责边界：
//   - 音频内容寻址（D3）：冻结公式 sha256(text|voice|speed|engine) 前 32 hex
//     的跨语言地面真值锚点（content_addressing.go）；Go TTS 总线产物 id 的
//     修复面公式（完整摘要、键含剥离后文本）在 core/ai/tts 单点实现，产线
//     对总线 id 透传（总线 id == 产线 id == 存储寻址，三位一体由单一公式
//     实现保证）；
//   - 点读/播放/贴码/听力均为纯数据面或端口注入面：对象存储写入、播放计数
//     存储、音频过门验证器全部以接口注入（与 TTSEngine 同模式的副作用边界），
//     测试以确定性假件 hermetic 覆盖，生产替换 MinIO/Redis/验证器装配适配器；
//   - QR SVG 位图生成复用 core/render.GenerateQRSVG——零新依赖约束下该实现
//     是显式骨架（ErrQRSVGNotImplemented），本包不静默降级（见 qr.go）；
//   - 宪法 A5/X6：不 import 学科包/学段包；学段参数经 core/ai/tts 配置表注入；
//     宪法 D7：PII 剥离在 TTS 总线内 fail-closed，本包不感知 PII 语义。
//
// 冻结对照：content_addressing.py / point_read.py / producer.py /
// qr_generator.py / listening_e2e.py / player_service.py。
package audio

import (
	"crypto/sha256"
	"encoding/hex"
	"fmt"
)

// audioIDHexLen 音频内容寻址 id 截取长度（冻结实现 hashlib.sha256(...).
// hexdigest()[:32]，32 位 hex）.
const audioIDHexLen = 32

// contentHashPrefix 内容哈希前缀（与 gate validators 的 _canonical_hash
// 前缀风格一致）.
const contentHashPrefix = "sha256:"

// ComputeAudioContentID 计算音频素材内容寻址 id（D3；冻结实现
// compute_audio_content_id 对齐）。
//
// 公式：sha256("{text}|{voice_profile}|{speed}|{engine_digest}") 前 32 位 hex。
//
//   - text：已剥离 PII 的合成文本（D7：PII 剥离由上层调用方负责）；
//   - voiceProfile：音色名（如 female_standard，对应 voice_profiles 表）；
//   - speed：语速 wpm（学段配置：L=120/M=140/H=160）；以通用名 speed 表达
//     「语速参数」、落值为 wpm 整数——命名解耦避免音频层硬绑 TTS 术语；
//   - engineDigest：引擎标识（如 "mock"；生产为真实适配器注册名）。
//
// 相同输入返回相同 id；任何参数变更（text/voice/speed/engine）产生新 id。
// 与冻结实现逐字节互验（golden 测试锁定采样值）。
//
// 与 Go TTS 总线 id 的关系（显式偏离，如实声明）：core/ai/tts 的产物 id 采用
// 修复后的公式（剥离后文本|学段|音色|voice_id|语速|引擎，完整 64 hex——冻结
// 实现截断 32 hex 的碰撞风险已在 T-W5-015 修复）。本函数承担：①冻结 id 体系
// （历史数据/审计回溯）在 Go 侧的可复现入口；②「音频 id 与文本 id 同公式族
// （text|voice|wpm|engine）」的语义证明。产线（producer.go）对 Go 总线 id
// 透传，不再持第二套公式做运行时防御断言——冻结实现的「双实现互验」在 Go 侧
// 收敛为本文件的黄金测试互验。
func ComputeAudioContentID(text, voiceProfile string, speed int, engineDigest string) string {
	payload := fmt.Sprintf("%s|%s|%d|%s", text, voiceProfile, speed, engineDigest)
	sum := sha256.Sum256([]byte(payload))
	return hex.EncodeToString(sum[:])[:audioIDHexLen]
}

// ComputeContentHash 计算音频字节流的规范化哈希（冻结实现
// compute_content_hash 对齐）：'sha256:' + 64 位 hex。
//
// 为什么与 audio_id 分离：audio_id 是「输入参数寻址」（同参数同 id，命中
// 缓存）；content_hash 是「产物字节寻址」（同字节同哈希，用于存储层去重与
// 完整性校验）。两者维度不同——同 audio_id 必同 content_hash（确定性合成）。
func ComputeContentHash(audio []byte) string {
	sum := sha256.Sum256(audio)
	return contentHashPrefix + hex.EncodeToString(sum[:])
}
