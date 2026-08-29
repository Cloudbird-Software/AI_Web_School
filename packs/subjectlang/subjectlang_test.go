package subjectlang

import (
	"strings"
	"testing"
)

func testCorpus(t *testing.T) *Corpus {
	t.Helper()
	c, err := LoadCorpus("../../content/sources/corpus/manifest.yaml", "demo-common-chars-v1")
	if err != nil {
		t.Fatalf("语料装载: %v", err)
	}
	return c
}

// 语料装载：字/词表非空、许可在案、来源可寻。
func TestLoadCorpus(t *testing.T) {
	c := testCorpus(t)
	if len(c.CharList) < 50 || len(c.WordList) < 30 {
		t.Fatalf("样例语料规模不足：chars=%d words=%d", len(c.CharList), len(c.WordList))
	}
	if !c.Chars["山"] || !c.Words["太阳"] {
		t.Fatal("样例字/词表缺基准条目")
	}
	if c.SourceID != "demo-common-chars-v1" {
		t.Fatalf("source_id=%q", c.SourceID)
	}
}

// char_in_corpus / word_in_vocab：在表 pass、不在表 fail、构造期 fail-closed。
func TestCharInCorpus(t *testing.T) {
	c := testCorpus(t)
	v, err := NewCharInCorpus(c)
	if err != nil {
		t.Fatal(err)
	}
	if !v.InCorpus("山") {
		t.Fatal("表内字应 pass")
	}
	if v.InCorpus("饕") {
		t.Fatal("表外字应 fail")
	}
	if v.InCorpus("") || v.InCorpus("太阳") {
		t.Fatal("空串/多字应 fail（判定域=单字）")
	}
	if _, err := NewCharInCorpus(nil); err == nil {
		t.Fatal("nil 语料应构造失败（fail-closed）")
	}
	w, err := NewWordInVocab(c)
	if err != nil {
		t.Fatal(err)
	}
	if !w.InVocab("太阳") || w.InVocab(" 太阳 ") == false {
		t.Fatal("词表判定（含 trim）应一致")
	}
	if w.InVocab("不存在的词") {
		t.Fatal("表外词应 fail")
	}
}

// 生成器：空间规模、索引界、纯函数性（同 index 同输出）。
func TestCharRecognizeGenerator(t *testing.T) {
	c := testCorpus(t)
	g, err := newCharRecognizeGen(c)
	if err != nil {
		t.Fatal(err)
	}
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
	if ja, jb := string(mustJSON(a.Content)), string(mustJSON(b.Content)); ja != jb {
		t.Fatal("同 index 两次构造应逐字节一致（纯函数）")
	}
	// 结构健全性：四选项、答案在 1..4、答案选项==目标字
	if len(a.Content["options"].([]any)) != 4 {
		t.Fatal("四选项形态")
	}
	ans := a.Content["answer"].(int)
	optsAny := a.Content["options"].([]any)
	opts := make([]string, len(optsAny))
	for i2, o := range optsAny {
		opts[i2] = o.(string)
	}
	target := a.Content["target"].(string)
	if ans < 1 || ans > 4 {
		t.Fatalf("答案位 %d 越界", ans)
	}
	if opts[ans-1] != target {
		t.Fatalf("答案选项 %q != 目标 %q", opts[ans-1], target)
	}
	// 独立校验器重判：答案字必须在语料字表内（validator 不读生成器内部状态）
	v, _ := NewCharInCorpus(c)
	if !v.InCorpus(opts[ans-1]) {
		t.Fatal("答案字应过 char_in_corpus")
	}
	distinct := map[string]bool{}
	for _, o := range opts {
		if distinct[o] {
			t.Fatal("选项重复（干扰项去重失效）")
		}
		distinct[o] = true
	}
}

// 生成器×校验器独立重判：渲染串重提选项（不复用生成器状态）全量过校验。
func TestBatchInstancesAllValidAndDistinct(t *testing.T) {
	c := testCorpus(t)
	g, _ := newCharRecognizeGen(c)
	v, _ := NewCharInCorpus(c)
	seen := map[string]bool{}
	n := 60
	for i := 0; i < n; i++ {
		inst, err := g.Instance(i)
		if err != nil {
			t.Fatalf("Instance(%d): %v", i, err)
		}
		optsAny := inst.Content["options"].([]any)
		opts := make([]string, len(optsAny))
		for i2, o := range optsAny {
			opts[i2] = o.(string)
		}
		ans := inst.Content["answer"].(int)
		if !v.InCorpus(opts[ans-1]) {
			t.Fatalf("i=%d 答案字不在语料（校验器应拒）", i)
		}
		key := inst.Content["stem"].(string) + "|" + strings.Join(opts, "")
		if seen[key] {
			t.Fatalf("i=%d 与先前实例重复（结构互异破坏）", i)
		}
		seen[key] = true
	}
}
