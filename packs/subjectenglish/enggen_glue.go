package subjectenglish

import (
	"fmt"

	"github.com/Cloudbird-Software/AI_Web_School/core/gate/validators"
	"github.com/Cloudbird-Software/AI_Web_School/registry"
)

// BuiltinGenerators 装载全部英语轮母题（从英语词表构造）。
// enggen 与测试共用此入口；新增母题 = 新文件实现 Generator + 此处追加。
func BuiltinGenerators(vocab *EnglishVocab) ([]Generator, error) {
	spell, err := newVocabSpellGen(vocab)
	if err != nil {
		return nil, err
	}
	gram, err := newGramSCGen(vocab)
	if err != nil {
		return nil, err
	}
	return []Generator{spell, gram}, nil
}

// InstanceDigest 内容摘要（复用 core/gate/validators 的唯一摘要口径——
// D3：同一内容同一摘要，禁另造）。
func InstanceDigest(inst *Instance) (string, error) {
	if inst == nil {
		return "", fmt.Errorf("nil instance")
	}
	return validators.ContentDigest(map[string]any{
		"template_id": inst.TemplateID,
		"content":     inst.Content,
		"scoring_ref": inst.ScoringRef,
	})
}

var _ = registry.Entry{} // 保持 registry 依赖（Generator.Entry 契约）
