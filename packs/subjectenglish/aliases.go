package subjectenglish

import "github.com/Cloudbird-Software/AI_Web_School/registry"

// generatorInstance 是数学/语文轮管线契约的包内别名（W6 各学科轮共用同一
// Generator/Instance 形态——issue #34 §二：同一入库服务/同一校验门/同一证据链）。
// 学科包互相独立：这里是本包私有的结构别名，不 import 其他学科包。
type (
	generatorContract = interface {
		Entry() registry.Entry
		Spec() map[string]any
		Size() int
		Instance(index int) (*Instance, error)
	}
	// Instance 是生成产物（item-model.md §2.2 四语义字段 + 谱系，map 形态
	// 与 subjectlang/subjectmath 逐字段同构）。
	Instance = struct {
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

// Generator 是英语轮母题生成器契约（纯函数式：同 index 同实例）。
type Generator = generatorContract
