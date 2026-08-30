package subjectlang

import (
	"errors"
	"fmt"
	"testing"
)

func testPinyinChars(t *testing.T) *PinyinChars {
	t.Helper()
	p, err := LoadPinyinChars("../../content/sources/corpus/manifest.yaml", SourcePinyinChars)
	if err != nil {
		t.Fatalf("拼音字表装载: %v", err)
	}
	return p
}

// cloneInstance 内存深拷贝（Content/交互/评分引用两级）——JSON 往返会把 int 变
// float64 破坏判分形态，故手工克隆，保证篡改面与生产实例形态一致。
func cloneInstance(src *Instance) *Instance {
	cp := *src
	cp.Content = copyAnyMap(src.Content)
	cp.InteractionRef = copyAnyMap(src.InteractionRef)
	cp.ScoringRef = copyAnyMap(src.ScoringRef)
	return &cp
}

func copyAnyMap(m map[string]any) map[string]any {
	out := make(map[string]any, len(m))
	for k, v := range m {
		switch t := v.(type) {
		case map[string]any:
			out[k] = copyAnyMap(t)
		case []any:
			sc := make([]any, len(t))
			copy(sc, t)
			out[k] = sc
		default:
			out[k] = v
		}
	}
	return out
}

// 语料装载：≥300 常用字、全表唯一、带调拼音、同音组与韵母归组就位。
func TestLoadPinyinChars(t *testing.T) {
	p := testPinyinChars(t)
	if len(p.Entries) < minPinyinChars {
		t.Fatalf("字表 %d 条低于下限 %d", len(p.Entries), minPinyinChars)
	}
	if p.ByChar["天"] != "tiān" || p.ByChar["绿"] != "lǜ" || p.ByChar["日"] != "rì" {
		t.Fatal("基准字目读音不符（带调拼音装载被破坏）")
	}
	if len(p.Correct["zuò"]) < 3 {
		t.Fatalf("同音组 zuò 应含做/坐/作等多字，实得 %v", p.Correct["zuò"])
	}
	if p.FinalOf["天"] != "ian" || p.FinalOf["窗"] != "uang" {
		t.Fatalf("韵母归组异常：天=%q 窗=%q", p.FinalOf["天"], p.FinalOf["窗"])
	}
	if len(p.Finals["ian"]) < 10 {
		t.Fatal("同韵域过小（同韵干扰取材失效）")
	}
	if _, err := LoadPinyinChars("../../content/sources/corpus/manifest.yaml", "no-such-source"); err == nil {
		t.Fatal("缺来源应构造失败（fail-closed）")
	}
}

// 生成器：空间规模、索引界、纯函数性（同 index 同输出）、答案位轮换、选项互异。
func TestPinyinCharGenerator(t *testing.T) {
	p := testPinyinChars(t)
	g, err := newPinyinCharGen(p)
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
	// 答案位确定性轮换：前 4 个 index 覆盖 4 个不同答案位；低位 index 轮换目标字
	//（小批量即可覆盖多字，不只在首字内换干扰）。
	seenPos := map[int]bool{}
	for i := 0; i < 4; i++ {
		inst, err := g.Instance(i)
		if err != nil {
			t.Fatal(err)
		}
		pos := inst.Content["answer"].(int)
		if pos < 1 || pos > 4 {
			t.Fatalf("i=%d 答案位 %d 越界", i, pos)
		}
		if seenPos[pos] {
			t.Fatalf("i=%d 答案位 %d 重复（轮换失效）", i, pos)
		}
		seenPos[pos] = true
		opts := inst.Content["options"].([]any)
		distinct := map[any]bool{}
		for _, o := range opts {
			if distinct[o] {
				t.Fatalf("i=%d 选项重复 %v", i, opts)
			}
			distinct[o] = true
		}
		if len(opts) != 4 {
			t.Fatalf("i=%d 选项数 %d", i, len(opts))
		}
	}
	seenTarget := map[string]bool{}
	for i := 0; i < 8; i++ {
		inst, err := g.Instance(i)
		if err != nil {
			t.Fatal(err)
		}
		tg := inst.Content["target"].(string)
		if seenTarget[tg] {
			t.Fatalf("i=%d 目标字 %q 重复（低位轮换失效）", i, tg)
		}
		seenTarget[tg] = true
	}
}

// 生成器×校验器独立重判：批量 240 实例全过校验、结构互异（摘要唯一）、
// 干扰字全数非同音（正确集零泄漏）、近形/同韵类别落 error_bindings。
func TestBatchPinyinValidAndDistinct(t *testing.T) {
	p := testPinyinChars(t)
	g, err := newPinyinCharGen(p)
	if err != nil {
		t.Fatal(err)
	}
	v, err := NewPinyinCharValidator(p)
	if err != nil {
		t.Fatal(err)
	}
	seen := map[string]bool{}
	kinds := map[string]int{}
	n := 240
	for i := 0; i < n; i++ {
		inst, err := g.Instance(i)
		if err != nil {
			t.Fatalf("Instance(%d): %v", i, err)
		}
		if err := v.Validate(inst); err != nil {
			t.Fatalf("i=%d 校验器拒绝: %v", i, err)
		}
		for _, eb := range inst.ErrorBindings {
			kinds[eb["error_type_id"].(string)]++
		}
		stemPinyin := p.ByChar[inst.Content["target"].(string)]
		for _, o := range inst.Content["options"].([]any) {
			if o.(string) != inst.Content["target"].(string) &&
				p.ByChar[o.(string)] == stemPinyin {
				t.Fatalf("i=%d 干扰字 %q 与题干同音", i, o)
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
	if kinds[distrShape] == 0 || kinds[distrRhyme] == 0 {
		t.Fatalf("干扰类别取材失效：%v", kinds)
	}
}

// combKth3Idx 性质：全组合枚举与 C(n,3) 双射、下标严格递增（unrank 无越界/无重复）。
func TestCombKth3IdxBijective(t *testing.T) {
	const n = 9
	total := comb3(n)
	seen := map[[3]int]bool{}
	for k := 0; k < total; k++ {
		c, err := combKth3Idx(n, k)
		if err != nil {
			t.Fatalf("k=%d: %v", k, err)
		}
		if !(c[0] < c[1] && c[1] < c[2]) {
			t.Fatalf("k=%d 组合 %v 非严格递增", k, c)
		}
		if seen[c] {
			t.Fatalf("k=%d 组合 %v 重复", k, c)
		}
		seen[c] = true
	}
	if len(seen) != total {
		t.Fatalf("枚举 %d != C(%d,3)=%d", len(seen), n, total)
	}
	if _, err := combKth3Idx(n, total); err == nil {
		t.Fatal("越界组合序应错")
	}
	if _, err := combKth3Idx(n, -1); err == nil {
		t.Fatal("负组合序应错")
	}
}

// 校验器负例组：每类篡改必须落在预期哨兵（防共谋——校验器不是生成器的回声）。
func TestPinyinCharValidatorRejects(t *testing.T) {
	p := testPinyinChars(t)
	g, err := newPinyinCharGen(p)
	if err != nil {
		t.Fatal(err)
	}
	v, err := NewPinyinCharValidator(p)
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

	// 母题 id 不符。
	m := cloneInstance(base)
	m.TemplateID = "tpl-sl-other"
	if err := v.Validate(m); !errors.Is(err, ErrPinyinTpl) {
		t.Fatalf("模板篡改应落 ErrPinyinTpl，实得 %v", err)
	}
	// 交互形态不符。
	m = cloneInstance(base)
	m.InteractionRef["interaction_id"] = "text_blank"
	if err := v.Validate(m); !errors.Is(err, ErrPinyinInteraction) {
		t.Fatalf("交互篡改应落 ErrPinyinInteraction，实得 %v", err)
	}
	// content/scoring 答案错位。
	m = cloneInstance(base)
	m.ScoringRef["scorer_params"].(map[string]any)["answer"] = 2
	if err := v.Validate(m); !errors.Is(err, ErrPinyinCrossMisalign) {
		t.Fatalf("答案错位应落 ErrPinyinCrossMisalign，实得 %v", err)
	}
	// 题干拼音槽位缺失。
	m = cloneInstance(base)
	m.Content["stem"] = "选字：请选出正确的字？"
	if err := v.Validate(m); !errors.Is(err, ErrPinyinStemMalformed) {
		t.Fatalf("槽位缺失应落 ErrPinyinStemMalformed，实得 %v", err)
	}
	// 题干拼音与答案字读音不符（换拼音不换答案）。
	m = cloneInstance(base)
	m.Content["stem"] = fmt.Sprintf("选字：读音是「%s」的字是哪一个？", "mò")
	if err := v.Validate(m); !errors.Is(err, ErrPinyinReadingMismatch) {
		t.Fatalf("读音不符应落 ErrPinyinReadingMismatch，实得 %v", err)
	}
	// 干扰字注入同音字（正确集泄漏）。构造同音对：目标字与其同音组内另一字。
	homoTarget, homoPeer := pickHomophonePair(t, p)
	m = cloneInstance(base)
	m.Content["target"] = homoTarget
	m.Content["stem"] = fmt.Sprintf("选字：读音是「%s」的字是哪一个？", p.ByChar[homoTarget])
	opts := m.Content["options"].([]any)
	for i := range opts {
		if i != m.Content["answer"].(int)-1 {
			opts[i] = homoPeer // 只换干扰位
			break
		}
	}
	opts[m.Content["answer"].(int)-1] = homoTarget // 答案位同步目标字，前序形态检查通过
	if err := v.Validate(m); !errors.Is(err, ErrPinyinHomophoneDistractor) {
		t.Fatalf("同音泄漏应落 ErrPinyinHomophoneDistractor，实得 %v", err)
	}
	// 答案字不在字表（答案位选项同步替换，保证前序形态检查通过、精准命中本哨兵）。
	m = cloneInstance(base)
	pos := m.Content["answer"].(int) - 1
	opts = m.Content["options"].([]any)
	opts[pos] = "锚"
	m.Content["target"] = "锚"
	if err := v.Validate(m); !errors.Is(err, ErrPinyinAnswerNotInCorpus) {
		t.Fatalf("表外答案应落 ErrPinyinAnswerNotInCorpus，实得 %v", err)
	}
	// 答案位与 target 不符。
	m = cloneInstance(base)
	m.Content["target"] = "天"
	if err := v.Validate(m); !errors.Is(err, ErrPinyinAnswerMismatch) {
		t.Fatalf("答案位错位应落 ErrPinyinAnswerMismatch，实得 %v", err)
	}
}

// pickHomophonePair 取一对同音字（正确集 ≥2 的任意组）。
func pickHomophonePair(t *testing.T, p *PinyinChars) (string, string) {
	t.Helper()
	for _, e := range p.Entries {
		group := p.Correct[e.Pinyin]
		if len(group) >= 2 {
			return group[0], group[1]
		}
	}
	t.Fatal("字表无同音组（拼音标注异常）")
	return "", ""
}
