package subjectlang

import "github.com/Cloudbird-Software/AI_Web_School/registry"

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
