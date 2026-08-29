// canon.go 承载 Python json.dumps(sort_keys=True, ensure_ascii=False) 兼容的
// 规范化 JSON 序列化（PyR 移植专用）。
//
// 为什么需要：AssemblyProfile.Digest 与 SpecTable.ToJSON 在冻结实现里是
// 「内容指纹/序列化无损」语义，跨实现（Python↔Go）必须产出逐字节相同的
// 字符串，指纹才可互验。Go 标准库 encoding/json 与 Python 的差异：
//   - map 键序（Python sort_keys；Go 逐键随机）；
//   - 分隔符（Python 默认 ', ' / ': '；Go 无空格）；
//   - HTML 转义（Go 默认转义 < > &；Python 不转义）；
//   - 浮点（Python repr 最短往返 + 整数值带 ".0"；Go 整数值裸写）。
//
// 本文件对四项逐一拉齐；极值浮点（|e| 超出 [0,1] 域常规范围）不在本域
// 取值空间内，见 pythonFloatRepr 注释的已知边界。
package assembly

import (
	"crypto/sha256"
	"fmt"
	"sort"
	"strconv"
	"strings"
)

// pythonFloatRepr 把 float64 渲染为 Python repr(json) 等价字符串：
// 最短往返（strconv 'g' -1 与 Python repr 同为最短往返算法），整数值补 ".0"。
// 已知边界：Python 在 |x| ≥ 1e16 或 < 1e-4 才切指数记法，Go 'g' 在
// 精度位 ≥ 21 时切——本域浮点全部落在难度/占比/边际的 [0,1] 邻域，
// 不会触及该分界。
func pythonFloatRepr(f float64) string {
	s := strconv.FormatFloat(f, 'g', -1, 64)
	if !strings.ContainsAny(s, ".eE") && !strings.Contains(s, "Inf") && !strings.Contains(s, "NaN") {
		s += ".0"
	}
	return s
}

// canonicalJSON 把 map/slice/标量 组成的值树渲染为
// Python json.dumps(obj, sort_keys=True, ensure_ascii=False) 逐字节等价字符串。
//   - map[string]any：键排序（Python sort_keys）；
//   - []any：保序数组（Python list/tuple）；
//   - string / bool / nil / int / int64 / float64：标量；
//   - 字符串转义走 json.Marshal 后去 HTML 转义面——Python ensure_ascii=False
//     仅转义 " \ 与控制字符，Go SetEscapeHTML(false) 后同规则。
func canonicalJSON(v any) string {
	var b strings.Builder
	writeCanonical(&b, v)
	return b.String()
}

func writeCanonical(b *strings.Builder, v any) {
	switch t := v.(type) {
	case nil:
		b.WriteString("null")
	case bool:
		if t {
			b.WriteString("true")
		} else {
			b.WriteString("false")
		}
	case string:
		writePythonString(b, t)
	case int:
		b.WriteString(strconv.Itoa(t))
	case int64:
		b.WriteString(strconv.FormatInt(t, 10))
	case float64:
		b.WriteString(pythonFloatRepr(t))
	case []any:
		if len(t) == 0 {
			b.WriteString("[]")
			return
		}
		b.WriteByte('[')
		for i, e := range t {
			if i > 0 {
				b.WriteString(", ")
			}
			writeCanonical(b, e)
		}
		b.WriteByte(']')
	case map[string]any:
		keys := make([]string, 0, len(t))
		for k := range t {
			keys = append(keys, k)
		}
		sort.Strings(keys)
		if len(keys) == 0 {
			b.WriteString("{}")
			return
		}
		b.WriteByte('{')
		for i, k := range keys {
			if i > 0 {
				b.WriteString(", ")
			}
			writeCanonical(b, k)
			b.WriteString(": ")
			writeCanonical(b, t[k])
		}
		b.WriteByte('}')
	default:
		// 本包序列化只接受上述形态；未知类型属编程错误，显式 panic 而非静默错指纹。
		panic(fmt.Sprintf("assembly: canonicalJSON 不支持的类型 %T", v))
	}
}

// writePythonString 按 Python json（ensure_ascii=False）规则转义字符串：
// 仅 " \ 与 <0x20 控制字符；其余（含全部非 ASCII）原样输出。
func writePythonString(b *strings.Builder, s string) {
	b.WriteByte('"')
	for _, r := range s {
		switch r {
		case '"':
			b.WriteString(`\"`)
		case '\\':
			b.WriteString(`\\`)
		case '\n':
			b.WriteString(`\n`)
		case '\r':
			b.WriteString(`\r`)
		case '\t':
			b.WriteString(`\t`)
		case '\b':
			b.WriteString(`\b`)
		case '\f':
			b.WriteString(`\f`)
		default:
			if r < 0x20 {
				b.WriteString(fmt.Sprintf(`\u%04x`, r))
			} else {
				b.WriteRune(r)
			}
		}
	}
	b.WriteByte('"')
}

// sha256Hex 返回十六进制 sha256（与 Python hashlib.sha256(...).hexdigest() 同形）。
func sha256Hex(s string) string {
	sum := sha256.Sum256([]byte(s))
	return fmt.Sprintf("%x", sum)
}
