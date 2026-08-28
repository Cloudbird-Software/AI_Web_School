package subjectlang

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"hash/fnv"
	"io"
	"strings"

	"github.com/Cloudbird-Software/AI_Web_School/core/ai"
	"github.com/Cloudbird-Software/AI_Web_School/registry"
)

// ErrUnsolvableDraft 是句子重组 draft 被可解性校验拒绝的哨兵（issue #34 §六
// 半确定档：draft 永远是 draft——调用方按 errors.Is 分支丢弃重试，绝不带病
// 成实例）。传输失败（caller 错误）不包此哨兵，两类失败可判别。
var ErrUnsolvableDraft = errors.New("subjectlang: 句子重组 draft 未过可解性校验（已丢弃）")

// SentenceReorgDraft 是 LLM draft 的解析形态（与 baml_src/generators/
// lang_sentence.baml 的 SentenceReorg 类 JSON 字段一一对应）。它只是草稿：
// 过 SentenceReorgSolvability.Check 前不得成为实例。
type SentenceReorgDraft struct {
	Sentence    string   `json:"sentence"`
	Answer      string   `json:"answer"`
	Distractors []string `json:"distractors"`
	Explanation string   `json:"explanation"`
}

// SentenceReorgSolvability 判定句子重组 draft 可解性（纯代码，独立于 LLM
// 调用面——输入只有 draft 数据，不持有也不接触任何 Caller）。与确定性档
// char_in_corpus 同纪律：fail-closed，判定域为零即拒绝构造。
type SentenceReorgSolvability struct {
	vocab *WordInVocab
}

// NewSentenceReorgSolvability 构造；语料词表为空即错误（判定域为零不设门）。
func NewSentenceReorgSolvability(c *Corpus) (*SentenceReorgSolvability, error) {
	v, err := NewWordInVocab(c)
	if err != nil {
		return nil, err
	}
	return &SentenceReorgSolvability{vocab: v}, nil
}

// Check 逐条复核可解性契约（与 lang_sentence.baml prompt 规则一一对应）：
//  1. 题干句子非空；
//  2. 挖空词在语料词表内（word_in_vocab）；
//  3. 句子恰好包含挖空词一次（唯一可恢复，挖空位置无歧义）；
//  4. 干扰词恰好 3 个、两两互异、全部在词表内且 ≠ 挖空词；
//  5. 干扰词均不出现在题干句中（避免多解）；
//  6. 解析非空。
//
// 返回 nil 表示可解；非 nil 为具体拒绝原因（生成器包 ErrUnsolvableDraft 丢弃）。
func (v *SentenceReorgSolvability) Check(d *SentenceReorgDraft) error {
	if d == nil {
		return fmt.Errorf("draft 为 nil")
	}
	sentence := strings.TrimSpace(d.Sentence)
	if sentence == "" {
		return fmt.Errorf("题干句子为空")
	}
	answer := strings.TrimSpace(d.Answer)
	if !v.vocab.InVocab(answer) {
		return fmt.Errorf("挖空词 %q 不在语料词表内（word_in_vocab）", answer)
	}
	if strings.Count(sentence, answer) != 1 {
		return fmt.Errorf("句子包含挖空词 %q 的次数 ≠ 1（挖空位置歧义）", answer)
	}
	if len(d.Distractors) != 3 {
		return fmt.Errorf("干扰词数量 %d ≠ 3", len(d.Distractors))
	}
	seen := map[string]bool{}
	for i, raw := range d.Distractors {
		dis := strings.TrimSpace(raw)
		if !v.vocab.InVocab(dis) {
			return fmt.Errorf("干扰词 %d %q 不在语料词表内", i+1, dis)
		}
		if dis == answer {
			return fmt.Errorf("干扰词 %d %q 与挖空词相同", i+1, dis)
		}
		if seen[dis] {
			return fmt.Errorf("干扰词 %d %q 重复", i+1, dis)
		}
		seen[dis] = true
		if strings.Contains(sentence, dis) {
			return fmt.Errorf("干扰词 %d %q 出现在题干句中（多解）", i+1, dis)
		}
	}
	if strings.TrimSpace(d.Explanation) == "" {
		return fmt.Errorf("解析为空")
	}
	return nil
}

// SentenceReorgGenerator 是「句子重组」半确定档母题（issue #34 §六第二档）：
// LLM 挖空产 draft → 代码验可解性 → 全过才成实例；任一失败丢弃 draft。
//
// 与确定性档母题的结构差异：LLM draft 源不是可枚举参数空间，本类型不实现
// Generator 的 Size/Instance(index) 纯索引面（因此不进 BuiltinGenerators 的
// 确定性批量入口）；Entry/Spec 照常暴露供审计。
type SentenceReorgGenerator struct {
	entry  registry.Entry
	spec   map[string]any
	corpus *Corpus
	solv   *SentenceReorgSolvability
	caller ai.Caller
	target string
}

// NewSentenceReorgGenerator 构造。caller 为出站执行面（生产=装配方把
// baml_client 的 GenerateSentenceReorg 包装成 ai.Caller 注入并挂总线台账；
// 测试=fake）；target 为 allowlist 出站目标名。任一前置缺失即错误
// （fail-closed 落构造期，不留半残生成器）。
func NewSentenceReorgGenerator(c *Corpus, caller ai.Caller, target string) (*SentenceReorgGenerator, error) {
	if caller == nil {
		return nil, fmt.Errorf("句子重组生成器：caller 未注入（fail-closed）")
	}
	if strings.TrimSpace(target) == "" {
		return nil, fmt.Errorf("句子重组生成器：target 未指定（allowlist 目标名必填）")
	}
	solv, err := NewSentenceReorgSolvability(c)
	if err != nil {
		return nil, err
	}
	g := &SentenceReorgGenerator{
		entry:  registry.Entry{ID: "tpl-sl-sent-reorg-c", Version: "1.0.0"},
		corpus: c,
		solv:   solv,
		caller: caller,
		target: target,
	}
	g.spec = map[string]any{
		"objective":    "句子重组：从四个词中选出能填回句中空格的词（半确定档：LLM 挖空 + 代码验可解性）",
		"slots":        []string{"sentence", "answer", "distractor_1", "distractor_2", "distractor_3", "explanation"},
		"variation":    []string{"source_word ∈ corpus.words", "answer ∈ corpus.words", "distractors ∈ corpus.words\\answer 且不在句中"},
		"presentation": "四选一选词填空：句子挖空 + 词表选项",
		"answer":       "correct_index(1..4)",
		"provenance":   "LLM draft（baml_src/generators/lang_sentence.baml）→ SentenceReorgSolvability 全过才成实例",
	}
	return g, nil
}

// Entry/Spec 实现审计面（生产线档位 C：LLM 单件级草稿过验后成实例）。
func (g *SentenceReorgGenerator) Entry() registry.Entry { return g.entry }
func (g *SentenceReorgGenerator) Spec() map[string]any  { return g.spec }

// Draft 走一轮「LLM 挖空 → 可解性校验」。source_word 先做出站前预检（不在
// 词表即拒绝，省一次必败出站）；caller 错误按传输失败原样上抛（区别于 draft
// 拒绝）；LLM 产出（含畸形 JSON）一律只是 draft——未过 Check 即以
// ErrUnsolvableDraft 拒绝并丢弃，永远不产生半实例。
func (g *SentenceReorgGenerator) Draft(ctx context.Context, sourceWord, gradeband string) (*Instance, error) {
	sourceWord = strings.TrimSpace(sourceWord)
	if !g.corpus.Words[sourceWord] {
		return nil, fmt.Errorf("题材词 %q 不在语料词表内（出站前预检拒绝）", sourceWord)
	}
	if strings.TrimSpace(gradeband) == "" {
		return nil, fmt.Errorf("gradeband 为空（学段必填）")
	}
	out, err := g.caller.Call(ctx, ai.OutboundRequest{
		Target: g.target,
		Prompt: draftPrompt(sourceWord, gradeband),
	})
	if err != nil {
		return nil, fmt.Errorf("sentence_reorg: 出站失败: %w", err)
	}
	d, err := parseSentenceReorgDraft(out.Content)
	if err != nil {
		return nil, fmt.Errorf("%w: %v", ErrUnsolvableDraft, err)
	}
	if err := g.solv.Check(d); err != nil {
		return nil, fmt.Errorf("%w: %v", ErrUnsolvableDraft, err)
	}
	return g.instance(sourceWord, gradeband, d), nil
}

// parseSentenceReorgDraft 严格解析 draft JSON：非对象形态、未知字段、JSON 后
// 尾随内容一律拒绝（输出契约由 baml_src SentenceReorg 类定义，超纲即丢弃）。
func parseSentenceReorgDraft(content string) (*SentenceReorgDraft, error) {
	dec := json.NewDecoder(strings.NewReader(content))
	dec.DisallowUnknownFields()
	var d SentenceReorgDraft
	if err := dec.Decode(&d); err != nil {
		return nil, fmt.Errorf("draft JSON 解析失败: %w", err)
	}
	// 尾随内容（LLM 在 JSON 后补注释/第二份输出）也拒——契约面止于一份 JSON。
	if err := dec.Decode(&struct{}{}); !errors.Is(err, io.EOF) {
		return nil, fmt.Errorf("draft JSON 后存在尾随内容")
	}
	return &d, nil
}

// instance 从已过验的 draft 构造实例：挖空词与 3 个干扰词组成四选项，答案位
// 由 draft 内容哈希确定性轮换（同 draft 同位序，可回放）。
func (g *SentenceReorgGenerator) instance(sourceWord, gradeband string, d *SentenceReorgDraft) *Instance {
	sentence := strings.TrimSpace(d.Sentence)
	blanked := strings.Replace(sentence, strings.TrimSpace(d.Answer), "____", 1)
	opts := []string{
		strings.TrimSpace(d.Answer),
		strings.TrimSpace(d.Distractors[0]),
		strings.TrimSpace(d.Distractors[1]),
		strings.TrimSpace(d.Distractors[2]),
	}
	correctIdx := int(fnv32(sentence+d.Answer)%4) + 1
	opts[0], opts[correctIdx-1] = opts[correctIdx-1], opts[0]
	stem := fmt.Sprintf("选词填空：从下面四个词中选出恰当的词，填入句子的空格里。\n%s", blanked)
	return &Instance{
		TemplateID: g.entry.ID,
		Locale:     "zh-Hans",
		Objective:  map[string]any{"kp": "lang.sent.reorg", "gradeband": gradeband},
		InteractionRef: map[string]any{
			"interaction_id":     "single_choice",
			"interaction_params": map[string]any{"options": toAnySlice(opts)},
		},
		Content: map[string]any{
			"stem":        stem,
			"sentence":    blanked,
			"options":     toAnySlice(opts),
			"answer":      correctIdx,
			"answer_word": strings.TrimSpace(d.Answer),
			"source_word": sourceWord,
			"explanation": strings.TrimSpace(d.Explanation),
			"source_id":   g.corpus.SourceID,
		},
		ScoringRef: map[string]any{
			"scorer_id":     "exact_match",
			"scorer_params": map[string]any{"answer": correctIdx},
		},
		ErrorBindings: []map[string]any{
			{"slot": "distractor_1", "error_type_id": "lang.sent.confusable"},
			{"slot": "distractor_2", "error_type_id": "lang.sent.confusable"},
			{"slot": "distractor_3", "error_type_id": "lang.sent.confusable"},
		},
		Lineage: map[string]any{
			"tier":        "C",
			"llm_draft":   true,
			"solvability": "sentence_reorg_solvability",
			"params":      map[string]any{"source_word": sourceWord, "gradeband": gradeband},
		},
	}
}

// draftPrompt 组装出站信封。prompt 层唯一事实源是 baml_src/generators/
// lang_sentence.baml（operators.baml：指令与输出契约留在 BAML，禁止 prompt
// 模板散落代码）；Caller 面只传结构化入参，生产装配方把入参解包进 BAML
// 函数调用。
func draftPrompt(sourceWord, gradeband string) string {
	return fmt.Sprintf("task: lang_sentence_reorg\nsource_word: %s\ngradeband: %s", sourceWord, gradeband)
}

// fnv32 稳定哈希（答案位轮换用——同 draft 同位序，纯确定性）。
func fnv32(s string) uint32 {
	h := fnv.New32a()
	_, _ = h.Write([]byte(s)) // hash.Hash 内存实现写入不失败（errcheck 显式化）
	return h.Sum32()
}
