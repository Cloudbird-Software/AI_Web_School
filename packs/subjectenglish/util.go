package subjectenglish

import (
	"encoding/json"
	"path/filepath"

	"gopkg.in/yaml.v3"
)

// yamlUnmarshal 依赖桥（yaml.v3 与 subjectlang 同源依赖，manifest 解析面一致）。
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

// toAnySlice 把字符串切片提升为 []any（content 摘要的 canonical 面只收 []any）。
func toAnySlice(ss []string) []any {
	out := make([]any, len(ss))
	for i, v := range ss {
		out[i] = v
	}
	return out
}

// isVowelLetter 判定 ASCII 小写元音字母（a/e/i/o/u）——a/an 判定的确定性基底。
func isVowelLetter(c byte) bool {
	switch c {
	case 'a', 'e', 'i', 'o', 'u':
		return true
	}
	return false
}
