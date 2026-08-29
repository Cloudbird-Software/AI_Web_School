// trace_codes.go 承载卷码/QR/题短码生成与校验（T-W2-037；Python 冻结实现
// src/core/render/trace_codes.py 的 Go 重锚定——A4 回溯链的卷面入口）。
//
// 三种码：
//  1. paper_code：卷码 = ULID + Luhn 校验位。打印在卷面，人类可读，防手抄错。
//  2. QR payload：仅含 paper_spec_id + 校验位。扫码后端反查 paper 表定位卷。
//     不含 item_version_id 等实例明文（QR 公开打印，不能泄露题目）。
//  3. item_short_code：题短码 = SHA1(paper_item_id) 前 30 bit → 6 字符
//     base32 + Luhn 校验位。短码（8 位内）便于打印与学生/家长口述；
//     校验位防口述错；反查 paper_item → item_version → gate_certificate → 签发人。
//
// 为什么用 Luhn 而非 CRC32：Luhn 是 1 位校验位，专门为人手工输入设计
// （数字抄错检测率 ~100% 单错、~90% 互换错）；CRC32 校验力强但码太长不利于人读。
//
// 设计要点：
//   - 学科零特判（A5）：本模块是核心域，不 import 学科包/学段包
//   - 全部纯函数（无副作用），可单元测试与确定性复现
//   - 零新依赖：Python 侧 ULID 生成与 QR SVG 位图（qrcode 库）不在 Go 侧
//     重实现——ULID 由调用方供给，QR SVG 留显式骨架（见 GenerateQRSVG）
package render

import (
	"crypto/sha1"
	"encoding/binary"
	"encoding/json"
	"errors"
	"fmt"
	"strconv"
	"strings"
)

// base32Alphabet Crockford base32 字符集（剔除 I/L/O/U 避免混淆）
// （与冻结实现 _BASE32_ALPHABET 逐字符一致）.
const base32Alphabet = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

// shortCodeLen 题短码 body 长度（不含校验位）.
const shortCodeLen = 6

// ErrInvalidCode 是追溯码入参非法的哨兵错误（长度/字符集/空串），
// 细分原因见 wrap 文本.
var ErrInvalidCode = errors.New("render: 追溯码入参非法")

// ErrQRSVGNotImplemented 是 QR SVG 生成的显式骨架哨兵（Python 侧 qrcode
// 库的能力，Go 侧零新依赖约束下不引入；接线时替换实现，签名不变）.
var ErrQRSVGNotImplemented = errors.New("render: QR SVG 生成未实现（零新依赖约束下的 IO 骨架，待接线专用实现）")

// luhnChecksum 计算 data 字符串的 Luhn 校验位（0-9）。
// 字符到数字的映射：数字→本身，字母→ord(c) % 10（按码点，与 Python ord 同构）。
// Luhn 算法：从右往左，每隔一位乘 2，超过 9 减 9，其余位不变，求和模 10；
// 校验位 = (10 - sum % 10) % 10，使整体（含校验位）模 10 为 0。
// 标准 Luhn 用 0-9 数字；我们的码是 base32 字母+数字，字母经映射落入 0-9。
// 这不是密码学安全哈希，只是防手抄错的轻量校验.
func luhnChecksum(data string) int {
	digits := make([]int, 0, len(data))
	for _, ch := range data {
		if ch >= '0' && ch <= '9' {
			digits = append(digits, int(ch-'0'))
		} else {
			digits = append(digits, int(ch)%10)
		}
	}
	// 从右往左，每隔一位乘 2
	total := 0
	for i := 0; i < len(digits); i++ {
		d := digits[len(digits)-1-i]
		if i%2 == 0 {
			if d2 := d * 2; d2 > 9 {
				d = d2 - 9
			} else {
				d = d2
			}
		}
		total += d
	}
	return (10 - total%10) % 10
}

// luhnVerify 验证 data（末位为 Luhn 校验位）是否通过校验。
// 按 rune（码点）处理——Python len(data)/data[-1] 是字符口径，payload 含
// 非 ASCII 时字节切片会错位，这里与冻结实现对齐.
func luhnVerify(data string) bool {
	rs := []rune(data)
	if len(rs) < 2 {
		return false
	}
	payload := string(rs[:len(rs)-1])
	checkDigit := rs[len(rs)-1]
	if checkDigit < '0' || checkDigit > '9' {
		return false
	}
	return int(checkDigit-'0') == luhnChecksum(payload)
}

// GeneratePaperCode 生成卷码 = ULID + Luhn 校验位.
//
// ulid: 26 字符 Crockford base32 字符串（不区分大小写，输出统一大写）。
// 与冻结实现的差异（显式偏离）：Python 侧 ulid 为 None 时现生成；Go 侧
// 零依赖不内置 ULID 生成器，由调用方供给（生成面在 id 服务）.
//
// 返回 27 字符卷码（26 ULID + 1 校验位数字）；长度或字符集不符返回
// ErrInvalidCode 包装错误.
func GeneratePaperCode(ulid string) (string, error) {
	if len([]rune(ulid)) != 26 {
		return "", fmt.Errorf("%w: ULID 长度必须 26，得到 %d", ErrInvalidCode, len([]rune(ulid)))
	}
	upper := strings.ToUpper(ulid)
	for _, ch := range upper {
		if !strings.ContainsRune(base32Alphabet, ch) {
			return "", fmt.Errorf("%w: ULID 含非法字符 %q（base32 字符集：%s）", ErrInvalidCode, ch, base32Alphabet)
		}
	}
	return upper + strconv.Itoa(luhnChecksum(upper)), nil
}

// VerifyPaperCode 验证卷码是否通过 Luhn 校验（27 字符）.
// true 通过校验；false 不通过（含长度/字符集/校验位不符）.
func VerifyPaperCode(code string) bool {
	if len([]rune(code)) != 27 {
		return false
	}
	ulidPart := []rune(code)[:26]
	for _, ch := range ulidPart {
		if !strings.ContainsRune(base32Alphabet, ch) {
			return false
		}
	}
	return luhnVerify(code)
}

// GenerateQRPayload 生成 QR payload = paper_spec_id + Luhn 校验位。
//
// QR 内容设计原则：
//   - 只含 paper_spec_id（卷规格稳定 ID）+ 校验位
//   - 不含 item_version_id 等实例明文（防题目泄露）
//   - 扫码后端用 spec_id 反查 paper 表的 paper_code 字段定位卷
//
// paper_spec_id 为空返回 ErrInvalidCode 包装错误.
func GenerateQRPayload(paperSpecID string) (string, error) {
	if paperSpecID == "" {
		return "", fmt.Errorf("%w: paper_spec_id 不能为空", ErrInvalidCode)
	}
	return paperSpecID + strconv.Itoa(luhnChecksum(paperSpecID)), nil
}

// VerifyQRPayload 验证 QR payload 是否通过 Luhn 校验.
func VerifyQRPayload(payload string) bool {
	return luhnVerify(payload)
}

// ExtractPaperSpecID 从 QR payload 提取 paper_spec_id（去校验位，按 rune
// 剥离末位——与 Python payload[:-1] 字符口径一致）。
// ok=false 表示校验不通过（payload 不可信，调用方必须拒绝）.
func ExtractPaperSpecID(payload string) (string, bool) {
	if !VerifyQRPayload(payload) {
		return "", false
	}
	rs := []rune(payload)
	return string(rs[:len(rs)-1]), true
}

// toBase32Crockford 整数 → Crockford base32 定长字符串（低位在前的
// 5-bit 切片逆序输出，与冻结实现 _to_base32_crockford 同构）.
func toBase32Crockford(n uint64, length int) string {
	chars := make([]byte, length)
	for i := length - 1; i >= 0; i-- {
		chars[i] = base32Alphabet[n&0x1F]
		n >>= 5
	}
	return string(chars)
}

// GenerateItemShortCode 生成题短码 = SHA1(paper_item_id) 前 30 bit →
// 6 字符 base32 + 1 Luhn 校验位.
//
// paper_item_id: paper_item 内部 id（应用层 ULID）。
// 返回 7 字符短码（6 base32 + 1 数字校验位）。
//
// 为什么 30 bit：6 字符 base32 = 30 bit，约 10 亿组合，单卷 100 题远够用；
// 全局唯一靠 paper_item_id 的 SHA1，短码只承担「人读+校验」。
// paper_item_id 为空返回 ErrInvalidCode 包装错误.
func GenerateItemShortCode(paperItemID string) (string, error) {
	if paperItemID == "" {
		return "", fmt.Errorf("%w: paper_item_id 不能为空", ErrInvalidCode)
	}
	digest := sha1.Sum([]byte(paperItemID))
	// 前 30 bit = 4 字节中的前 30 bit（大端取 4 字节右移 2）
	n := binary.BigEndian.Uint32(digest[:4]) >> 2
	body := toBase32Crockford(uint64(n), shortCodeLen)
	return body + strconv.Itoa(luhnChecksum(body)), nil
}

// VerifyItemShortCode 验证题短码是否通过 Luhn 校验（7 字符）.
func VerifyItemShortCode(code string) bool {
	if len([]rune(code)) != shortCodeLen+1 {
		return false
	}
	body := []rune(code)[:shortCodeLen]
	for _, ch := range body {
		if !strings.ContainsRune(base32Alphabet, ch) {
			return false
		}
	}
	return luhnVerify(code)
}

// GenerateQRSVG 生成 QR 码 SVG 字符串（IO 骨架，显式留白）。
//
// Python 侧用 qrcode 库生成 SVG（位图 PNG 嵌入会模糊，SVG 适合嵌入
// HTML/PDF）；Go 侧零新依赖约束（AGENTS 硬规则 3）不引入 QR 位图/矢量
// 生成库，本函数保留签名占位并显式失败——调用方在接线专用实现前必须
// 走 Python 出口或人工回卷贴码，不得静默降级。
func GenerateQRSVG(payload string, boxSize, border int) (string, error) {
	return "", fmt.Errorf("%w: payload=%q boxSize=%d border=%d", ErrQRSVGNotImplemented, payload, boxSize, border)
}

// TraceChain 是回溯链字典（短码 → 题版本 → 签发证书 → 签发人）的类型化形态，
// 对应冻结实现 build_trace_chain 的返回 dict.
type TraceChain struct {
	ItemShortCode     string
	PaperItemID       string
	PaperID           string
	ItemNumber        int64
	ItemVersionID     string
	ItemID            string
	GateCertificateID string // 空 = 无证书
	IssuedBy          string // 空 = 无签发人记录
	IssuedAt          string // 空 = 无签发时间记录
	PolicyVersion     string // 空 = 无策略版本记录
	Lineage           map[string]any
}

// BuildTraceChain 构造回溯链（不查库，纯数据组装）。
//
// 给定 paper_item / item_version / gate_certificate 三行数据（dict 形态，
// 与 DB 行反序列化形状一致），组装成「短码 → 题版本 → 签发证书 → 签发人」
// 回溯链。为什么不查库：保持纯函数特性，查询由调用方负责（不同运行时查法
// 不同）；本函数只做数据形态转换。
//
// gateCertificate 可为 nil（无证书行）；paperItem / itemVersion 必填，
// 缺必要键返回 ErrInvalidCode 包装错误.
func BuildTraceChain(paperItem, itemVersion, gateCertificate map[string]any) (TraceChain, error) {
	out := TraceChain{Lineage: map[string]any{}}
	var err error
	if out.ItemShortCode, err = requiredString(paperItem, "item_short_code"); err != nil {
		return TraceChain{}, err
	}
	if out.PaperItemID, err = requiredString(paperItem, "paper_item_id"); err != nil {
		return TraceChain{}, err
	}
	if out.PaperID, err = requiredString(paperItem, "paper_id"); err != nil {
		return TraceChain{}, err
	}
	if out.ItemNumber, err = requiredInt(paperItem, "item_number"); err != nil {
		return TraceChain{}, err
	}
	if out.ItemVersionID, err = requiredString(itemVersion, "item_version_id"); err != nil {
		return TraceChain{}, err
	}
	if out.ItemID, err = requiredString(itemVersion, "item_id"); err != nil {
		return TraceChain{}, err
	}
	out.GateCertificateID, _ = itemVersion["gate_certificate_id"].(string)
	if lineage, ok := itemVersion["lineage"].(map[string]any); ok {
		out.Lineage = lineage
	}
	if gateCertificate != nil {
		out.IssuedBy, _ = gateCertificate["issued_by"].(string)
		out.IssuedAt, _ = gateCertificate["issued_at"].(string)
		out.PolicyVersion, _ = gateCertificate["policy_version"].(string)
	}
	return out, nil
}

func requiredString(m map[string]any, key string) (string, error) {
	s, _ := m[key].(string)
	if s == "" {
		return "", fmt.Errorf("%w: 缺必要字段 %s", ErrInvalidCode, key)
	}
	return s, nil
}

// requiredInt 取整数必填字段（int 族/整值 float64/json.Number/数字字符串）.
func requiredInt(m map[string]any, key string) (int64, error) {
	v, present := m[key]
	if !present {
		return 0, fmt.Errorf("%w: 缺必要字段 %s", ErrInvalidCode, key)
	}
	switch x := v.(type) {
	case int:
		return int64(x), nil
	case int32:
		return int64(x), nil
	case int64:
		return x, nil
	case float64:
		if x != float64(int64(x)) {
			return 0, fmt.Errorf("%w: %s 必须是整数，得到 %v", ErrInvalidCode, key, x)
		}
		return int64(x), nil
	case json.Number:
		n, err := x.Int64()
		if err != nil {
			return 0, fmt.Errorf("%w: %s 必须是整数，得到 %s", ErrInvalidCode, key, x.String())
		}
		return n, nil
	default:
		return 0, fmt.Errorf("%w: %s 必须是整数，得到 %T", ErrInvalidCode, key, v)
	}
}
