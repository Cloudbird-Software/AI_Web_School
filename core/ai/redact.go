package ai

import (
	"errors"
	"regexp"
	"strings"
	"unicode/utf8"
)

// PII 剥离中间件（宪法 D7）：LLM/TTS 调用前对 prompt 文本剥离直标识。
// Go 移植自冻结实现 src/core/ai/ledger/pii_filter.py，语义逐条对齐：
//   - 学生姓名 → 学生A/学生B…（按出现顺序编号）
//   - 电话号码 → [PHONE]；身份证号 → [ID_CARD]；邮箱 → [EMAIL]；地址 → [ADDRESS]
//
// T-W5-013 边界修复（相对冻结实现的两处保守收紧，方向均为宁可多剥不漏剥）：
//  1. 姓名规则的分隔符（：/: ，,、；;与空白）从「姓名」分支推广到全部指人
//     关键字——修复「学生：张三」「同学，李四」等紧邻标点形态整体漏脱敏；
//  2. 数字类规则（id_card/phone）增加数字邻接边界判定：候选仅在其左右均无
//     相邻数字（即它是完整数字串的首尾）时才替换——修复长数字串中间片段被
//     截断替换（如订单号 100138123456789 被切成 100[PHONE]9）。字母/汉字紧邻
//     的号码仍照常剥离（内容语义上已分词，无歧义）。
//
// 为什么启发式正则而非 NER 模型：PII 剥离在总线热路径，NER 引入 AI 调用
// （自举问题：剥 PII 的调用本身可能漏 PII）；正则确定性、可审计、零外部
// 依赖。RE2 无回溯，全部模式线性时间。回归防线：core/ai/redact_test.go
// 边界用例 + FuzzRedactNoNameLeak（fuzz 靶仅本地手动跑，见其文件头注释）.

var (
	// 身份证号：18 位（前 17 位数字，末位数字或 X/x）；先于 phone 应用，避免
	// 长数字串中出生年份段 19xx/20xx 被部分误识别为手机号（冻结实现顺序）。
	// 数字邻接边界在 applyBoundedDigits 里判定（RE2 无 lookaround，不能用
	// (?<!\d)/(?!\d) 表达，且 Go \w 是 ASCII 语义、\b 对汉字邻接与 Python
	// 冻结实现行为不一致——故用与冻结正则同形的裸模式 + 手工边界过滤）.
	reIDCard = regexp.MustCompile(`\d{17}[\dXx]`)
	// 中文手机号：1[3-9]xxxxxxxxx（11 位）.
	rePhone = regexp.MustCompile(`1[3-9]\d{9}`)
	// 邮箱：RFC 5322 简化子集.
	reEmail = regexp.MustCompile(`[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}`)
	// 地址：≥2 连续汉字前缀 + 行政区划/门牌关键字（前缀下限防误伤普通名词）.
	// 已知启发式边界（冻结实现同构，本卡不扩scope）：无标点的长汉字连排里
	// 贪婪回溯会把关键字（如「在路上」「订单号」的 路/号）前的整段一并吞为
	// 地址。与冻结实现保持同构记录在案，修复须待上下文级规则而非正则局部.
	reAddress = regexp.MustCompile(`[\p{Han}]{2,}(?:省|市|区|县|镇|乡|村|路|街|号|弄|室|栋|单元)`)
	// 学生姓名：指人上下文关键字 + 可选分隔符（：/: ，,、；;与空白，T-W5-013
	// 从「姓名」分支推广到全部分支）+ 非贪婪 2–4 字姓名（优先匹配 2 字，避免
	// 贪婪吃掉后续关键字，冻结实现同款理由）。student_alias_id（ULID/UUID
	// 格式）刻意不在剥离范围，它是 D7 允许的主库合法身份.
	reNameContext = regexp.MustCompile(`(学生|同学|家长|我叫|姓名)[：:，,、；;]?\s*(\p{Han}{2,4}?)`)
)

// PII 类型常量（Redact 返回的 stripped 列表值；冻结实现常量名对齐）.
const (
	PIIIDCard  = "id_card"
	PIIPhone   = "phone"
	PIIEmail   = "email"
	PIIAddress = "address"
	PIIName    = "name"
)

// ErrRedactUncertain 是脱敏不确定的失败信号（T-W5-013 AC5）：输入边界不可
// 判定时返回，上层按非 nil err 一律 fail-closed（X12 无降级放行开关）.
var ErrRedactUncertain = errors.New("ai: 输入非合法 UTF-8，PII 边界不可判定")

// RegexRedactor 是 Redactor 的确定性正则实现。应用次序固定：
// id_card → phone → email → address → name（长格式先剥，防子串误切分）.
//
// fail-closed 语义（T-W5-013 AC5）：非合法 UTF-8 输入的字符边界不可判定，
// 返回 (原文, nil, ErrRedactUncertain)——零改写零剥离记录，总线侧对非 nil
// err 一律拒绝调用并落 rejected 行（X12 无降级开关）；合法 UTF-8 输入模式
// 线性且与外部状态无关，必然给出确定剥离结果.
type RegexRedactor struct{}

// isASCIIDigit 报告字节是否为十进制数字（数字邻接边界判定用；汉字等非
// ASCII 字节天然充当边界，与冻结实现「号码紧邻文字即分离」的直觉一致）.
func isASCIIDigit(c byte) bool { return c >= '0' && c <= '9' }

// applyBoundedDigits 应用带数字邻接边界的数字类规则：FindAllStringIndex 给出
// 全部候选，仅保留左右均无相邻数字者（即完整数字串的首尾形态），再手工拼接
// 替换。为什么手工拼接而非 ReplaceAllString：连续号码共享单个分隔符时
// （「138…，139…」），前次替换若消费分隔符会饿死下一次的左边界判定；手工
// 拼接不消费任何邻接字符，两个候选各自独立成立。
// 返回替换后文本与是否发生真实替换——被边界否决的候选不计入 stripped
// （观测面诚实：kinds 只反映真实发生的剥离）.
func applyBoundedDigits(re *regexp.Regexp, text, repl string) (string, bool) {
	locs := re.FindAllStringIndex(text, -1)
	kept := make([][]int, 0, len(locs))
	for _, loc := range locs {
		if loc[0] > 0 && isASCIIDigit(text[loc[0]-1]) {
			continue // 左邻是数字：候选只是长数字串的中间片段
		}
		if loc[1] < len(text) && isASCIIDigit(text[loc[1]]) {
			continue // 右邻是数字：同理（如 18 位候选嵌在 20 位串的前缀）
		}
		kept = append(kept, loc)
	}
	if len(kept) == 0 {
		return text, false
	}
	var b strings.Builder
	b.Grow(len(text))
	prev := 0
	for _, loc := range kept {
		b.WriteString(text[prev:loc[0]])
		b.WriteString(repl)
		prev = loc[1]
	}
	b.WriteString(text[prev:])
	return b.String(), true
}

// Redact 实现 Redactor：返回剥离后文本与被剥离的 PII 类型列表（首次出现序，
// 不去重可重复计数同一类型的多处命中——观测面保留密度信息；被边界否决的
// 候选不计入）.
func (RegexRedactor) Redact(text string) (string, []string, error) {
	if text == "" {
		return text, nil, nil
	}
	// AC5 fail-closed：非法 UTF-8 下汉字/数字边界判定失去意义（\p{Han} 对
	// 坏字节的匹配结果不构成可靠语义），宁可拒绝调用也不带病放行.
	if !utf8.ValidString(text) {
		return text, nil, ErrRedactUncertain
	}
	sanitized := text
	var stripped []string

	mark := func(kind string) { stripped = append(stripped, kind) }

	if next, replaced := applyBoundedDigits(reIDCard, sanitized, "[ID_CARD]"); replaced {
		sanitized = next
		mark(PIIIDCard)
	}
	if next, replaced := applyBoundedDigits(rePhone, sanitized, "[PHONE]"); replaced {
		sanitized = next
		mark(PIIPhone)
	}
	if reEmail.MatchString(sanitized) {
		sanitized = reEmail.ReplaceAllString(sanitized, "[EMAIL]")
		mark(PIIEmail)
	}
	if reAddress.MatchString(sanitized) {
		sanitized = reAddress.ReplaceAllString(sanitized, "[ADDRESS]")
		mark(PIIAddress)
	}
	if reNameContext.MatchString(sanitized) {
		seq := 0
		sanitized = reNameContext.ReplaceAllStringFunc(sanitized, func(string) string {
			seq++
			// 第 1..26 个姓名→学生A…学生Z（出现顺序编号）；>26 取模复用字母。
			// 冻结实现 chr(64+n) 在 >26 时产出非法字符，这里钳位更安全，
			// 语义仍落在「学生X 统一指代」内.
			idx := (seq-1)%26 + 1
			return "学生" + string(rune('A'+idx-1))
		})
		mark(PIIName)
	}
	return sanitized, stripped, nil
}
