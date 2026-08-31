// packs.go —— 多学科包绑定面（P0-2：语文/英语 ingest 入账断链修复，
// 2026-08-31）：mathgen/langgen/enggen 产出的 JSONL 同轴入账。
//
// 断链根因（修复前）：ingest 硬绑 packs/subjectmath——digest 对表 ① 用数学轮
// content-only 摘要口径、② 模板注册表只查 subjectmath.Get，tpl-sl-*/tpl-se-*
// 模板与语英轮三字段摘要（{template_id, content, scoring_ref}）在门上必挂。
//
// 绑定面按 template_id 前缀解析学科包（tpl-sm-/tpl-sl-/tpl-se-，冻结命名
// 惯例）；每包提供：pack_id、唯一摘要口径（packs 既有 InstanceDigest——
// 禁另造第二套规范化，D3）、模板 spec 注册表、谱系缺省生产线 id。
// 语料装载（lang 套件/eng 词表）在首次命中该包时惰性初始化——纯数学批
// 不因语料缺席而失败（fail-closed 只约束被消费的包）。
//
// 为什么住 cmd/：门侧学科确定性检查依赖学科包（core 禁 import 学科包，X6），
// 装配层的学科绑定只能住 cmd/（与 checks.go 同一纪律）。
package main

import (
	"fmt"
	"sync"

	"github.com/Cloudbird-Software/AI_Web_School/packs/subjectenglish"
	"github.com/Cloudbird-Software/AI_Web_School/packs/subjectlang"
	"github.com/Cloudbird-Software/AI_Web_School/packs/subjectmath"
	"github.com/Cloudbird-Software/AI_Web_School/registry"
)

// templateSource 是学科包生成器的最小消费面（Entry + Spec——ingest 不执行
// 生成，只取 spec 摘要做模板版本对表与 item_template_version 落账）。
// 数学/语文/英语三包的 Generator 契约均结构满足.
type templateSource interface {
	Entry() registry.Entry
	Spec() map[string]any
}

// staticTemplate 是 spec-only 的模板源（句子重组等 LLM 档母题：注册表身份
// 与 spec 摘要对表不需要出站执行面）。
type staticTemplate struct {
	entry registry.Entry
	spec  map[string]any
}

func (s staticTemplate) Entry() registry.Entry { return s.entry }
func (s staticTemplate) Spec() map[string]any  { return s.spec }

// packBinding 是一个学科包的入账绑定.
type packBinding struct {
	// PackID item.pack_id（学科包 overlay 身份）.
	PackID string
	// PipelineID 谱系缺省生产线 id（lineage.pipeline.id 缺失时的 source 值）.
	PipelineID string
	// Digest 唯一摘要口径（packs 既有 InstanceDigest 的同源调用——① 的重算面）.
	Digest func(rec *subjectmath.Record) (string, error)
	// Templates 母题 spec 注册表（template_id → 生成器）.
	Templates map[string]templateSource
}

// 前缀 → 包绑定（惰性初始化的语料面）。
var (
	packOnce   sync.Map // pack 前缀 → *sync.Once
	packLoaded sync.Map // pack 前缀 → *packBinding
	packErr    sync.Map // pack 前缀 → error（装载失败缓存：fail-closed 且不反复重试）
)

// manifestDefault 是语料清单路径（与 langgen/enggen 的装载口径一致：仓库根
// 相对路径）。
const manifestDefault = "content/sources/corpus/manifest.yaml"

// packPrefixes 冻结的模板 id 前缀 → 学科包名（顺序即判定序，恒定）。
var packPrefixes = []struct{ prefix, pack string }{
	{"tpl-sm-", "subject-math"},
	{"tpl-sl-", "subject-lang"},
	{"tpl-se-", "subject-english"},
}

// packOf 按模板 id 解析学科包名（前缀匹配；未识别即错误——私造模板在
// 解析层就拒绝，fail-closed）。
func packOf(templateID string) (string, error) {
	for _, p := range packPrefixes {
		if len(templateID) >= len(p.prefix) && templateID[:len(p.prefix)] == p.prefix {
			return p.pack, nil
		}
	}
	return "", fmt.Errorf("模板 id %q 无学科包前缀（tpl-sm-/tpl-sl-/tpl-se-，冻结命名惯例）", templateID)
}

// resolvePack 解析模板 id 的学科包绑定（惰性装载语料面；同一包重复调用
// 返回同一绑定——装载失败同样缓存，整批一致失败不反复重试）。
func resolvePack(templateID string) (*packBinding, error) {
	pack, err := packOf(templateID)
	if err != nil {
		return nil, err
	}
	once, _ := packOnce.LoadOrStore(pack, &sync.Once{})
	bindingOnce := once.(*sync.Once)
	bindingOnce.Do(func() {
		b, err := loadPack(pack)
		if err != nil {
			packErr.Store(pack, err)
			return
		}
		packLoaded.Store(pack, b)
	})
	if v, ok := packErr.Load(pack); ok {
		return nil, fmt.Errorf("学科包 %s 装载失败: %w", pack, v.(error))
	}
	v, _ := packLoaded.Load(pack)
	return v.(*packBinding), nil
}

// loadPack 装载单学科包的绑定面（摘要口径与模板注册表均取 packs 既有实现，
// 本层零规范化逻辑）。
func loadPack(pack string) (*packBinding, error) {
	switch pack {
	case "subject-math":
		// 数学轮：注册表 init 期自装（packs/subjectmath generators），零语料依赖。
		ids := subjectmath.IDs()
		templates := make(map[string]templateSource, len(ids))
		for _, id := range ids {
			g, ok := subjectmath.Get(id)
			if !ok {
				return nil, fmt.Errorf("数学注册表枚举失配: %s", id)
			}
			templates[id] = g
		}
		return &packBinding{
			PackID:     "subject-math",
			PipelineID: "subjectmath-mathgen",
			Digest:     func(rec *subjectmath.Record) (string, error) { return subjectmath.ContentDigest(rec.Content) },
			Templates:  templates,
		}, nil

	case "subject-lang":
		// 语文轮：确定性套件（字辨认/拼音选字/词义关系/偏旁归类）+ 句子重组
		// （半确定档，LLM 路径——审计面 Entry/Spec 零 LLM 依赖）。
		suite, err := subjectlang.BuildDeterministicSuite(manifestDefault)
		if err != nil {
			return nil, fmt.Errorf("语文套件装载失败: %w", err)
		}
		templates := make(map[string]templateSource, len(suite.Generators)+1)
		for _, g := range suite.Generators {
			templates[g.Entry().ID] = g
		}
		reorgEntry, reorgSpec := subjectlang.SentenceReorgTemplateSpec()
		templates[reorgEntry.ID] = staticTemplate{entry: reorgEntry, spec: reorgSpec}
		return &packBinding{
			PackID:     "subject-lang",
			PipelineID: "subjectlang-langgen",
			Digest: func(rec *subjectmath.Record) (string, error) {
				return subjectlang.InstanceDigest((*subjectlang.Instance)(rec.Instance))
			},
			Templates: templates,
		}, nil

	case "subject-english":
		// 英语轮：基础词表两母题（词汇拼写/语法单选）。
		vocab, err := subjectenglish.LoadEnglishVocab(manifestDefault, "eng-basic-vocab-v1")
		if err != nil {
			return nil, fmt.Errorf("英语词表装载失败: %w", err)
		}
		gens, err := subjectenglish.BuiltinGenerators(vocab)
		if err != nil {
			return nil, fmt.Errorf("英语母题装载失败: %w", err)
		}
		templates := make(map[string]templateSource, len(gens))
		for _, g := range gens {
			templates[g.Entry().ID] = g
		}
		return &packBinding{
			PackID:     "subject-english",
			PipelineID: "subjectenglish-enggen",
			Digest: func(rec *subjectmath.Record) (string, error) {
				return subjectenglish.InstanceDigest((*subjectenglish.Instance)(rec.Instance))
			},
			Templates: templates,
		}, nil
	}
	return nil, fmt.Errorf("未知学科包 %q", pack)
}
