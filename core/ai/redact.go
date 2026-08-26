package ai

import "regexp"

// PII 剥离中间件（宪法 D7）：LLM/TTS 调用前对 prompt 文本剥离直标识。
// Go 移植自冻结实现 src/core/ai/ledger/pii_filter.py，语义逐条对齐：
//   - 学生姓名 → 学生A/学生B…（按出现顺序编号）
//   - 电话号码 → [PHONE]；身份证号 → [ID_CARD]；邮箱 → [EMAIL]；地址 → [ADDRESS]
//
// 为什么启发式正则而非 NER 模型：PII 剥离在总线热路径，NER 引入 AI 调用
// （自举问题：剥 PII 的调用本身可能漏 PII）；正则确定性、可审计、零外部
// 依赖。RE2 无回溯，全部模式线性时间。

var (
	// 身份证号：18 位（前 17 位数字，末位数字或 X/x）；先于 phone 应用，避免
	// 长数字串中出生年份段 19xx/20xx 被部分误识别为手机号（冻结实现顺序）.
	reIDCard = regexp.MustCompile(`\d{17}[\dXx]`)
	// 中文手机号：1[3-9]xxxxxxxxx（11 位）.
	rePhone = regexp.MustCompile(`1[3-9]\d{9}`)
	// 邮箱：RFC 5322 简化子集.
	reEmail = regexp.MustCompile(`[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}`)
	// 地址：≥2 连续汉字前缀 + 行政区划/门牌关键字（前缀下限防误伤普通名词）.
	reAddress = regexp.MustCompile(`[\p{Han}]{2,}(?:省|市|区|县|镇|乡|村|路|街|号|弄|室|栋|单元)`)
	// 学生姓名：指人上下文关键字 + 非贪婪 2–4 字姓名（优先匹配 2 字，避免贪婪
	// 吃掉后续关键字）。student_alias_id（ULID/UUID 格式）刻意不在剥离范围，
	// 它是 D7 允许的主库合法身份.
	reNameContext = regexp.MustCompile(`(学生|同学|家长|我叫|姓名[：:]?\s*)(\p{Han}{2,4}?)`)
)

// PII 类型常量（Redact 返回的 stripped 列表值；冻结实现常量名对齐）.
const (
	PIIIDCard  = "id_card"
	PIIPhone   = "phone"
	PIIEmail   = "email"
	PIIAddress = "address"
	PIIName    = "name"
)

// RegexRedactor 是 Redactor 的确定性正则实现。应用次序固定：
// id_card → phone → email → address → name（长格式先剥，防子串误切分）.
//
// fail-closed 语境下的角色差异：本实现永不返回 error（模式线性且与外部状态
// 无关，任何输入都可完成剥离）；error 信道是为更严格的实现保留的表达面——
// 例如接入外部敏感词库校验的实现，「无法确认剥离完成」必须报错而非放行，
// 总线对任何非 nil err 一律拒绝调用并落 rejected 行（X12 无降级开关）.
type RegexRedactor struct{}

// Redact 实现 Redactor：返回剥离后文本与被剥离的 PII 类型列表（首次出现序，
// 不去重可重复计数同一类型的多处命中——观测面保留密度信息）.
func (RegexRedactor) Redact(text string) (string, []string, error) {
	if text == "" {
		return text, nil, nil
	}
	sanitized := text
	var stripped []string

	mark := func(kind string) { stripped = append(stripped, kind) }

	if reIDCard.MatchString(sanitized) {
		sanitized = reIDCard.ReplaceAllString(sanitized, "[ID_CARD]")
		mark(PIIIDCard)
	}
	if rePhone.MatchString(sanitized) {
		sanitized = rePhone.ReplaceAllString(sanitized, "[PHONE]")
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
