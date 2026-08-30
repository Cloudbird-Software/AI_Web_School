package audio

import (
	"errors"
	"fmt"
	"strings"
	"unicode"
)

// 点读错误（冻结实现 PointReadError 细分）：word_index 越界、文本为空或
// 音频时长非法。调用方按 errors.Is 分支处理。
var (
	// ErrEmptyText 表示待分词文本为空（或全空白）.
	ErrEmptyText = errors.New("audio: text 为空，无法分词")
	// ErrInvalidDuration 表示音频时长非法（必须 > 0，毫秒）.
	ErrInvalidDuration = errors.New("audio: duration_ms 非法（必须 > 0）")
	// ErrWordIndexOutOfRange 表示请求的词序号越界.
	ErrWordIndexOutOfRange = errors.New("audio: word_index 越界")
)

// 时间戳来源（冻结实现 method 字段值域）.
const (
	// MethodWordTimings 精确时间戳：TTS 适配器注入 word_timings 时生效.
	MethodWordTimings = "word_timings"
	// MethodEvenSplit 估算时间戳：按文本长度均匀分配.
	MethodEvenSplit = "even_split"
)

// WordTiming 是单个词的精确时间戳范围（毫秒）。
//
// 为什么是类型化切片而非冻结实现的 dict[str, Any]：扩展点语义不变——真实
// TTS 适配器（如 Azure SSML word boundary）在交付面注入 WordTiming 切片
// 即生效，本模块无需修改（冻结实现 tts_metadata["word_timings"] 的 Go 面）.
type WordTiming struct {
	StartMS int
	EndMS   int
}

// PointReadResult 是点读结果（冻结实现 PointReadResult / 验收 #3）：
// 客户端按 [StartMS, EndMS] 片段播放 AudioURL 处音频.
type PointReadResult struct {
	// AudioID 音频素材 id.
	AudioID string
	// WordIndex 请求的词序号（0-based）.
	WordIndex int
	// Word 该位置的文本片段.
	Word string
	// StartMS / EndMS 该词在音频中的时间戳范围（毫秒）.
	StartMS int
	EndMS   int
	// AudioURL 音频可访问 URL.
	AudioURL string
	// Method 时间戳来源（MethodWordTimings / MethodEvenSplit）.
	Method string
}

// isCJK 判断字符是否为 CJK 字符（中文/日文；冻结实现 _CJK_PATTERN 的 rune
// 区间等价：CJK 统一表意文字 + 扩展 A + 兼容区 + 平假名/片假名）。
// 分词规则是语言无关的字符级判断（宪法 A5/X6：不依赖学科语料库）.
func isCJK(r rune) bool {
	switch {
	case r >= 0x4E00 && r <= 0x9FFF: // CJK 统一表意文字
		return true
	case r >= 0x3400 && r <= 0x4DBF: // CJK 扩展 A
		return true
	case r >= 0xF900 && r <= 0xFAFF: // CJK 兼容表意文字
		return true
	case r >= 0x3040 && r <= 0x309F: // 平假名
		return true
	case r >= 0x30A0 && r <= 0x30FF: // 片假名
		return true
	default:
		return false
	}
}

// SplitWords 将文本分词（冻结实现 split_words 对齐，语言无关的字符级分词）：
//
//   - CJK 字符：逐字作为一个 word（低段点读粒度=单字，如「苹果」→「苹」「果」
//     ——中文无空格分词，单字粒度是低段点读的产品语义）；
//   - 非 CJK（英文/数字）：按空白分词，保留连续非空白非 CJK 序列；
//   - 空白字符：跳过（不产生 word）。
//
// 词列表保持原序。与冻结实现的已知边角差异（显式声明）：Python str.isspace
// 含文件分隔符 U+001C–U+001F，Go unicode.IsSpace 不含——正文文本不可达该
// 差异，不影响点读语义。
func SplitWords(text string) []string {
	words := []string{}
	buf := []rune{}
	flush := func() {
		if len(buf) > 0 {
			words = append(words, string(buf))
			buf = buf[:0]
		}
	}
	for _, ch := range text {
		switch {
		case isCJK(ch):
			flush()
			words = append(words, string(ch))
		case unicode.IsSpace(ch):
			flush()
		default:
			buf = append(buf, ch)
		}
	}
	flush()
	return words
}

// PointRead 点读：返回指定词的音频时间戳范围（冻结实现 point_read / 验收 #3）。
//
// 流程：
//  1. 分词（SplitWords）；
//  2. wordIndex 越界 / 文本为空 / durationMS ≤ 0 → 哨兵错误；
//  3. wordTimings 覆盖请求位 → 用精确时间戳（method=word_timings）；
//  4. 否则按词数均匀分配 durationMS（整除估算，method=even_split）——最后一个
//     词取到音频末尾（避免整除丢尾）。
//
// 为什么接受 text/durationMS 而非 AudioAsset：解耦——调用方从 AudioAsset 传入
// 所需字段，本函数不依赖产线类型（与播放服务同模式）。
func PointRead(audioID string, wordIndex int, text string, durationMS int, audioURL string, wordTimings []WordTiming) (*PointReadResult, error) {
	if strings.TrimSpace(text) == "" {
		return nil, ErrEmptyText
	}
	if durationMS <= 0 {
		return nil, fmt.Errorf("%w: duration_ms=%d", ErrInvalidDuration, durationMS)
	}
	words := SplitWords(text)
	if wordIndex < 0 || wordIndex >= len(words) {
		return nil, fmt.Errorf("%w: word_index=%d（文本分词后共 %d 个词，有效范围 0..%d）",
			ErrWordIndexOutOfRange, wordIndex, len(words), len(words)-1)
	}
	word := words[wordIndex]

	// ── 精确时间戳路径（TTS 适配器注入 word_timings 时生效）──
	// 覆盖语义与冻结实现一致：timings 存在但不覆盖请求位 → 落估算路径.
	if len(wordTimings) > 0 && wordIndex < len(wordTimings) {
		t := wordTimings[wordIndex]
		return &PointReadResult{
			AudioID:   audioID,
			WordIndex: wordIndex,
			Word:      word,
			StartMS:   t.StartMS,
			EndMS:     t.EndMS,
			AudioURL:  audioURL,
			Method:    MethodWordTimings,
		}, nil
	}

	// ── 估算路径：均匀分配时间（毫秒整除精度足够，浮点引入不必要复杂度）──
	perWordMS := durationMS / len(words)
	startMS := wordIndex * perWordMS
	endMS := startMS + perWordMS
	if wordIndex == len(words)-1 {
		endMS = durationMS
	}
	return &PointReadResult{
		AudioID:   audioID,
		WordIndex: wordIndex,
		Word:      word,
		StartMS:   startMS,
		EndMS:     endMS,
		AudioURL:  audioURL,
		Method:    MethodEvenSplit,
	}, nil
}
