package subjectlang

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"sync"
	"testing"

	"github.com/Cloudbird-Software/AI_Web_School/core/ai"
)

// ── 测试假件：fake Caller（core/ai fakeCaller 同构：记录请求、按脚本回放）──

// fakeReorgCaller 记录每次出站请求，回放预置 content 或错误——三类 draft
// （好/坏/畸形 JSON）都经它注入，无任何真实网络调用。
type fakeReorgCaller struct {
	mu      sync.Mutex
	reqs    []ai.OutboundRequest
	content string
	err     error
}

func (f *fakeReorgCaller) Call(_ context.Context, req ai.OutboundRequest) (ai.OutboundResult, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.reqs = append(f.reqs, req)
	if f.err != nil {
		return ai.OutboundResult{}, f.err
	}
	return ai.OutboundResult{Content: f.content}, nil
}

func (f *fakeReorgCaller) calls() int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return len(f.reqs)
}

var errReorgCallerDown = errors.New("fake caller: 供应商不可达")

// goodDraft 是词表内可解的基准 draft：句子「小山羊在山上吃草。」恰好含一次
// 「山羊」；三个干扰词全在词表、互异、≠答案、不在句中。
const goodDraftJSON = `{"sentence":"小山羊在山上吃草。","answer":"山羊","distractors":["白马","小鸟","跑步"],"explanation":"句子说的是小山羊吃草，空格处填「山羊」。"}`

func newReorgGen(t *testing.T, caller ai.Caller) *SentenceReorgGenerator {
	t.Helper()
	g, err := NewSentenceReorgGenerator(testCorpus(t), caller, "lang-draft-dev")
	if err != nil {
		t.Fatal(err)
	}
	return g
}

// 好路径：fake Caller 返回可解 draft → 成实例，且独立校验器重判全过。
func TestSentenceReorgGoodDraftBecomesInstance(t *testing.T) {
	fake := &fakeReorgCaller{content: goodDraftJSON}
	g := newReorgGen(t, fake)
	inst, err := g.Draft(context.Background(), "太阳", "L")
	if err != nil {
		t.Fatal(err)
	}
	if inst.TemplateID != "tpl-sl-sent-reorg-c" {
		t.Fatalf("template_id=%q", inst.TemplateID)
	}
	optsAny := inst.Content["options"].([]any)
	if len(optsAny) != 4 {
		t.Fatalf("四选项形态：%d", len(optsAny))
	}
	opts := make([]string, 4)
	for i, o := range optsAny {
		opts[i] = o.(string)
	}
	ans := inst.Content["answer"].(int)
	if ans < 1 || ans > 4 {
		t.Fatalf("答案位 %d 越界", ans)
	}
	if opts[ans-1] != "山羊" {
		t.Fatalf("答案选项 %q ≠ 挖空词", opts[ans-1])
	}
	distinct := map[string]bool{}
	for _, o := range opts {
		if distinct[o] {
			t.Fatal("选项重复")
		}
		distinct[o] = true
	}
	sentence := inst.Content["sentence"].(string)
	if !strings.Contains(sentence, "____") {
		t.Fatal("题干句应含挖空标记")
	}
	if strings.Contains(sentence, "山羊") {
		t.Fatal("挖空后句中不应残留答案词")
	}
	// 独立校验器重判（不读生成器状态）：从实例还原 draft 再 Check。
	v, err := NewSentenceReorgSolvability(testCorpus(t))
	if err != nil {
		t.Fatal(err)
	}
	if err := v.Check(&SentenceReorgDraft{
		Sentence:    strings.Replace(sentence, "____", "山羊", 1),
		Answer:      "山羊",
		Distractors: []string{"白马", "小鸟", "跑步"},
		Explanation: inst.Content["explanation"].(string),
	}); err != nil {
		t.Fatalf("独立校验器重判拒绝: %v", err)
	}
	// 出站信封：一次调用，入参进 prompt 信封（指令契约留在 BAML 侧）。
	if fake.calls() != 1 {
		t.Fatalf("出站次数 %d ≠ 1", fake.calls())
	}
	if got := fake.reqs[0].Prompt; !strings.Contains(got, "source_word: 太阳") || !strings.Contains(got, "gradeband: L") {
		t.Fatalf("出站信封缺结构化入参: %q", got)
	}
	// 同 draft 两次构造逐字节一致（确定性）。
	inst2, err := g.Draft(context.Background(), "太阳", "L")
	if err != nil {
		t.Fatal(err)
	}
	if string(mustJSON(inst.Content)) != string(mustJSON(inst2.Content)) {
		t.Fatal("同 draft 两次构造应逐字节一致")
	}
}

// 坏 draft：违反可解性契约的变体逐一被拒（errors.Is ErrUnsolvableDraft，无实例）。
func TestSentenceReorgRejectsBadDrafts(t *testing.T) {
	cases := map[string]SentenceReorgDraft{
		"挖空词表外":    {Sentence: "小山羊在山上吃草。", Answer: "恐龙", Distractors: []string{"白马", "小鸟", "跑步"}, Explanation: "x"},
		"句不含挖空词":   {Sentence: "小白马在山上跑步。", Answer: "山羊", Distractors: []string{"白马", "小鸟", "跑步"}, Explanation: "x"},
		"挖空词出现两次":  {Sentence: "小山羊和山羊在吃草。", Answer: "山羊", Distractors: []string{"白马", "小鸟", "跑步"}, Explanation: "x"},
		"干扰词表外":    {Sentence: "小山羊在山上吃草。", Answer: "山羊", Distractors: []string{"小狗", "小鸟", "跑步"}, Explanation: "x"},
		"干扰词等于答案":  {Sentence: "小山羊在山上吃草。", Answer: "山羊", Distractors: []string{"山羊", "小鸟", "跑步"}, Explanation: "x"},
		"干扰词重复":    {Sentence: "小山羊在山上吃草。", Answer: "山羊", Distractors: []string{"白马", "白马", "跑步"}, Explanation: "x"},
		"干扰词在句中多解": {Sentence: "小山羊和白马在吃草。", Answer: "山羊", Distractors: []string{"白马", "小鸟", "跑步"}, Explanation: "x"},
		"干扰词两个":    {Sentence: "小山羊在山上吃草。", Answer: "山羊", Distractors: []string{"白马", "小鸟"}, Explanation: "x"},
		"干扰词四个":    {Sentence: "小山羊在山上吃草。", Answer: "山羊", Distractors: []string{"白马", "小鸟", "跑步", "游泳"}, Explanation: "x"},
		"解析为空":     {Sentence: "小山羊在山上吃草。", Answer: "山羊", Distractors: []string{"白马", "小鸟", "跑步"}, Explanation: "  "},
		"句子为空":     {Sentence: "  ", Answer: "山羊", Distractors: []string{"白马", "小鸟", "跑步"}, Explanation: "x"},
	}
	for name, draft := range cases {
		t.Run(name, func(t *testing.T) {
			fake := &fakeReorgCaller{content: string(mustJSON(draft))}
			g := newReorgGen(t, fake)
			inst, err := g.Draft(context.Background(), "太阳", "L")
			if err == nil {
				t.Fatal("坏 draft 应被拒")
			}
			if !errors.Is(err, ErrUnsolvableDraft) {
				t.Fatalf("应判 draft 拒绝（ErrUnsolvableDraft），得: %v", err)
			}
			if inst != nil {
				t.Fatal("被拒 draft 不得产生实例（draft 永远是 draft）")
			}
		})
	}
}

// 畸形 JSON：非 JSON 文本、截断、数组形态、未知字段、尾随内容——全拒。
func TestSentenceReorgRejectsMalformedJSON(t *testing.T) {
	cases := map[string]string{
		"纯文本":     "我不会输出 JSON",
		"截断 JSON": `{"sentence":"小山羊在山上吃草。","answer":"山羊"`,
		"数组形态":    `["小山羊在山上吃草。","山羊"]`,
		"未知字段":    `{"sentence":"小山羊在山上吃草。","answer":"山羊","distractors":["白马","小鸟","跑步"],"explanation":"x","tier":"B"}`,
		"尾随内容":    goodDraftJSON + "\n另附说明：以上是好答案",
		"空串":      "",
	}
	for name, content := range cases {
		t.Run(name, func(t *testing.T) {
			fake := &fakeReorgCaller{content: content}
			g := newReorgGen(t, fake)
			inst, err := g.Draft(context.Background(), "太阳", "L")
			if err == nil {
				t.Fatal("畸形 JSON 应被拒")
			}
			if !errors.Is(err, ErrUnsolvableDraft) {
				t.Fatalf("畸形输出应按 draft 拒绝（ErrUnsolvableDraft），得: %v", err)
			}
			if inst != nil {
				t.Fatal("畸形 draft 不得产生实例")
			}
		})
	}
}

// 传输失败区别于 draft 拒绝：caller 错误不包 ErrUnsolvableDraft。
func TestSentenceReorgCallerErrorIsTransportFailure(t *testing.T) {
	fake := &fakeReorgCaller{err: errReorgCallerDown}
	g := newReorgGen(t, fake)
	inst, err := g.Draft(context.Background(), "太阳", "L")
	if err == nil {
		t.Fatal("caller 错误应上抛")
	}
	if errors.Is(err, ErrUnsolvableDraft) {
		t.Fatalf("传输失败不得误判为 draft 拒绝: %v", err)
	}
	if !strings.Contains(err.Error(), "出站失败") {
		t.Fatalf("应保留出站失败语义: %v", err)
	}
	if inst != nil {
		t.Fatal("失败调用不得产生实例")
	}
}

// 出站前预检：题材词不在词表 / gradeband 为空即拒绝，且不发起出站调用。
func TestSentenceReorgPreGateWithoutOutbound(t *testing.T) {
	fake := &fakeReorgCaller{content: goodDraftJSON}
	g := newReorgGen(t, fake)
	if _, err := g.Draft(context.Background(), "恐龙", "L"); err == nil {
		t.Fatal("表外题材词应预检拒绝")
	}
	if _, err := g.Draft(context.Background(), "太阳", "  "); err == nil {
		t.Fatal("空 gradeband 应拒绝")
	}
	if fake.calls() != 0 {
		t.Fatalf("预检拒绝不得出站，实际 %d 次", fake.calls())
	}
}

// 构造期 fail-closed：nil caller / 空 target / nil 语料 / 空词表语料即错。
func TestSentenceReorgConstructorFailClosed(t *testing.T) {
	c := testCorpus(t)
	if _, err := NewSentenceReorgGenerator(c, nil, "lang-draft-dev"); err == nil {
		t.Fatal("nil caller 应构造失败")
	}
	if _, err := NewSentenceReorgGenerator(c, &fakeReorgCaller{}, "  "); err == nil {
		t.Fatal("空 target 应构造失败")
	}
	if _, err := NewSentenceReorgGenerator(nil, &fakeReorgCaller{}, "lang-draft-dev"); err == nil {
		t.Fatal("nil 语料应构造失败")
	}
	if _, err := NewSentenceReorgGenerator(&Corpus{SourceID: "x", Words: map[string]bool{}}, &fakeReorgCaller{}, "t"); err == nil {
		t.Fatal("空词表应构造失败（判定域为零）")
	}
}

// 可解性校验器独立于 LLM 调用面：直接构造、纯数据入/错误出，零 Caller 参与。
func TestSentenceReorgSolvabilityStandalone(t *testing.T) {
	v, err := NewSentenceReorgSolvability(testCorpus(t))
	if err != nil {
		t.Fatal(err)
	}
	if err := v.Check(&SentenceReorgDraft{
		Sentence:    "小山羊在山上吃草。",
		Answer:      "山羊",
		Distractors: []string{"白马", "小鸟", "跑步"},
		Explanation: "句子说的是小山羊吃草。",
	}); err != nil {
		t.Fatalf("可解 draft 应过: %v", err)
	}
	if err := v.Check(nil); err == nil {
		t.Fatal("nil draft 应拒")
	}
	if err := v.Check(&SentenceReorgDraft{
		Sentence:    "小山羊在山上吃草。",
		Answer:      "山羊",
		Distractors: []string{"白马", "小鸟", "跑步"},
	}); err == nil {
		t.Fatal("缺解析应拒")
	}
	if _, err := NewSentenceReorgSolvability(nil); err == nil {
		t.Fatal("nil 语料应构造失败")
	}
}

// 并发 Draft（-race 面）：共享 fake Caller 多 goroutine 同跑，全过且互不串扰。
func TestSentenceReorgConcurrentDrafts(t *testing.T) {
	fake := &fakeReorgCaller{content: goodDraftJSON}
	g := newReorgGen(t, fake)
	var wg sync.WaitGroup
	errs := make([]error, 8)
	for i := 0; i < 8; i++ {
		wg.Add(1)
		go func(idx int) {
			defer wg.Done()
			inst, err := g.Draft(context.Background(), "太阳", "L")
			if err == nil && inst == nil {
				err = fmt.Errorf("idx=%d 实例为 nil", idx)
			}
			errs[idx] = err
		}(i)
	}
	wg.Wait()
	for i, err := range errs {
		if err != nil {
			t.Fatalf("goroutine %d: %v", i, err)
		}
	}
	if fake.calls() != 8 {
		t.Fatalf("出站次数 %d ≠ 8", fake.calls())
	}
}
