package subjectlang

import "github.com/Cloudbird-Software/AI_Web_School/registry"

// objective 构造组卷装配层（assembly）消费的标准 objective 结构：kp_set 单
// 点 + 判定语境三键。语英轮早期生成器产 {"kp": "..."} 简化形态，组卷装配
// 层只认 kp_set（与数学轮 subjectmath.objective 同构）——学科包内容形态
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

// subjectmathGenerator / subjectmathInstance 是数学轮管线接口的包内别名
// （语英轮与数学轮共用同一 Generator/Instance 契约——issue #34 §二）。
type (
	subjectmathGenerator = interface {
		Entry() registry.Entry
		Spec() map[string]any
		Size() int
		Instance(index int) (*subjectmathInstance, error)
	}
	subjectmathInstance = struct {
		TemplateID        string           `json:"template_id"`
		TemplateVersionID string           `json:"template_version_id"`
		Locale            string           `json:"locale"`
		Objective         map[string]any   `json:"objective"`
		InteractionRef    map[string]any   `json:"interaction_ref"`
		Content           map[string]any   `json:"content"`
		ScoringRef        map[string]any   `json:"scoring_ref"`
		ErrorBindings     []map[string]any `json:"error_bindings"`
		Lineage           map[string]any   `json:"lineage"`
	}
)
