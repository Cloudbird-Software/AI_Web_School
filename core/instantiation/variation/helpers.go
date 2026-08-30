// helpers.go 承载 variation 包内部的规范化序列化与数值工具。
// 规范化 JSON 与 content_addressing._canonical_json 逐字节同构：
// 复用 core/gate/validators.CanonicalJSON（键序升序、分隔符紧凑、
// UTF-8 直出），不另造。
package variation

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"math/big"
	"sort"
	"strconv"

	"github.com/Cloudbird-Software/AI_Web_School/core/gate/validators"
	"github.com/Cloudbird-Software/AI_Web_School/core/instantiation/expr"
)

// certPayloadDigest 计算 payload 的带前缀 SHA-256 摘要（对齐 _sha256_hex：
// 对原始字符串字节直接取摘要，不走 JSON 规范化）。
func certPayloadDigest(payload string) string {
	sum := sha256.Sum256([]byte(payload))
	return validators.DigestPrefix + hex.EncodeToString(sum[:])
}

// canonicalJSONString Python json.dumps(sort_keys=True, ensure_ascii=False,
// separators=(",",":")) 等价文本（复用唯一实现）。证书 payload 只含
// string/bool/[]string，不可能失败；防御性兜底保证失败可见。
func canonicalJSONString(v any) string {
	s, err := validators.CanonicalJSON(v)
	if err != nil {
		panic("variation: 证书 payload 规范化失败: " + err.Error())
	}
	return s
}

// strAnyList []string → []any（CanonicalJSON 容器口径）。
func strAnyList(ss []string) []any {
	out := make([]any, len(ss))
	for i, s := range ss {
		out[i] = s
	}
	return out
}

// sortedAny 排序后的字符串列表（evidence 的 axis_slots/frozen_slots）。
func sortedAny(ss []string) []any {
	cp := make([]string, len(ss))
	copy(cp, ss)
	sort.Strings(cp)
	return strAnyList(cp)
}

// toInt64 宽松整数转换（对齐 int(value)：int/json.Number/整值字符串；
// 非整数按 Python int() 截断）。
func toInt64(v any) (int64, error) {
	switch x := v.(type) {
	case int:
		return int64(x), nil
	case int64:
		return x, nil
	case float64:
		return int64(x), nil // Python int(5.7) 截断
	case json.Number:
		if i, ok := new(big.Int).SetString(string(x), 10); ok && i.IsInt64() {
			return i.Int64(), nil
		}
		f, err := strconv.ParseFloat(string(x), 64)
		if err != nil {
			return 0, fmt.Errorf("无法转为 int: %q", string(x))
		}
		return int64(f), nil
	case string:
		if i, err := strconv.ParseInt(x, 10, 64); err == nil {
			return i, nil
		}
		f, err := strconv.ParseFloat(x, 64)
		if err != nil {
			return 0, fmt.Errorf("无法转为 int: %q", x)
		}
		return int64(f), nil
	default:
		return 0, fmt.Errorf("无法转为 int: %T", v)
	}
}

// numberLiteral 参数值 → 数字字面量文本（对齐 str(value) 后交给
// Decimal/Fraction 解析的口径）。
func numberLiteral(v any) string {
	switch x := v.(type) {
	case string:
		return x
	case json.Number:
		return string(x)
	case int:
		return strconv.Itoa(x)
	case int64:
		return strconv.FormatInt(x, 10)
	case float64:
		return strconv.FormatFloat(x, 'g', -1, 64)
	default:
		b, _ := json.Marshal(x)
		return string(b)
	}
}

func newRatSetString(s string) (*big.Rat, bool) {
	return new(big.Rat).SetString(s)
}

func bigRatFromInt(i int64) *big.Rat {
	return new(big.Rat).SetInt64(i)
}

// valuesEqualAny 跨类型宽松相等（对齐 Python == 的 choice 索引定位）。
func valuesEqualAny(a, b any) bool {
	av, aerr := expr.ToValue(a)
	bv, berr := expr.ToValue(b)
	if aerr != nil || berr != nil {
		return false
	}
	return expr.ValuesEqual(av, bv)
}
