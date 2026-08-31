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

// objective 构造组卷装配层（assembly）消费的标准 objective 结构：kp_set 单
// 点 + 判定语境三键。与数学轮 subjectmath.objective 同构——学科包内容形态
// 统一，跨科组卷才不需要装配层分方言特判。
func objective(kpCode string) map[string]any {
	return map[string]any{
		"kp_set": []any{
			map[string]any{"dimension": "kp", "code": kpCode},
		},
		"kp_set_mode":     "single",
		"cognitive_level": "remember",
		"gradeband":       "L",
		"graph_release":   "2026.1",
	}
}

// scBlocks 把单选题题干与选项拼为 blocks 方言（render.ItemToIR 的唯一内容
// 方言——与数学轮 expected_content_snapshot 同构：题干 text 块 + 每选项一行
// text 块）。语英轮平铺 stem/options 仅供交互参数与校验器消费，卷面渲染
// 走 blocks；缺 blocks 的 content 在渲染侧是空卷面。
func scBlocks(stem string, opts []string) []any {
	blocks := make([]any, 0, len(opts)+1)
	blocks = append(blocks, map[string]any{"kind": "text", "rendered": stem})
	for i, o := range opts {
		blocks = append(blocks, map[string]any{
			"kind":     "text",
			"rendered": string(rune('A'+i)) + ". " + o,
		})
	}
	return blocks
}

// fillBlocks 把填空题题干拼为 blocks 方言：题干 text 块 + 答题空 fill 块
// （blank_id 与 interaction_params.blank_ids 对齐；fill 块的块类型走 type
// 键——kind 键在 fill 块内是 text/numeric 语义，由 interaction_id 推导）。
func fillBlocks(stem string, blankIDs []string) []any {
	blocks := make([]any, 0, len(blankIDs)+1)
	blocks = append(blocks, map[string]any{"kind": "text", "rendered": stem})
	for _, id := range blankIDs {
		blocks = append(blocks, map[string]any{"type": "fill", "blank_id": id})
	}
	return blocks
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
