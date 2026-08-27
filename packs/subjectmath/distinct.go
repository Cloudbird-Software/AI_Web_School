package subjectmath

// distinct.go —— 结构互异判定（issue #34 §11.2 H-W6-1 机器判定口径）：
// 「结构互异 = 同母题下已发布实例 content 摘要两两不同（唯一率 100%）」。
//
// 实现要点：
//   - canonical：递归键排序 + 紧凑序列化，**禁 float**（float 的跨平台/
//     跨版本序列化不保证逐字节一致，会污染摘要确定性；数学轮全部整数化）。
//   - 摘要 sha256 前缀 "sha256:"，与仓内内容寻址惯例（tests/golden、
//     item-model.md §3）一致。
//   - AssertPairwiseDistinct 是唯一率 100% 的断言落点：发现碰撞即报错并列出
//     碰撞对——唯一率是断言过的事实，不是宣称。

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"strconv"
)

// DigestAny 返回任意本包构造型值的规范化摘要（canonical + sha256 前缀）。
func DigestAny(v any) (string, error) {
	b, err := canonicalBytes(v)
	if err != nil {
		return "", err
	}
	sum := sha256.Sum256(b)
	return "sha256:" + hex.EncodeToString(sum[:]), nil
}

// ContentDigest 计算实例 content 的结构互异摘要（H-W6-1 判定对象）。
func ContentDigest(content map[string]any) (string, error) {
	return DigestAny(content)
}

// AssertPairwiseDistinct 断言摘要两两不同；碰撞时报出前几个碰撞对样本。
func AssertPairwiseDistinct(digests []string) error {
	seen := make(map[string]int, len(digests))
	for i, d := range digests {
		if first, dup := seen[d]; dup {
			return fmt.Errorf(
				"结构互异破坏（H-W6-1）：实例 #%d 与 #%d content 摘要相同 %s——参数空间存在折叠",
				first, i, d)
		}
		seen[d] = i
	}
	return nil
}

// canonicalBytes 输出确定性字节流：
//   - map[string]any   键升序 → "k":v 紧凑序列
//   - []any            元素保序（数组序本身语义）
//   - string/int/int64/bool/json.Number(整数)/nil
//
// 其余类型一律拒绝：宁可失败也不产出平台相关的“伪规范”字节。
func canonicalBytes(v any) ([]byte, error) {
	var buf bytes.Buffer
	if err := encodeCanonical(&buf, v); err != nil {
		return nil, err
	}
	return buf.Bytes(), nil
}

func encodeCanonical(buf *bytes.Buffer, v any) error {
	switch x := v.(type) {
	case nil:
		buf.WriteString("null")
	case bool:
		if x {
			buf.WriteString("true")
		} else {
			buf.WriteString("false")
		}
	case string:
		encodeString(buf, x)
	case int:
		buf.WriteString(strconv.FormatInt(int64(x), 10))
	case int64:
		buf.WriteString(strconv.FormatInt(x, 10))
	case json.Number:
		n := string(x)
		if !allDigits(n) {
			return fmt.Errorf("canonical: 非整数 json.Number %q 被拒（禁 float 摘要）", n)
		}
		n = strings_trimLeadingZeros(n)
		buf.WriteString(n)
	case []any:
		buf.WriteByte('[')
		for i, e := range x {
			if i > 0 {
				buf.WriteByte(',')
			}
			if err := encodeCanonical(buf, e); err != nil {
				return err
			}
		}
		buf.WriteByte(']')
	case map[string]any:
		buf.WriteByte('{')
		for i, k := range sortedKeys(x) {
			if i > 0 {
				buf.WriteByte(',')
			}
			encodeString(buf, k)
			buf.WriteByte(':')
			if err := encodeCanonical(buf, x[k]); err != nil {
				return err
			}
		}
		buf.WriteByte('}')
	default:
		return fmt.Errorf("canonical: 类型 %T 不支持（仅允许 nil/bool/string/int/int64/json.Number/[]any/map[string]any）", v)
	}
	return nil
}

const hexDigits = "0123456789abcdef"

// encodeString 手写最小 JSON 字符串编码：RFC 8259 必转义集 + 其余 UTF-8 原样。
// 不用 strconv.Quote（其转义策略面向 Go 源码字面量，输出格式非 JSON 规范集）。
func encodeString(buf *bytes.Buffer, s string) {
	buf.WriteByte('"')
	for i := 0; i < len(s); i++ {
		c := s[i]
		switch {
		case c == '"':
			buf.WriteString(`\"`)
		case c == '\\':
			buf.WriteString(`\\`)
		case c == '\b':
			buf.WriteString(`\b`)
		case c == '\f':
			buf.WriteString(`\f`)
		case c == '\n':
			buf.WriteString(`\n`)
		case c == '\r':
			buf.WriteString(`\r`)
		case c == '\t':
			buf.WriteString(`\t`)
		case c < 0x20:
			buf.WriteString(`\u00`)
			buf.WriteByte(hexDigits[c>>4])
			buf.WriteByte(hexDigits[c&0xF])
		default:
			buf.WriteByte(c)
		}
	}
	buf.WriteByte('"')
}
