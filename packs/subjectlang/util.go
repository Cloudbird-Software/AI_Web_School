package subjectlang

import (
	"encoding/json"
	"path/filepath"

	"gopkg.in/yaml.v3"
)

// yamlUnmarshal 依赖桥（yaml.v3 由 go.sum 已有转直接；与 api 契约测试同依赖）。
func yamlUnmarshal(b []byte, v any) error {
	return yaml.Unmarshal(b, v)
}

// mustJSON 序列化失败即 panic（生成器构造期的不可达路径——纯可序列化类型）。
func mustJSON(v any) []byte {
	b, err := json.Marshal(v)
	if err != nil {
		panic(err) // 纯 map/slice/string 组合，不可达；显式暴露而非静默吞
	}
	return b
}

// joinPath 与 dirOf 配套。
func joinPath(dir, name string) string {
	return filepath.Join(dir, name)
}

// toAnySlice 把字符串切片提升为 []any（ContentDigest 的 D3 fail-closed 面
// 只收 []any——生成器构造的 options 载荷统一走此形态）。
func toAnySlice(ss []string) []any {
	out := make([]any, len(ss))
	for i, v := range ss {
		out[i] = v
	}
	return out
}
