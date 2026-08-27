// 规范化内容摘要（T-W5-020 验收 #4：摘要口径与 D3 内容寻址一致，唯一实现）。
//
// CanonicalJSON 产出确定性规范化文本：对象键序升序、无键序与空白差异、
// UTF-8 直出、数值用最短往返表示。ContentDigest 在其上取 sha256，
// 输出 "sha256:<hex>"（沿用冻结实现的带前缀口径）。
//
// fail-closed：非法 UTF-8、非有限浮点、不支持类型一律返回错误——
// 宁可拒绝判定也绝不产出歧义哈希。
package validators

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"math"
	"reflect"
	"sort"
	"strconv"
	"strings"
	"unicode/utf8"
)

// DigestPrefix 是内容摘要的算法前缀（与冻结实现 _canonical_hash 口径一致）。
const DigestPrefix = "sha256:"

// CanonicalJSON 返回 v 的规范化 JSON 文本（D3 可复现基础）：
// map[string]any 键按升序输出；[]any 保序；分隔符紧凑（',' ':'），
// 键插入顺序与源文本空白不影响结果。
func CanonicalJSON(v any) (string, error) {
	var b strings.Builder
	if err := writeCanonical(&b, v); err != nil {
		return "", err
	}
	return b.String(), nil
}

// ContentDigest 计算 v 的规范化 SHA-256 摘要，形如 "sha256:<hex>"。
// 查重验证器比对的是本函数输出；W6 落库时 content_digest 列必须由
// 本函数回填（复用同一规范化函数，不另造）。
func ContentDigest(v any) (string, error) {
	canon, err := CanonicalJSON(v)
	if err != nil {
		return "", err
	}
	sum := sha256.Sum256([]byte(canon))
	return DigestPrefix + hex.EncodeToString(sum[:]), nil
}

// writeCanonical 递归写出规范化形态。容器仅接受 map[string]any / []any
// （结构化内容的 JSON 解码形态）；标量含 bool/string/json.Number/数值族；
// 其余类型显式报错而非静默走 fmt 透传（防 %v 文本歧义污染哈希）。
func writeCanonical(b *strings.Builder, v any) error {
	switch x := v.(type) {
	case nil:
		b.WriteString("null")
	case bool:
		b.WriteString(strconv.FormatBool(x))
	case string:
		return writeCanonicalString(b, x)
	case json.Number:
		b.WriteString(x.String())
	case map[string]any:
		keys := make([]string, 0, len(x))
		for k := range x {
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
			if err := writeCanonical(b, x[k]); err != nil {
				return fmt.Errorf("键 %q: %w", k, err)
			}
		}
		b.WriteByte('}')
	case []any:
		b.WriteByte('[')
		for i, e := range x {
			if i > 0 {
				b.WriteByte(',')
			}
			if err := writeCanonical(b, e); err != nil {
				return fmt.Errorf("[%d]: %w", i, err)
			}
		}
		b.WriteByte(']')
	default:
		rv := reflect.ValueOf(v)
		switch rv.Kind() {
		case reflect.Int, reflect.Int8, reflect.Int16, reflect.Int32, reflect.Int64:
			b.WriteString(strconv.FormatInt(rv.Int(), 10))
		case reflect.Uint, reflect.Uint8, reflect.Uint16, reflect.Uint32, reflect.Uint64:
			b.WriteString(strconv.FormatUint(rv.Uint(), 10))
		case reflect.Float32, reflect.Float64:
			f := rv.Float()
			bitSize := 64
			if rv.Kind() == reflect.Float32 {
				bitSize = 32
			}
			s, err := canonicalFloat(f, bitSize)
			if err != nil {
				return err
			}
			b.WriteString(s)
		default:
			return fmt.Errorf("validators: 不支持的内容元素类型 %T（fail-closed）", v)
		}
	}
	return nil
}

// canonicalFloat 用最短往返表示格式化浮点数（json.Marshal 同族口径）。
// 非有限值拒绝：NaN/Inf 无唯一规范文本，落入哈希即成判重盲区。
func canonicalFloat(f float64, bitSize int) (string, error) {
	if math.IsNaN(f) || math.IsInf(f, 0) {
		return "", fmt.Errorf("validators: 规范化不接受非有限数 %v（fail-closed）", f)
	}
	return strconv.FormatFloat(f, 'g', -1, bitSize), nil
}

// writeCanonicalString 以 JSON 字符串规则转义写出；非法 UTF-8 显式拒绝，
// 避免 json.Marshal 的 U+FFFD 替换把不同字节序列折叠为同一哈希。
func writeCanonicalString(b *strings.Builder, s string) error {
	if !utf8.ValidString(s) {
		return fmt.Errorf("validators: 字符串含非法 UTF-8 序列（fail-closed）")
	}
	enc, err := json.Marshal(s)
	if err != nil {
		return fmt.Errorf("validators: 字符串编码失败: %w", err)
	}
	b.Write(enc)
	return nil
}
