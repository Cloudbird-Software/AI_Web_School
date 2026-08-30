package bamlai

import (
	"context"
	"encoding/json"
	"errors"
	"strings"
	"testing"

	"github.com/Cloudbird-Software/AI_Web_School/baml_client"
	"github.com/Cloudbird-Software/AI_Web_School/baml_client/types"
	"github.com/Cloudbird-Software/AI_Web_School/core/ai"
	"github.com/Cloudbird-Software/AI_Web_School/packs/subjectlang"
)

// fakeFn 构造不触网的 BAML 函数替身（签名对齐 SentenceReorgFunc）.
func fakeFn(t *testing.T, out types.SentenceReorg, rec *string) SentenceReorgFunc {
	t.Helper()
	return func(_ context.Context, sourceWord, gradeband string, _ ...baml_client.CallOptionFunc) (types.SentenceReorg, error) {
		if rec != nil {
			*rec = sourceWord + "|" + gradeband
		}
		return out, nil
	}
}

func TestCallDraftJSONShape(t *testing.T) {
	t.Parallel()
	caller := SentenceReorgCaller{Fn: fakeFn(t, types.SentenceReorg{
		Sentence:    "我爱吃苹果。",
		Answer:      "苹果",
		Distractors: []string{"香蕉", "橘子", "葡萄"},
		Explanation: "挖空词与吃搭配。",
	}, nil)}
	out, err := caller.Call(context.Background(), ai.OutboundRequest{
		Target: "t",
		Prompt: "task: lang_sentence_reorg\nsource_word: 苹果\ngradeband: L",
	})
	if err != nil {
		t.Fatalf("Call: %v", err)
	}
	// 与 packs 侧严格解析器逐字段互验（DisallowUnknownFields + 拒尾随形态）——
	// BAML 输出契约与 pack 契约的字段漂移在本包测试期即红，不等真实出站.
	dec := json.NewDecoder(strings.NewReader(out.Content))
	dec.DisallowUnknownFields()
	var d subjectlang.SentenceReorgDraft
	if err := dec.Decode(&d); err != nil {
		t.Fatalf("packs 解析器拒收适配器输出: %v", err)
	}
	var extra map[string]any
	if err := json.NewDecoder(strings.NewReader(out.Content)).Decode(&extra); err != nil {
		t.Fatalf("输出必须是单份 JSON 对象: %v", err)
	}
	if len(extra) != 4 {
		t.Fatalf("输出键数 = %d, want 4（契约面止于四字段）", len(extra))
	}
	if d.Sentence != "我爱吃苹果。" || d.Answer != "苹果" || len(d.Distractors) != 3 || d.Explanation == "" {
		t.Fatalf("字段回读不符: %+v", d)
	}
}

func TestCallPassesUnpackedArgs(t *testing.T) {
	t.Parallel()
	var got string
	caller := SentenceReorgCaller{Fn: fakeFn(t, types.SentenceReorg{Distractors: []string{"a", "b", "c"}}, &got)}
	if _, err := caller.Call(context.Background(), ai.OutboundRequest{
		Target: "t",
		Prompt: "task: lang_sentence_reorg\nsource_word: 太阳\ngradeband: L",
	}); err != nil {
		t.Fatalf("Call: %v", err)
	}
	if got != "太阳|L" {
		t.Fatalf("BAML 入参解包错误: %q", got)
	}
}

func TestCallRejectsMalformedEnvelopeBeforeOutbound(t *testing.T) {
	t.Parallel()
	invoked := false
	caller := SentenceReorgCaller{Fn: func(_ context.Context, _, _ string, _ ...baml_client.CallOptionFunc) (types.SentenceReorg, error) {
		invoked = true
		return types.SentenceReorg{}, nil
	}}
	_, err := caller.Call(context.Background(), ai.OutboundRequest{Target: "t", Prompt: "请生成一道句子重组题"})
	if err == nil {
		t.Fatal("非法信封必须出站前拒绝")
	}
	if invoked {
		t.Fatal("非法信封不得触达 BAML 函数")
	}
	if strings.Contains(err.Error(), "请生成一道句子重组题") {
		t.Fatal("错误文本不得回显 prompt 原文（X3/D7）")
	}
}

func TestCallFnErrorPassthrough(t *testing.T) {
	t.Parallel()
	sentinel := errors.New("upstream down")
	caller := SentenceReorgCaller{Fn: func(_ context.Context, _, _ string, _ ...baml_client.CallOptionFunc) (types.SentenceReorg, error) {
		return types.SentenceReorg{}, sentinel
	}}
	_, err := caller.Call(context.Background(), ai.OutboundRequest{
		Target: "t",
		Prompt: "task: lang_sentence_reorg\nsource_word: 太阳\ngradeband: L",
	})
	if !errors.Is(err, sentinel) {
		t.Fatalf("上游错误必须原样透传: %v", err)
	}
}

func TestCallNilFnFailClosed(t *testing.T) {
	t.Parallel()
	caller := SentenceReorgCaller{}
	_, err := caller.Call(context.Background(), ai.OutboundRequest{
		Target: "t",
		Prompt: "task: lang_sentence_reorg\nsource_word: 太阳\ngradeband: L",
	})
	if err == nil {
		t.Fatal("Fn 未注入必须拒绝")
	}
}

func TestEnvelopeParse(t *testing.T) {
	t.Parallel()
	sw, gb, err := ParseSentenceReorgRequest("task: lang_sentence_reorg\nsource_word: 太阳\ngradeband: L")
	if err != nil {
		t.Fatalf("合法信封被拒: %v", err)
	}
	if sw != "太阳" || gb != "L" {
		t.Fatalf("解包错误: %q %q", sw, gb)
	}
}

func TestEnvelopeParseFailClosed(t *testing.T) {
	t.Parallel()
	cases := map[string]string{
		"task不符": "task: other_task\nsource_word: 太阳\ngradeband: L",
		"缺键":     "task: lang_sentence_reorg\nsource_word: 太阳",
		"重复键":    "task: lang_sentence_reorg\nsource_word: 太阳\nsource_word: 月亮\ngradeband: L",
		"未知键":    "task: lang_sentence_reorg\nsource_word: 太阳\ngradeband: L\nfoo: bar",
		"空值":     "task: lang_sentence_reorg\nsource_word: \ngradeband: L",
		"无冒号行":   "task: lang_sentence_reorg\nsource_word 太阳\ngradeband: L",
		"空信封":    "",
	}
	for name, prompt := range cases {
		if _, _, err := ParseSentenceReorgRequest(prompt); err == nil {
			t.Errorf("%s: 必须拒绝", name)
		}
	}
}
