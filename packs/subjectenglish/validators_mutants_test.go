package subjectenglish

import (
	"errors"
	"fmt"
	"strings"
	"testing"
)

// validators_mutants_test.go —— 防共谋与负例组（subjectmath 同口径）：
//   - 每母题 ≥10 条故意坏实例（手工拼装，绕开生成器），且必须落在预期的
//     哨兵类别上——生成器与校验器同向同错会在这组立刻暴露。

// mutClass 断言 err 落在预期哨兵并累计负例计数。
func mutClass(t *testing.T, n *int, err error, want error, name string) {
	t.Helper()
	*n++
	if err == nil {
		t.Errorf("[m%02d %s] 坏实例未被拒", *n, name)
		return
	}
	if !errors.Is(err, want) {
		t.Errorf("[m%02d %s] 错误类别不符: got %v want %v", *n, name, err, want)
	}
}

// handSpell 手工拼装拼写实例（默认全部合法：apple/苹果/5/a）。
func handSpell(tplID, stem, answer, scorerAnswer, scorerID, interactionID string) *Instance {
	return &Instance{
		TemplateID: tplID,
		Locale:     "en",
		InteractionRef: map[string]any{
			"interaction_id":     interactionID,
			"interaction_params": map[string]any{"blank_ids": []any{"b1"}},
		},
		Content: map[string]any{
			"stem":         stem,
			"answer":       answer,
			"gloss":        "苹果",
			"letter_count": len(answer),
			"initial":      "a",
			"source_id":    "eng-basic-vocab-v1",
		},
		ScoringRef: map[string]any{
			"scorer_id":     scorerID,
			"scorer_params": map[string]any{"answer": scorerAnswer, "blank_id": "b1"},
		},
	}
}

func spellStem(gloss string, count int, initial string) string {
	return fmt.Sprintf("拼写：表示「%s」的英语单词（%d 个字母，以 %s 开头）：%s＿＿＿＿", gloss, count, initial, initial)
}

// 词汇拼写母题：13 条负例。
func TestVocabSpellMutants(t *testing.T) {
	v := testVocab(t)
	vld, err := NewVocabSpellValidator(v)
	if err != nil {
		t.Fatal(err)
	}
	n := 0
	okStem := spellStem("苹果", 5, "a")
	// 1 答案不在词表（词尾变形）
	mutClass(t, &n, vld.Validate(handSpell(TplVocabSpell, okStem, "applez", "applez", "exact_match", "text_blank")), ErrSpellNotInVocab, "表外词")
	// 2 答案截断
	mutClass(t, &n, vld.Validate(handSpell(TplVocabSpell, okStem, "appl", "appl", "exact_match", "text_blank")), ErrSpellNotInVocab, "截断词")
	// 3 答案大写（词表判定域大小写敏感）
	mutClass(t, &n, vld.Validate(handSpell(TplVocabSpell, okStem, "Apple", "Apple", "exact_match", "text_blank")), ErrSpellNotInVocab, "大写词")
	// 4 答案是表内另一个词（释义不符）
	mutClass(t, &n, vld.Validate(handSpell(TplVocabSpell, okStem, "dog", "dog", "exact_match", "text_blank")), ErrSpellGlossMismatch, "表内错词")
	// 5 题干释义换成另一词的释义
	mutClass(t, &n, vld.Validate(handSpell(TplVocabSpell, spellStem("狗", 5, "a"), "apple", "apple", "exact_match", "text_blank")), ErrSpellGlossMismatch, "释义错")
	// 6 首字母提示错
	mutClass(t, &n, vld.Validate(handSpell(TplVocabSpell, spellStem("苹果", 5, "b"), "apple", "apple", "exact_match", "text_blank")), ErrSpellInitialMis, "首字母错")
	// 7 字数提示错
	mutClass(t, &n, vld.Validate(handSpell(TplVocabSpell, spellStem("苹果", 9, "a"), "apple", "apple", "exact_match", "text_blank")), ErrSpellCountMis, "字数错")
	// 8 题干缺「」释义槽位
	mutClass(t, &n, vld.Validate(handSpell(TplVocabSpell, "拼写：苹果的英语（5 个字母，以 a 开头）：a＿＿＿＿", "apple", "apple", "exact_match", "text_blank")), ErrSpellStemMalformed, "缺释义槽位")
	// 9 字数槽位非整数
	mutClass(t, &n, vld.Validate(handSpell(TplVocabSpell, "拼写：表示「苹果」的英语单词（五个字母，以 a 开头）：a＿＿＿＿", "apple", "apple", "exact_match", "text_blank")), ErrSpellStemMalformed, "字数非整数")
	// 10 content 答案与 scoring 答案不一致
	mutClass(t, &n, vld.Validate(handSpell(TplVocabSpell, okStem, "apple", "pear", "exact_match", "text_blank")), ErrAnswerCrossMisalign, "跨域答案错位")
	// 11 评分器非 exact_match
	mutClass(t, &n, vld.Validate(handSpell(TplVocabSpell, okStem, "apple", "apple", "regex_match", "text_blank")), ErrScorerUnsupported, "scorer 错")
	// 12 交互形态不符
	mutClass(t, &n, vld.Validate(handSpell(TplVocabSpell, okStem, "apple", "apple", "exact_match", "single_choice")), ErrInteractionUnsupp, "interaction 错")
	// 13 母题 id 不符
	mutClass(t, &n, vld.Validate(handSpell(TplGramSC, okStem, "apple", "apple", "exact_match", "text_blank")), ErrWrongTemplate, "模板 id 错")
	// nil 实例
	if err := vld.Validate(nil); err == nil {
		t.Error("nil 实例应拒")
	}
	if n < 10 {
		t.Fatalf("拼写负例数 %d < 10", n)
	}
}

// handGram 手工拼装语法单选实例（opts 直传，answer/scorerAnswer 显式；
// answer_form 仅在 ans 在界内时回填——负例允许越界 ans）。
func handGram(tplID, family, word, stem string, opts []string, ans, scorerAns int, scorerID, interactionID string) *Instance {
	form := ""
	if ans >= 1 && ans <= len(opts) {
		form = opts[ans-1]
	}
	return &Instance{
		TemplateID: tplID,
		Locale:     "en",
		InteractionRef: map[string]any{
			"interaction_id":     interactionID,
			"interaction_params": map[string]any{"options": toAnySlice(opts)},
		},
		Content: map[string]any{
			"stem":        stem,
			"options":     toAnySlice(opts),
			"answer":      ans,
			"answer_form": form,
			"family":      family,
			"word":        word,
			"source_id":   "eng-basic-vocab-v1",
		},
		ScoringRef: map[string]any{
			"scorer_id":     scorerID,
			"scorer_params": map[string]any{"answer": scorerAns},
		},
	}
}

// 语法单选母题：15 条负例（覆盖三家族）。
func TestGramSCMutants(t *testing.T) {
	v := testVocab(t)
	vld, err := NewGramSCValidator(v)
	if err != nil {
		t.Fatal(err)
	}
	n := 0
	pluralStem := "Choose the correct plural form: one book, two ____."
	okPlural := []string{"book", "books", "bookes", "bookies"} // 正确项 books 在位 2
	thirdStem := "Choose the correct verb form: He ____ (catch) every morning."
	okThird := []string{"catches", "catchs", "catchies", "catch"} // 正确项 catches 在位 1
	artStem := "Fill in the blank: I have ___ apple."
	okArticle := []string{"a", "an"} // 正确项 an 在位 2
	// 1 标注位指向干扰项（复数）
	mutClass(t, &n, vld.Validate(handGram(TplGramSC, "plural", "book", pluralStem, okPlural, 1, 1, "exact_match", "single_choice")), ErrGramAnswerMismatch, "复数答案位错")
	// 2 答案位 0 越界
	mutClass(t, &n, vld.Validate(handGram(TplGramSC, "plural", "book", pluralStem, okPlural, 0, 0, "exact_match", "single_choice")), ErrGramAnswerMismatch, "答案位 0")
	// 3 答案位超上界
	mutClass(t, &n, vld.Validate(handGram(TplGramSC, "plural", "book", pluralStem, okPlural, 9, 9, "exact_match", "single_choice")), ErrGramAnswerMismatch, "答案位 9")
	// 4 选项缺规则重判正确项
	mutClass(t, &n, vld.Validate(handGram(TplGramSC, "plural", "book", pluralStem, []string{"book", "bookss", "bookes", "bookies"}, 2, 2, "exact_match", "single_choice")), ErrGramOptionsInvalid, "缺正确项")
	// 5 选项重复
	mutClass(t, &n, vld.Validate(handGram(TplGramSC, "plural", "book", pluralStem, []string{"books", "books", "bookes", "book"}, 1, 1, "exact_match", "single_choice")), ErrGramOptionsInvalid, "选项重复")
	// 6 选项数量 3
	mutClass(t, &n, vld.Validate(handGram(TplGramSC, "plural", "book", pluralStem, []string{"books", "bookes", "book"}, 1, 1, "exact_match", "single_choice")), ErrGramOptionsInvalid, "三选项")
	// 7 冠词题给了 4 选项
	mutClass(t, &n, vld.Validate(handGram(TplGramSC, "article", "apple", artStem, []string{"a", "an", "the", "some"}, 2, 2, "exact_match", "single_choice")), ErrGramOptionsInvalid, "冠词四选项")
	// 8 冠词答案位错（an 在位 2 标成 1）
	mutClass(t, &n, vld.Validate(handGram(TplGramSC, "article", "apple", artStem, okArticle, 1, 1, "exact_match", "single_choice")), ErrGramAnswerMismatch, "冠词答案位错")
	// 9 题干不含题词
	mutClass(t, &n, vld.Validate(handGram(TplGramSC, "plural", "book", "Choose the correct plural form: one pen, two ____.", okPlural, 2, 2, "exact_match", "single_choice")), ErrGramStemMalformed, "题干错词")
	// 10 家族不在白名单
	mutClass(t, &n, vld.Validate(handGram(TplGramSC, "dative", "book", pluralStem, okPlural, 2, 2, "exact_match", "single_choice")), ErrGramFamilyUnknown, "未知家族")
	// 11 三单家族给了名词
	mutClass(t, &n, vld.Validate(handGram(TplGramSC, "third_person", "cat", thirdStem, okThird, 1, 1, "exact_match", "single_choice")), ErrGramPosMismatch, "名词冒充动词")
	// 12 复数家族给了动词
	mutClass(t, &n, vld.Validate(handGram(TplGramSC, "plural", "run", pluralStem, okPlural, 2, 2, "exact_match", "single_choice")), ErrGramPosMismatch, "动词冒充名词")
	// 13 三单答案位错（catches 在位 1 标成 2）
	mutClass(t, &n, vld.Validate(handGram(TplGramSC, "third_person", "catch", thirdStem, okThird, 2, 2, "exact_match", "single_choice")), ErrGramAnswerMismatch, "三单答案位错")
	// 14 content 答案与 scoring 答案不一致
	mutClass(t, &n, vld.Validate(handGram(TplGramSC, "plural", "book", pluralStem, okPlural, 2, 1, "exact_match", "single_choice")), ErrAnswerCrossMisalign, "跨域答案错位")
	// 15 评分器/交互/模板形态错
	mutClass(t, &n, vld.Validate(handGram(TplGramSC, "plural", "book", pluralStem, okPlural, 2, 2, "math_equivalence", "single_choice")), ErrScorerUnsupported, "scorer 错")
	mutClass(t, &n, vld.Validate(handGram(TplGramSC, "plural", "book", pluralStem, okPlural, 2, 2, "exact_match", "text_blank")), ErrInteractionUnsupp, "interaction 错")
	mutClass(t, &n, vld.Validate(handGram(TplVocabSpell, "plural", "book", pluralStem, okPlural, 2, 2, "exact_match", "single_choice")), ErrWrongTemplate, "模板 id 错")
	if err := vld.Validate(nil); err == nil {
		t.Error("nil 实例应拒")
	}
	if n < 10 {
		t.Fatalf("语法负例数 %d < 10", n)
	}
}

// 正例地面真值：手写题干/答案直接喂校验器（不经过生成器），钉死规则判定面。
func TestGramGroundTruthPositive(t *testing.T) {
	v := testVocab(t)
	vld, err := NewGramSCValidator(v)
	if err != nil {
		t.Fatal(err)
	}
	cases := []struct {
		family, word, stem string
		opts               []string
		ans                int
	}{
		{"article", "umbrella", "Fill in the blank: I have ___ umbrella.", []string{"an", "a"}, 1},
		{"article", "book", "Fill in the blank: I have ___ book.", []string{"a", "an"}, 1},
		{"third_person", "study", "Choose the correct verb form: He ____ (study) every morning.", []string{"study", "studys", "studies", "studyes"}, 3},
		{"third_person", "go", "Choose the correct verb form: He ____ (go) every morning.", []string{"goes", "gos", "goies", "go"}, 1},
		{"plural", "city", "Choose the correct plural form: one city, two ____.", []string{"citys", "cityes", "cities", "city"}, 3},
		{"plural", "day", "Choose the correct plural form: one day, two ____.", []string{"days", "dayes", "daies", "day"}, 1},
	}
	for i, c := range cases {
		if err := vld.Validate(handGram(TplGramSC, c.family, c.word, c.stem, c.opts, c.ans, c.ans, "exact_match", "single_choice")); err != nil {
			t.Errorf("地面真值 #%d (%s/%s) 应过: %v", i, c.family, c.word, err)
		}
	}
	// 拼写正例：手写题干直接喂校验器。
	sv, _ := NewVocabSpellValidator(v)
	stem := spellStem("雨伞", 8, "u")
	if err := sv.Validate(handSpell(TplVocabSpell, stem, "umbrella", "umbrella", "exact_match", "text_blank")); err != nil {
		t.Errorf("拼写地面真值应过: %v", err)
	}
	if !strings.Contains(stem, "雨伞") {
		t.Fatal("sanity")
	}
}
