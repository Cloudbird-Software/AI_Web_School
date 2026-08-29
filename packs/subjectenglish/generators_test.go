package subjectenglish

import (
	"strings"
	"testing"
)

// generators_test.go：母题结构与产能实证（subjectlang/math 同构口径）。
//   - 空间规模/索引界/纯函数性；
//   - 生成器×校验器全域一致 + content 全域无碰撞（全空间扫描）；
//   - 规则双写互证：生成器侧与校验器侧推导在全词域逐词一致。

// 结构健全性 + 纯函数性（两母题共用扫描骨架）。
func assertGeneratorShape(t *testing.T, g Generator, validate func(*Instance) error) {
	t.Helper()
	if g.Size() <= 0 {
		t.Fatal("参数空间应非空")
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
		t.Fatal("同 index 两次构造应逐字节一致（纯函数）")
	}
	if err := validate(a); err != nil {
		t.Fatalf("首个实例应过独立校验器: %v", err)
	}
	if a.Locale != "en" || a.Lineage["tier"] != "A" {
		t.Fatal("locale/tier 契约形态不符")
	}
}

func TestVocabSpellGeneratorShape(t *testing.T) {
	v := testVocab(t)
	g, err := newVocabSpellGen(v)
	if err != nil {
		t.Fatal(err)
	}
	if g.Entry().ID != TplVocabSpell {
		t.Fatalf("模板 id=%q", g.Entry().ID)
	}
	if g.Size() != len(v.EntryList) {
		t.Fatalf("拼写空间 %d ≠ 词表 %d", g.Size(), len(v.EntryList))
	}
	vld, err := NewVocabSpellValidator(v)
	if err != nil {
		t.Fatal(err)
	}
	assertGeneratorShape(t, g, vld.Validate)
	inst, _ := g.Instance(0)
	stem := inst.Content["stem"].(string)
	answer := inst.Content["answer"].(string)
	if !strings.Contains(stem, v.Entries[answer].Gloss) || !strings.Contains(stem, answer[:1]) {
		t.Fatalf("题干缺释义/首字母槽位: %q", stem)
	}
	if got := inst.ScoringRef["scorer_id"]; got != "exact_match" {
		t.Fatalf("scorer=%v（应为 exact_match 字符串填空）", got)
	}
	if got := inst.InteractionRef["interaction_id"]; got != "text_blank" {
		t.Fatalf("interaction=%v", got)
	}
}

func TestGramSCGeneratorShape(t *testing.T) {
	v := testVocab(t)
	g, err := newGramSCGen(v)
	if err != nil {
		t.Fatal(err)
	}
	if g.Entry().ID != TplGramSC {
		t.Fatalf("模板 id=%q", g.Entry().ID)
	}
	wantSize := 2*len(v.NounList) + len(thirdPersonVerbs)
	if g.Size() != wantSize {
		t.Fatalf("语法空间 %d ≠ 2×名词%d+三单%d", g.Size(), len(v.NounList), len(thirdPersonVerbs))
	}
	vld, err := NewGramSCValidator(v)
	if err != nil {
		t.Fatal(err)
	}
	assertGeneratorShape(t, g, vld.Validate)
}

// 规则双写互证：生成器侧与校验器侧实现在全词域逐词一致（含规则各类代表点）。
func TestRuleImplsAgreeAcrossDomain(t *testing.T) {
	v := testVocab(t)
	spots := map[string]struct{ gen, ref string }{
		// 复数规则各类
		"book":   {"books", "books"},
		"box":    {"boxes", "boxes"},
		"watch":  {"watches", "watches"},
		"baby":   {"babies", "babies"},
		"day":    {"days", "days"},
		"monkey": {"monkeys", "monkeys"},
		// 三单规则各类
		"run":   {"runs", "runs"},
		"go":    {"goes", "goes"},
		"do":    {"does", "does"},
		"teach": {"teaches", "teaches"},
		"relax": {"relaxes", "relaxes"},
		"study": {"studies", "studies"},
		"cry":   {"cries", "cries"},
		"fly":   {"flies", "flies"},
		"buy":   {"buys", "buys"},
		"dance": {"dances", "dances"},
	}
	for w, want := range spots {
		if got := gramThirdForm(w); got != want.gen {
			t.Errorf("三单生成器侧 %s→%s, want %s", w, got, want.gen)
		}
		if got := refThirdPerson(w); got != want.ref {
			t.Errorf("三单校验器侧 %s→%s, want %s", w, got, want.ref)
		}
	}
	for _, n := range v.NounList {
		if gramPluralForm(n) != refPlural(n) {
			t.Fatalf("复数双写分歧: %s → %q vs %q", n, gramPluralForm(n), refPlural(n))
		}
	}
	for _, vb := range v.VerbList {
		if gramThirdForm(vb) != refThirdPerson(vb) {
			t.Fatalf("三单双写分歧: %s → %q vs %q", vb, gramThirdForm(vb), refThirdPerson(vb))
		}
	}
	for _, n := range v.NounList {
		want := "a"
		if isVowelLetter(n[0]) {
			want = "an"
		}
		if got := refArticle(n); got != want {
			t.Fatalf("冠词重判分歧: %s → %q want %q", n, got, want)
		}
	}
	// 词表规则类覆盖完备性：+es/ies/元音+y 三类必须都有真实词入库（防词表退化）。
	classes := map[string]bool{"es": false, "ies": false, "vowelY": false, "s": false}
	for _, n := range v.NounList {
		switch {
		case strings.HasSuffix(n, "y") && !isVowelLetter(n[len(n)-2]):
			classes["ies"] = true
		case strings.HasSuffix(n, "y") && isVowelLetter(n[len(n)-2]):
			classes["vowelY"] = true
		case strings.HasSuffix(gramPluralForm(n), "es"):
			classes["es"] = true
		default:
			classes["s"] = true
		}
	}
	for c, ok := range classes {
		if !ok {
			t.Fatalf("词表复数规则类 %q 无覆盖（词表退化）", c)
		}
	}
}

// 全空间扫描：两母题全域「构造零失败 + 校验器全过 + content 摘要两两互异」，
// 且跨母题摘要零碰撞（H-W6-1 判定口径的机器断言）。
func TestWholeSpaceDistinctAndValid(t *testing.T) {
	if testing.Short() {
		t.Skip("short 模式跳过全空间扫描")
	}
	v := testVocab(t)
	spellV, _ := NewVocabSpellValidator(v)
	gramV, _ := NewGramSCValidator(v)
	gens, err := BuiltinGenerators(v)
	if err != nil {
		t.Fatal(err)
	}
	seenAll := map[string]string{}
	for _, g := range gens {
		validate := spellV.Validate
		if g.Entry().ID == TplGramSC {
			validate = gramV.Validate
		}
		seen := map[string]int{}
		for i := 0; i < g.Size(); i++ {
			inst, err := g.Instance(i)
			if err != nil {
				t.Fatalf("%s idx=%d 构造失败（全空间应零构造失败）: %v", g.Entry().ID, i, err)
			}
			if verr := validate(inst); verr != nil {
				t.Fatalf("%s idx=%d 校验器拒绝: %v", g.Entry().ID, i, verr)
			}
			d, derr := InstanceDigest(inst)
			if derr != nil {
				t.Fatalf("%s idx=%d 摘要失败: %v", g.Entry().ID, i, derr)
			}
			if first, dup := seen[d]; dup {
				t.Fatalf("%s 全空间摘要碰撞：idx %d 与 %d", g.Entry().ID, first, i)
			}
			if prevOwner, dup := seenAll[d]; dup {
				t.Fatalf("跨母题摘要碰撞：%s 与 %s", prevOwner, g.Entry().ID)
			}
			seen[d] = i
			seenAll[d] = g.Entry().ID
		}
		t.Logf("%s 全空间 %d 参数点：校验器全过、content 两两互异", g.Entry().ID, g.Size())
	}
	if len(seenAll) < 150 {
		t.Fatalf("全空间总量 %d 异常（应 ≥150）", len(seenAll))
	}
}
