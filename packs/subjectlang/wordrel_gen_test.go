package subjectlang

import (
	"errors"
	"fmt"
	"testing"
)

func testWordRel(t *testing.T) *WordRel {
	t.Helper()
	w, err := LoadWordRel("../../content/sources/corpus/manifest.yaml", SourceWordRel)
	if err != nil {
		t.Fatalf("词义关系表装载: %v", err)
	}
	return w
}

// 语料装载：≥100 对、关系白名单、(词,关系) 唯一、关系内角色唯一、基准条目在案。
func TestLoadWordRel(t *testing.T) {
	w := testWordRel(t)
	if len(w.Entries) < minWordRelPairs {
		t.Fatalf("关系对 %d 低于下限 %d", len(w.Entries), minWordRelPairs)
	}
	if got := w.AnswerOf("美丽", RelSynonym); len(got) != 1 || got[0] != "漂亮" {
		t.Fatalf("基准近义条目不符: %v", got)
	}
	if got := w.AnswerOf("高大", RelAntonym); len(got) != 1 || got[0] != "矮小" {
		t.Fatalf("基准反义条目不符: %v", got)
	}
	if w.AnswerOf("美丽", RelAntonym) != nil {
		t.Fatal("未登记关系应查空")
	}
	if len(w.WordList) != 2*len(w.Entries) {
		t.Fatalf("词表 %d ≠ 2×关系对 %d（角色唯一性被破坏）", len(w.WordList), len(w.Entries))
	}
	if _, err := LoadWordRel("../../content/sources/corpus/manifest.yaml", "no-such-source"); err == nil {
		t.Fatal("缺来源应构造失败（fail-closed）")
	}
}

// 生成器：空间规模、索引界、纯函数性、答案位轮换、低位轮换关系对、选项互异。
func TestWordRelGenerator(t *testing.T) {
	w := testWordRel(t)
	g, err := newWordRelGen(w)
	if err != nil {
		t.Fatal(err)
	}
	if g.Size() < 100 {
		t.Fatalf("参数空间 %d < 100", g.Size())
	}
	if _, err := g.Instance(-1); err == nil {
		t.Fatal("负索引应错")
	}
	if _, err := g.Instance(g.Size()); err == nil {
		t.Fatal("上界索引应错")
	}
	a, err := g.Instance(0)
	if err != nil {
		t.Fatal(err)
	}
	b, err := g.Instance(0)
	if err != nil {
		t.Fatal(err)
	}
	if ja, jb := string(mustJSON(a)), string(mustJSON(b)); ja != jb {
		t.Fatal("同 index 两次构造应逐字节一致（纯函数，同 seed 同输出）")
	}
	seenPos, seenWord := map[int]bool{}, map[string]bool{}
	for i := 0; i < 4; i++ {
		inst, err := g.Instance(i)
		if err != nil {
			t.Fatal(err)
		}
		pos := inst.Content["answer"].(int)
		if pos < 1 || pos > 4 || seenPos[pos] {
			t.Fatalf("i=%d 答案位 %d 越界或重复（轮换失效）", i, pos)
		}
		seenPos[pos] = true
		word := inst.Content["word"].(string)
		if seenWord[word] {
			t.Fatalf("i=%d 题词 %q 重复（低位轮换失效）", i, word)
		}
		seenWord[word] = true
		opts := inst.Content["options"].([]any)
		if len(opts) != 4 {
			t.Fatalf("i=%d 选项数 %d", i, len(opts))
		}
		distinct := map[any]bool{}
		for _, o := range opts {
			if distinct[o] {
				t.Fatalf("i=%d 选项重复 %v", i, opts)
			}
			distinct[o] = true
		}
	}
}

// 生成器×校验器独立重判：全量 105 关系对逐条出题全过校验、摘要唯一、
// 两类 kp（lang.sem.synonym/antonym）都在场。
func TestBatchWordRelValidAndDistinct(t *testing.T) {
	w := testWordRel(t)
	g, err := newWordRelGen(w)
	if err != nil {
		t.Fatal(err)
	}
	v, err := NewWordRelValidator(w)
	if err != nil {
		t.Fatal(err)
	}
	seen := map[string]bool{}
	kps := map[string]bool{}
	for i := 0; i < len(w.Entries); i++ {
		inst, err := g.Instance(i)
		if err != nil {
			t.Fatalf("Instance(%d): %v", i, err)
		}
		if err := v.Validate(inst); err != nil {
			t.Fatalf("i=%d 校验器拒绝: %v", i, err)
		}
		kps[inst.Objective["kp"].(string)] = true
		// 干扰词与题词必须无同关系（正确集零泄漏，独立于校验器再证一遍）。
		word := inst.Content["word"].(string)
		relation := inst.Content["relation"].(string)
		for _, o := range inst.Content["options"].([]any) {
			if o.(string) != inst.Content["answer_word"].(string) &&
				containsStr(w.AnswerOf(word, relation), o.(string)) {
				t.Fatalf("i=%d 干扰词 %q 落在正确集", i, o)
			}
		}
		digest, err := InstanceDigest(inst)
		if err != nil {
			t.Fatal(err)
		}
		if seen[digest] {
			t.Fatalf("i=%d 摘要重复（结构互异破坏）", i)
		}
		seen[digest] = true
	}
	if !kps["lang.sem.synonym"] || !kps["lang.sem.antonym"] {
		t.Fatalf("kp 覆盖不全: %v", kps)
	}
}

// 校验器负例组：每类篡改必须落在预期哨兵。
func TestWordRelValidatorRejects(t *testing.T) {
	w := testWordRel(t)
	g, err := newWordRelGen(w)
	if err != nil {
		t.Fatal(err)
	}
	v, err := NewWordRelValidator(w)
	if err != nil {
		t.Fatal(err)
	}
	base, err := g.Instance(0)
	if err != nil {
		t.Fatal(err)
	}
	if err := v.Validate(base); err != nil {
		t.Fatalf("基准实例应通过: %v", err)
	}

	m := cloneInstance(base)
	m.TemplateID = "tpl-sl-other"
	if err := v.Validate(m); !errors.Is(err, ErrWordRelTpl) {
		t.Fatalf("模板篡改应落 ErrWordRelTpl，实得 %v", err)
	}
	m = cloneInstance(base)
	m.InteractionRef["interaction_id"] = "text_blank"
	if err := v.Validate(m); !errors.Is(err, ErrWordRelInteraction) {
		t.Fatalf("交互篡改应落 ErrWordRelInteraction，实得 %v", err)
	}
	m = cloneInstance(base)
	m.ScoringRef["scorer_params"].(map[string]any)["answer"] = 3
	if err := v.Validate(m); !errors.Is(err, ErrWordRelCrossMisalign) {
		t.Fatalf("答案错位应落 ErrWordRelCrossMisalign，实得 %v", err)
	}
	m = cloneInstance(base)
	m.Content["stem"] = "选词：请选出正确的词？"
	if err := v.Validate(m); !errors.Is(err, ErrWordRelStemMalformed) {
		t.Fatalf("槽位缺失应落 ErrWordRelStemMalformed，实得 %v", err)
	}
	// 关系关键词不唯一（相近/相反同时出现）。
	m = cloneInstance(base)
	m.Content["stem"] = "选词：与「美丽」意思相近也是意思相反的词是哪一个？"
	if err := v.Validate(m); !errors.Is(err, ErrWordRelRelationUnknown) {
		t.Fatalf("关系不唯一应落 ErrWordRelRelationUnknown，实得 %v", err)
	}
	// 题干改为相反而答案词仍为近义目标（关系表重判不符）。
	m = cloneInstance(base)
	m.Content["stem"] = fmt.Sprintf("选词：与「%s」意思相反的词是哪一个？", base.Content["word"])
	if err := v.Validate(m); !errors.Is(err, ErrWordRelRelationMismatch) {
		t.Fatalf("关系重判不符应落 ErrWordRelRelationMismatch，实得 %v", err)
	}
	// 题词不在词表。
	m = cloneInstance(base)
	m.Content["stem"] = "选词：与「鎏金」意思相近的词是哪一个？"
	if err := v.Validate(m); !errors.Is(err, ErrWordRelAnswerNotInVocab) {
		t.Fatalf("表外题词应落 ErrWordRelAnswerNotInVocab，实得 %v", err)
	}
	// 答案位与 answer_word 不符。
	m = cloneInstance(base)
	m.Content["answer_word"] = "随便"
	if err := v.Validate(m); !errors.Is(err, ErrWordRelAnswerMismatch) {
		t.Fatalf("答案词错位应落 ErrWordRelAnswerMismatch，实得 %v", err)
	}
	// 干扰词落在正确集（多目标关系注入——white-box 构造关系表验证泄漏防线）。
	leaky := &WordRel{
		SourceID: "fixture",
		Entries:  []WordRelEntry{{Word: "开心", Relation: RelSynonym, Target: "快乐"}},
		WordSet:  map[string]bool{"开心": true, "快乐": true, "高兴": true, "阳光": true},
		WordList: []string{"开心", "快乐", "高兴", "阳光"},
		Answer: map[string][]string{
			relKey("开心", RelSynonym): {"快乐", "高兴"},
		},
	}
	lv, err := NewWordRelValidator(leaky)
	if err != nil {
		t.Fatal(err)
	}
	leakInst := &Instance{
		TemplateID: TplWordRel,
		InteractionRef: map[string]any{
			"interaction_id":     "single_choice",
			"interaction_params": map[string]any{},
		},
		ScoringRef: map[string]any{
			"scorer_id":     "exact_match",
			"scorer_params": map[string]any{"answer": 1},
		},
		Content: map[string]any{
			"stem":        "选词：与「开心」意思相近的词是哪一个？",
			"options":     toAnySlice([]string{"快乐", "高兴", "阳光", "温暖"}),
			"answer":      1,
			"word":        "开心",
			"relation":    RelSynonym,
			"answer_word": "快乐",
		},
	}
	if err := lv.Validate(leakInst); !errors.Is(err, ErrWordRelDistractorInCorrect) {
		t.Fatalf("正确集泄漏应落 ErrWordRelDistractorInCorrect，实得 %v", err)
	}
}
