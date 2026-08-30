package subjectlang

import (
	"fmt"

	"github.com/Cloudbird-Software/AI_Web_School/core/gate/validators"
	"github.com/Cloudbird-Software/AI_Web_School/registry"
)

// BuiltinGenerators 装载演示语料面的语文母题（字辨认）。
// 完整确定性档入口见 BuildDeterministicSuite；本函数保留为最小演示面入口。
func BuiltinGenerators(corpus *Corpus) ([]Generator, error) {
	g, err := newCharRecognizeGen(corpus)
	if err != nil {
		return nil, err
	}
	return []Generator{g}, nil
}

// LangSuite 打包确定性档生成器组与各母题独立校验器（cmd/langgen 批量面按
// 模板 id 分派重判——校验器与生成器双写互证，批量面真跑而非只在测试里跑）。
type LangSuite struct {
	Generators []Generator
	// Validators：模板 id → 独立重判函数（enggen 按模板分派同构）。
	Validators map[string]func(*Instance) error
}

// BuildDeterministicSuite 装载全部语文确定性档母题（字辨认/拼音选字/词义关系/
// 偏旁归类）并配齐独立校验器。语料从 manifest 就地装载，任一来源失配即整体
// 失败（fail-closed：不留半残生成器组）。langgen 与测试共用此入口。
func BuildDeterministicSuite(manifestPath string) (*LangSuite, error) {
	corpus, err := LoadCorpus(manifestPath, SourceDemoChars)
	if err != nil {
		return nil, err
	}
	pinyin, err := LoadPinyinChars(manifestPath, SourcePinyinChars)
	if err != nil {
		return nil, err
	}
	wordRel, err := LoadWordRel(manifestPath, SourceWordRel)
	if err != nil {
		return nil, err
	}
	radical, err := LoadRadicalVocab()
	if err != nil {
		return nil, err
	}
	base, err := BuiltinGenerators(corpus)
	if err != nil {
		return nil, err
	}
	pinyinG, err := newPinyinCharGen(pinyin)
	if err != nil {
		return nil, err
	}
	relG, err := newWordRelGen(wordRel)
	if err != nil {
		return nil, err
	}
	radG, err := newRadicalGen(radical)
	if err != nil {
		return nil, err
	}
	pinyinV, err := NewPinyinCharValidator(pinyin)
	if err != nil {
		return nil, err
	}
	relV, err := NewWordRelValidator(wordRel)
	if err != nil {
		return nil, err
	}
	radV, err := NewRadicalValidator(radical)
	if err != nil {
		return nil, err
	}
	// 字辨认（既有母题）走 char_in_corpus 独立判定（答案字必须落在语料字表）。
	chrIn, err := NewCharInCorpus(corpus)
	if err != nil {
		return nil, err
	}
	chrCheck := func(inst *Instance) error {
		opts, err := optionStrings(inst)
		if err != nil {
			return err
		}
		ans, _ := inst.Content["answer"].(int)
		if ans < 1 || ans > len(opts) {
			return fmt.Errorf("答案位 %d 越界", ans)
		}
		if !chrIn.InCorpus(opts[ans-1]) {
			return fmt.Errorf("答案字 %q 不在语料字表", opts[ans-1])
		}
		return nil
	}
	return &LangSuite{
		Generators: append(base, pinyinG, relG, radG),
		Validators: map[string]func(*Instance) error{
			TplCharRecognize: chrCheck,
			TplPinyinChar:    pinyinV.Validate,
			TplWordRel:       relV.Validate,
			TplRadical:       radV.Validate,
		},
	}, nil
}

// NewPinyinCharGenerator 拼音选字生成器（导出面——独立装载场景使用）。
func NewPinyinCharGenerator(tab *PinyinChars) (Generator, error) { return newPinyinCharGen(tab) }

// NewWordRelGenerator 词义关系生成器（导出面）。
func NewWordRelGenerator(rel *WordRel) (Generator, error) { return newWordRelGen(rel) }

// NewRadicalGenerator 偏旁归类生成器（导出面）。
func NewRadicalGenerator(vocab *RadicalVocab) (Generator, error) { return newRadicalGen(vocab) }

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
