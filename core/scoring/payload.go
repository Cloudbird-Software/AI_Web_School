package scoring

// 作答载荷与评分参数的取值助手（PyR 评分域补全）。
//
// Runner 以 string 承载作答原文（registry.Scorer.Score 契约面）；结构化作答
// （selected/blanks/pairs/elements/steps 等交互形态）经 JSON 序列化进入该
// string——各评分器按交互形态解码，解码失败按裸字符串处理（文本作答的常态
// 形态，Python 冻结实现 response 形态分派的 JSON 通道投影）。

import (
	"encoding/json"
	"strconv"
)

// decodeAnswer 解码作答载荷：合法 JSON → 解码值（object/array/标量）；
// 否则原样作为字符串.
func decodeAnswer(answer string) any {
	var v any
	// 解码失败即文本作答：不是错误，是载荷的常态形态之一.
	if err := json.Unmarshal([]byte(answer), &v); err != nil {
		return answer
	}
	return v
}

// scalarString 把标量值统一为可比字符串（Python str() 投影：JSON 数字经
// float64 最短表示、布尔取 JSON 小写口径；两侧同投影，判定一致性不受影响）.
func scalarString(v any) string {
	switch x := v.(type) {
	case nil:
		return ""
	case string:
		return x
	case bool:
		if x {
			return "true"
		}
		return "false"
	case float64:
		return strconv.FormatFloat(x, 'f', -1, 64)
	case int:
		return strconv.Itoa(x)
	case int64:
		return strconv.FormatInt(x, 10)
	case json.Number:
		return x.String()
	default:
		blob, err := json.Marshal(x)
		if err != nil {
			return ""
		}
		return string(blob)
	}
}

// paramFloat 取数值（JSON 通道 float64 + 进程内 int/int64 + 数字字符串——
// Python float() 投影）；非数值第二返回值为 false.
func paramFloat(v any) (float64, bool) {
	switch x := v.(type) {
	case float64:
		return x, true
	case int:
		return float64(x), true
	case int64:
		return float64(x), true
	case json.Number:
		f, err := x.Float64()
		return f, err == nil
	case string:
		f, err := strconv.ParseFloat(x, 64)
		return f, err == nil
	default:
		return 0, false
	}
}

// stringSlice 把 []any 载荷投影为字符串切片（非数组返回 nil）.
func stringSlice(v any) []string {
	raw, ok := v.([]any)
	if !ok {
		return nil
	}
	out := make([]string, 0, len(raw))
	for _, x := range raw {
		out = append(out, scalarString(x))
	}
	return out
}
