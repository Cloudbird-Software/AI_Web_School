package ai

import (
	"crypto/sha256"
	"encoding/hex"
	"math"
	"unicode"
)

// ── 模型单价表（人民币，per 1K tokens）────────────────────────────────
// 与冻结实现 src/core/ai/ledger/ledger.py 的 _MODEL_PRICING_CNY_PER_1K 逐项一致：
// DeepSeek 官网公开价；gpt-4o 按 OpenAI 公开价 ×7（汇率估算）。
// 为什么内置而非配置：与冻结实现对齐保默认可用；W6 成本核算卡接线时再外置，
// 计价口径（per-1K、round 到 1e-6）不变。
var modelPricingCNYPer1K = map[string]struct{ In, Out float64 }{
	"deepseek-chat":     {In: 0.001, Out: 0.002},
	"deepseek-reasoner": {In: 0.004, Out: 0.016},
	"gpt-4o":            {In: 0.1225, Out: 0.49},
}

// ComputeCostCNY 按单价表计算一次调用的人民币成本.
//
// 未知模型返回 0（冻结实现同语义：计价缺失不阻断调用，否则单价表漂移会演变成
// 生产事故）。round 到 1e-6 与 JSONL 账的 float 口径逐位对齐.
func ComputeCostCNY(model string, tokenIn, tokenOut int) float64 {
	p, ok := modelPricingCNYPer1K[model]
	if !ok {
		return 0
	}
	cost := p.In*float64(tokenIn)/1000 + p.Out*float64(tokenOut)/1000
	return math.Round(cost*1e6) / 1e6
}

// HashPrompt 返回 prompt 的 sha256 hex 前 16 位（不存原文，防 PII 残留；
// 冻结实现 ledger.hash_prompt 对齐）。总线只在 ok 行固化剥离后文本的指纹；
// 剥离失败被拒的行连哈希都不产生（「失败原因不含原文 PII」的存储层表达）.
func HashPrompt(prompt string) string {
	sum := sha256.Sum256([]byte(prompt))
	return hex.EncodeToString(sum[:8])
}

// TokenCounter 是 token 计数契约：出站执行面上报真实 usage 缺失时的兜底计量，
// 同时是预算门估算输入侧消耗的统一入口。实现必须确定性（同输入同输出）——
// 预算判定的可复现性依赖于此.
type TokenCounter interface {
	Count(text string) int
}

// SimpleTokenCounter 是零依赖确定性兜底计数器：
//   - 每个 CJK 表意字符记 1 token（中文按字切分的保守近似）；
//   - 其余连续字母/数字串记 1 token。
//
// 这是显式的工程近似（CJK 按字、英文按词都贴近主流 BPE 实际粒度，宁高估不
// 低估——预算门防超限方向的安全侧）；真实 usage 一到即被覆盖（Call 第 6 步）.
type SimpleTokenCounter struct{}

// Count 实现 TokenCounter.
func (SimpleTokenCounter) Count(text string) int {
	n := 0
	inWord := false
	for _, r := range text {
		switch {
		case isCJK(r):
			n++
			inWord = false
		case unicode.IsLetter(r) || unicode.IsDigit(r):
			if !inWord {
				n++
				inWord = true
			}
		default:
			inWord = false
		}
	}
	return n
}

// isCJK 判定 CJK 统一表意文字区（含扩展 A；与地址/姓名剥离正则的工作区一致）.
func isCJK(r rune) bool {
	return (r >= 0x4E00 && r <= 0x9FFF) || (r >= 0x3400 && r <= 0x4DBF)
}
