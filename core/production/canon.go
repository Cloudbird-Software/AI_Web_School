// canon.go 承载公式二内容寻址（Python 冻结基准
// src/core/models/content_addressing.py 的 compute_canonical_item_version_id
// 及其 _canonical_json/_sha256_hex 的 Go 移植，逐字节对齐）。
//
// 为什么不复用 core/gate/validators 的 CanonicalJSON：两处规范化口径在浮点
// 上不同——Python json.dumps 的浮点渲染走 repr（整数值浮点带 ".0"，如
// 2.0 → "2.0"），validators 口径为 strconv 'g' 最短往返（2.0 → "2"）。
// 跨实现（Python↔Go）互验要求逐字节一致，故本包按冻结公式独立实现：
//   - 键序升序（sort_keys=True）；
//   - 分隔符紧凑（separators=(",", ":")）；
//   - 非 ASCII 原样 UTF-8 直出（ensure_ascii=False）；
//   - 浮点 repr 最短往返、整数值补 ".0"（pythonFloatRepr）；
//   - json.Number 原文直出（JSON 通道数字不重解析，与冻结行为一致）。
//
// D3：同一输入必产生同一输出——重复命题/粘贴产生同 id，入库时作去重提示。
package production

import (
	"crypto/sha256"
	"encoding/json"
	"fmt"
	"sort"
	"strconv"
	"strings"
	"unicode/utf8"
)

// DigestPrefix 是内容摘要的算法前缀（与冻结 _sha256_hex 口径一致）.
const DigestPrefix = "sha256:"

// canonicalJSON 把值树渲染为 Python
// json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
// 逐字节等价字符串。非法 UTF-8 / 非有限浮点 / 不支持类型显式报错
// （fail-closed：证明不了的摘要不产出，零歧义哈希）.
func canonicalJSON(v any) (string, error) {
	var b strings.Builder
	if err := writeCanonical(&b, v); err != nil {
		return "", err
	}
	return b.String(), nil
}

func writeCanonical(b *strings.Builder, v any) error {
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
		return writeCanonicalString(b, t)
	case int:
		b.WriteString(strconv.Itoa(t))
	case int64:
		b.WriteString(strconv.FormatInt(t, 10))
	case float64:
		s, err := pythonFloatRepr(t)
		if err != nil {
			return err
		}
		b.WriteString(s)
	case json.Number:
		// JSON 通道数字原文直出（content/publish.go decodeJSONB 同纪律：
		// 浮点重解析即口径漂移）.
		if !json.Valid([]byte(t)) {
			return fmt.Errorf("production: json.Number %q 非法数字原文", string(t))
		}
		b.WriteString(string(t))
	case []any:
		b.WriteByte('[')
		for i, e := range t {
			if i > 0 {
				b.WriteByte(',')
			}
			if err := writeCanonical(b, e); err != nil {
				return fmt.Errorf("[%d]: %w", i, err)
			}
		}
		b.WriteByte(']')
	case map[string]any:
		keys := make([]string, 0, len(t))
		for k := range t {
			keys = append(keys, k)
		}
		sort.Strings(keys)
		b.WriteByte('{')
		for i, k := range keys {
			if i > 0 {
				b.WriteByte(',')
			}
			if err := writeCanonicalString(b, k); err != nil {
				return err
			}
			b.WriteByte(':')
			if err := writeCanonical(b, t[k]); err != nil {
				return fmt.Errorf("键 %q: %w", k, err)
			}
		}
		b.WriteByte('}')
	default:
		return fmt.Errorf("production: canonicalJSON 不支持的类型 %T（fail-closed）", v)
	}
	return nil
}

// pythonFloatRepr 把 float64 渲染为 Python repr(json) 等价字符串：最短往返
// + 整数值补 ".0"。已知边界：Python 在 |x| ≥ 1e16 或 < 1e-4 才切指数记法，
// Go 'g' 在精度位 ≥ 21 时切——本域浮点全在分值/宽松度/容差的 [0,1] 邻域，
// 不会触及该分界（core/assembly/canon.go 同结论）.
func pythonFloatRepr(f float64) (string, error) {
	if f != f || f > 1.7976931348623157e308 || f < -1.7976931348623157e308 {
		return "", fmt.Errorf("production: 规范化不接受非有限数 %v（fail-closed）", f)
	}
	s := strconv.FormatFloat(f, 'g', -1, 64)
	if !strings.ContainsAny(s, ".eE") && !strings.Contains(s, "Inf") && !strings.Contains(s, "NaN") {
		s += ".0"
	}
	return s, nil
}

// writeCanonicalString 按 Python json（ensure_ascii=False）规则转义字符串：
// 仅 " \ 与 <0x20 控制字符；其余（含全部非 ASCII）原样输出。非法 UTF-8
// 显式拒绝，避免 U+FFFD 替换把不同字节序列折叠为同一哈希.
func writeCanonicalString(b *strings.Builder, s string) error {
	if !utf8.ValidString(s) {
		return fmt.Errorf("production: 字符串含非法 UTF-8 序列（fail-closed）")
	}
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
	return nil
}

// ComputeCanonicalItemVersionID 是契约 §3 公式二的 Go 移植：
//
//	H( canonical( objective, interaction_ref, content, scoring_ref,
//	              error_bindings ), locale )
//
// error_bindings 形参保持 any 与冻结实现对齐（实际数据是 list[dict]；
// canonical JSON 对 dict 与 list 均规范化）。同一内容（六块完全一致 +
// 同 locale）必得同一 id（D3）。失败即返回错误，绝不产出歧义哈希.
func ComputeCanonicalItemVersionID(
	objective any,
	interactionRef any,
	content any,
	scoringRef any,
	errorBindings any,
	locale string,
) (string, error) {
	canonical, err := canonicalJSON(map[string]any{
		"o":  objective,
		"ir": interactionRef,
		"c":  content,
		"sr": scoringRef,
		"eb": errorBindings,
		"l":  locale,
	})
	if err != nil {
		return "", fmt.Errorf("production: 公式二规范化失败: %w", err)
	}
	sum := sha256.Sum256([]byte(canonical))
	return DigestPrefix + fmt.Sprintf("%x", sum), nil
}
