package subjectlang

import (
	"errors"
	"testing"
)

func testRadicalVocab(t *testing.T) *RadicalVocab {
	t.Helper()
	v, err := LoadRadicalVocab()
	if err != nil {
		t.Fatalf("偏旁数据装载: %v", err)
	}
	return v
}

// 内置数据：规模下限、一字一部、基准归部条目、构造期互证（重复归部/空组即停线）。
func TestLoadRadicalVocab(t *testing.T) {
	v := testRadicalVocab(t)
	if len(v.Chars) < minRadicalChars {
		t.Fatalf("例字 %d 低于下限 %d", len(v.Chars), minRadicalChars)
	}
	if len(v.Radicals) < minRadicalKinds {
		t.Fatalf("偏旁 %d 种低于下限 %d", len(v.Radicals), minRadicalKinds)
	}
	if v.CharRadical["河"] != "氵" || v.CharRadical["想"] != "心" || v.CharRadical["跑"] != "足" {
		t.Fatal("基准归部条目不符")
	}
	// 坏数据负例：一字两部必须被构造期拒绝。
	dup := []radicalGroup{
		{"氵", []string{"江", "河"}},
		{"木", []string{"桥", "河"}},
	}
	if _, err := buildRadicalVocab(dup); err == nil {
		t.Fatal("重复归部应构造失败（一字一部口径）")
	}
	empty := []radicalGroup{{"氵", nil}}
	if _, err := buildRadicalVocab(empty); err == nil {
		t.Fatal("空例字组应构造失败（fail-closed）")
	}
}

// 生成器：空间规模、索引界、纯函数性、答案位轮换、低位轮换例字、选项互异。
func TestRadicalGenerator(t *testing.T) {
	v := testRadicalVocab(t)
	g, err := newRadicalGen(v)
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
	seenPos, seenChar := map[int]bool{}, map[string]bool{}
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
		ch := inst.Content["char"].(string)
		if seenChar[ch] {
			t.Fatalf("i=%d 例字 %q 重复（低位轮换失效）", i, ch)
		}
		seenChar[ch] = true
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

// 生成器×校验器独立重判：全量例字逐字出题全过校验、摘要唯一、干扰全为他部。
func TestBatchRadicalValidAndDistinct(t *testing.T) {
	v := testRadicalVocab(t)
	g, err := newRadicalGen(v)
	if err != nil {
		t.Fatal(err)
	}
	val, err := NewRadicalValidator(v)
	if err != nil {
		t.Fatal(err)
	}
	seen := map[string]bool{}
	for i := 0; i < len(v.Chars); i++ {
		inst, err := g.Instance(i)
		if err != nil {
			t.Fatalf("Instance(%d): %v", i, err)
		}
		if err := val.Validate(inst); err != nil {
			t.Fatalf("i=%d 校验器拒绝: %v", i, err)
		}
		ch := inst.Content["char"].(string)
		for _, o := range inst.Content["options"].([]any) {
			if o.(string) != inst.Content["radical"].(string) &&
				v.CharRadical[ch] == o.(string) {
				t.Fatalf("i=%d 干扰偏旁 %q 恰为本字归部", i, o)
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
}

// 校验器负例组：每类篡改必须落在预期哨兵。
func TestRadicalValidatorRejects(t *testing.T) {
	v := testRadicalVocab(t)
	g, err := newRadicalGen(v)
	if err != nil {
		t.Fatal(err)
	}
	val, err := NewRadicalValidator(v)
	if err != nil {
		t.Fatal(err)
	}
	base, err := g.Instance(0)
	if err != nil {
		t.Fatal(err)
	}
	if err := val.Validate(base); err != nil {
		t.Fatalf("基准实例应通过: %v", err)
	}

	m := cloneInstance(base)
	m.TemplateID = "tpl-sl-other"
	if err := val.Validate(m); !errors.Is(err, ErrRadicalTpl) {
		t.Fatalf("模板篡改应落 ErrRadicalTpl，实得 %v", err)
	}
	m = cloneInstance(base)
	m.InteractionRef["interaction_id"] = "text_blank"
	if err := val.Validate(m); !errors.Is(err, ErrRadicalInteraction) {
		t.Fatalf("交互篡改应落 ErrRadicalInteraction，实得 %v", err)
	}
	m = cloneInstance(base)
	m.ScoringRef["scorer_params"].(map[string]any)["answer"] = 4
	if err := val.Validate(m); !errors.Is(err, ErrRadicalCrossMisalign) {
		t.Fatalf("答案错位应落 ErrRadicalCrossMisalign，实得 %v", err)
	}
	m = cloneInstance(base)
	m.Content["stem"] = "选偏旁：请选出正确的偏旁？"
	if err := val.Validate(m); !errors.Is(err, ErrRadicalStemMalformed) {
		t.Fatalf("槽位缺失应落 ErrRadicalStemMalformed，实得 %v", err)
	}
	// 例字不在数据（答案位选项同步替换，保证前序形态检查通过、精准命中本哨兵）。
	m = cloneInstance(base)
	pos := m.Content["answer"].(int) - 1
	opts := m.Content["options"].([]any)
	opts[pos] = "钅"
	m.Content["radical"] = "钅"
	m.Content["char"] = "锋"
	m.Content["stem"] = "选偏旁：「锋」字的偏旁是哪一个？"
	if err := val.Validate(m); !errors.Is(err, ErrRadicalCharNotInVocab) {
		t.Fatalf("数据外例字应落 ErrRadicalCharNotInVocab，实得 %v", err)
	}
	// 归部标注与答案位不符。
	m = cloneInstance(base)
	m.Content["radical"] = "木"
	if err := val.Validate(m); !errors.Is(err, ErrRadicalAnswerMismatch) {
		t.Fatalf("答案位错位应落 ErrRadicalAnswerMismatch，实得 %v", err)
	}
	// 干扰位注入未知偏旁（合法性防线）。
	m = cloneInstance(base)
	opts = m.Content["options"].([]any)
	for i := range opts {
		if i != m.Content["answer"].(int)-1 {
			opts[i] = "饕"
			break
		}
	}
	if err := val.Validate(m); !errors.Is(err, ErrRadicalDistractorBad) {
		t.Fatalf("未知干扰偏旁应落 ErrRadicalDistractorBad，实得 %v", err)
	}
}
