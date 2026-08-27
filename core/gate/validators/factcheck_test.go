package validators

import (
	"context"
	"encoding/json"
	"errors"
	"sync"
	"testing"
)

// 表驱动：判定表逐行钉死（验收 #1/#3）。判定面独立于任何生成逻辑——
// 用例内容全部手工构造字面量，事实引用手工登记，不存在「以生成逻辑
// 构造命中」的自证路径（A8/X11）。
func TestFactCheckValidatorVerdicts(t *testing.T) {
	tests := []struct {
		name      string
		content   string // 送检内容（JSON）；空串用 contentGo
		contentGo any
		claims    []FactClaim // 登记在内容摘要键下的事实引用
		want      Verdict
		wantConf  float64
		evKey     string // 必须存在的证据键（空串不检查）
	}{
		{
			name:    "干净语篇_零断言零事实_pass",
			content: `{"body":"春天来了，花儿开了。","genre":"narrative"}`,
			want:    VerdictPass, wantConf: 1.0, evKey: "checked_facts",
		},
		{
			name:    "数值断言与登记事实一致_pass",
			content: `{"body":"小明有 3 个苹果。"}`,
			claims:  []FactClaim{{Kind: FactKindNumber, Value: "3"}},
			want:    VerdictPass, wantConf: 1.0,
		},
		{
			name:    "日期断言与登记事实一致_双向归一_pass",
			content: `{"body":"1949-10-01，开国大典在北京举行。"}`,
			claims:  []FactClaim{{Kind: FactKindDate, Value: "1949年10月1日"}},
			want:    VerdictPass, wantConf: 1.0,
		},
		{
			name:    "粒度相容_正文粗于登记_pass",
			content: `{"body":"1949年，新中国成立了。"}`,
			claims:  []FactClaim{{Kind: FactKindDate, Value: "1949-10-01"}},
			want:    VerdictPass, wantConf: 1.0,
		},
		{
			name:    "日期跨度内数字不重复进数值面",
			content: `{"body":"1949年10月1日，典礼举行。"}`,
			claims:  []FactClaim{{Kind: FactKindDate, Value: "1949-10-01"}},
			want:    VerdictPass, wantConf: 1.0,
		},
		{
			name:    "数字矛盾_正文数值与登记事实冲突_fail",
			content: `{"body":"小明有 5 个苹果。"}`,
			claims:  []FactClaim{{Kind: FactKindNumber, Value: "3"}},
			want:    VerdictFail, wantConf: 1.0, evKey: "contradictions",
		},
		{
			name:    "日期矛盾_正文日期不在登记事实内_fail",
			content: `{"body":"1949年10月2日，典礼举行。"}`,
			claims:  []FactClaim{{Kind: FactKindDate, Value: "1949-10-01"}},
			want:    VerdictFail, wantConf: 1.0, evKey: "contradictions",
		},
		{
			name:    "无引用可对账_正文有数值但零登记_review",
			content: `{"body":"果园里种了 12 棵树。"}`,
			claims:  nil,
			want:    VerdictReview, wantConf: 1.0, evKey: "unreconciled",
		},
		{
			name:    "登记实体未在正文出现_review",
			content: `{"body":"远处的山峦在晨雾中若隐若现。"}`,
			claims:  []FactClaim{{Kind: FactKindEntity, Value: "长城"}},
			want:    VerdictReview, wantConf: 1.0, evKey: "missing_entities",
		},
		{
			name:    "语义事实而Judge未挂接_review不放行",
			content: `{"body":"太阳从东边升起。"}`,
			claims:  []FactClaim{{Kind: FactKindSemantic, Value: "太阳东升西落"}},
			want:    VerdictReview, wantConf: 1.0, evKey: "semantic_unjudged",
		},
		{
			name:      "内容根非结构化_fail_closed",
			contentGo: "纯文本语篇",
			want:      VerdictFail, wantConf: 1.0,
		},
		{
			name:      "空容器_fail_closed",
			contentGo: map[string]any{},
			want:      VerdictFail, wantConf: 1.0,
		},
		{
			name:    "body缺失_fail_closed",
			content: `{"genre":"narrative"}`,
			want:    VerdictFail, wantConf: 1.0,
		},
		{
			name:    "body空白_fail_closed",
			content: `{"body":"   "}`,
			want:    VerdictFail, wantConf: 1.0,
		},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			var content any = tt.contentGo
			if tt.content != "" {
				if err := json.Unmarshal([]byte(tt.content), &content); err != nil {
					t.Fatalf("fixture 解码失败: %v", err)
				}
			}
			src := NewMemoryFactSource()
			if len(tt.claims) > 0 {
				src.Register(digestOf(t, content), tt.claims...)
			}
			v := NewFactCheckValidator(src, nil)

			got := v.Validate(context.Background(), Candidate{ArtifactType: "passage", Content: content})
			if got.Verdict != tt.want {
				t.Fatalf("verdict = %s (%v), want %s", got.Verdict, got.Evidence, tt.want)
			}
			if got.Confidence != tt.wantConf {
				t.Fatalf("confidence = %v, want %v", got.Confidence, tt.wantConf)
			}
			if tt.evKey != "" {
				if _, ok := got.Evidence[tt.evKey]; !ok {
					t.Fatalf("证据缺键 %q: %v", tt.evKey, got.Evidence)
				}
			}
			if got.Validator != FactCheckValidatorID || got.Version != FactCheckValidatorVersion {
				t.Fatalf("验证器身份漂移: %+v", got)
			}
		})
	}
}

// artifact_type 为空 fail-closed（与查重验证器同一纪律）。
func TestFactCheckValidatorEmptyArtifactTypeFailsClosed(t *testing.T) {
	v := NewFactCheckValidator(NewMemoryFactSource(), nil)
	got := v.Validate(context.Background(), Candidate{
		ArtifactType: " ", Content: map[string]any{"body": "正文"},
	})
	if got.Verdict != VerdictFail {
		t.Fatalf("空 artifact_type 必须 fail-closed: %+v", got)
	}
}

// 不放行语义（#79 review-not-pass）：登记源未挂接 / 查询失败 / 事实集合非法
// 一律 review 置信 0，绝不伪造 pass。
func TestFactCheckValidatorFailsClosedOnUnknownSourceState(t *testing.T) {
	clean := map[string]any{"body": "春天来了，花儿开了。"}
	failing := FactSourceFunc(func(_ context.Context, _ string) ([]FactClaim, error) {
		return nil, errors.New("db down")
	})
	illegal := FactSourceFunc(func(_ context.Context, _ string) ([]FactClaim, error) {
		return []FactClaim{{Kind: FactKindNumber, Value: "abc"}}, nil
	})
	tests := []struct {
		name string
		src  FactSource
		ev   string
	}{
		{name: "未挂接源", src: nil, ev: "reason"},
		{name: "源查询失败", src: failing, ev: "source_error"},
		{name: "事实集合非法", src: illegal, ev: "claim_error"},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			v := NewFactCheckValidator(tt.src, nil)
			got := v.Validate(context.Background(), Candidate{ArtifactType: "passage", Content: clean})
			if got.Verdict != VerdictReview || got.Confidence != 0 {
				t.Fatalf("%s: 必须 review 且置信 0，实得 %+v", tt.name, got)
			}
			if _, ok := got.Evidence[tt.ev]; !ok {
				t.Fatalf("证据缺键 %q: %v", tt.ev, got.Evidence)
			}
		})
	}
}

// 语义判定面（Judge 注入点）：三值与故障路径逐条钉死；判定面只收语义子集，
// 且只对登记的语义事实被调用。
func TestFactCheckJudgeSurfaces(t *testing.T) {
	bodyPassage := map[string]any{"body": "太阳从东边升起。"}
	claims := []FactClaim{{Kind: FactKindSemantic, Value: "太阳东升西落"}}

	var gotClaims []FactClaim
	judge := FactJudgeFunc(func(_ context.Context, body string, claims []FactClaim) (Verdict, float64, error) {
		if body != "太阳从东边升起。" {
			t.Errorf("Judge 收到错误正文: %q", body)
		}
		gotClaims = claims
		return VerdictPass, 0.9, nil
	})

	src := NewMemoryFactSource()
	src.Register(digestOf(t, bodyPassage), claims...)
	v := NewFactCheckValidator(src, judge)
	got := v.Validate(context.Background(), Candidate{ArtifactType: "passage", Content: bodyPassage})
	if got.Verdict != VerdictPass || got.Confidence != 0.9 {
		t.Fatalf("语义一致应 pass 且采信判定面置信: %+v", got)
	}
	if len(gotClaims) != 1 || gotClaims[0].Kind != FactKindSemantic {
		t.Fatalf("Judge 只应收到语义子集: %+v", gotClaims)
	}

	tests := []struct {
		name  string
		judge FactJudge
		want  Verdict
		conf  float64
	}{
		{
			name:  "判定fail_硬错误阻断",
			judge: FactJudgeFunc(func(context.Context, string, []FactClaim) (Verdict, float64, error) { return VerdictFail, 0.8, nil }),
			want:  VerdictFail, conf: 0.8,
		},
		{
			name:  "判定review_转人工",
			judge: FactJudgeFunc(func(context.Context, string, []FactClaim) (Verdict, float64, error) { return VerdictReview, 0.5, nil }),
			want:  VerdictReview, conf: 0.5,
		},
		{
			name: "判定故障_review置信0",
			judge: FactJudgeFunc(func(context.Context, string, []FactClaim) (Verdict, float64, error) {
				return VerdictPass, 1, errors.New("llm down")
			}),
			want: VerdictReview, conf: 0,
		},
		{
			name:  "未知verdict_review置信0",
			judge: FactJudgeFunc(func(context.Context, string, []FactClaim) (Verdict, float64, error) { return "maybe", 1, nil }),
			want:  VerdictReview, conf: 0,
		},
		{
			name:  "越界置信_判定面故障级_review不放行",
			judge: FactJudgeFunc(func(context.Context, string, []FactClaim) (Verdict, float64, error) { return VerdictPass, 1.5, nil }),
			want:  VerdictReview, conf: 0,
		},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			src := NewMemoryFactSource()
			src.Register(digestOf(t, bodyPassage), claims...)
			v := NewFactCheckValidator(src, tt.judge)
			got := v.Validate(context.Background(), Candidate{ArtifactType: "passage", Content: bodyPassage})
			if got.Verdict != tt.want || got.Confidence != tt.conf {
				t.Fatalf("verdict/conf = %s/%v, want %s/%v (%v)", got.Verdict, got.Confidence, tt.want, tt.conf, got.Evidence)
			}
		})
	}
}

// 硬矛盾短路语义判定面：确定性面已可定谳时不烧 Judge（廉价先行）。
func TestFactCheckContradictionShortCircuitsJudge(t *testing.T) {
	called := false
	judge := FactJudgeFunc(func(context.Context, string, []FactClaim) (Verdict, float64, error) {
		called = true
		return VerdictPass, 1, nil
	})
	content := map[string]any{"body": "小明有 5 个苹果。"}
	src := NewMemoryFactSource()
	src.Register(digestOf(t, content),
		FactClaim{Kind: FactKindNumber, Value: "3"},
		FactClaim{Kind: FactKindSemantic, Value: "苹果是水果"})
	v := NewFactCheckValidator(src, judge)
	got := v.Validate(context.Background(), Candidate{ArtifactType: "passage", Content: content})
	if got.Verdict != VerdictFail || called {
		t.Fatalf("硬矛盾必须 fail 且短路 Judge: %+v called=%v", got, called)
	}
}

// 并发安全：事实登记与判定并发交错，结果恒为合法三值且无数据竞争（-race）。
func TestFactCheckValidatorConcurrentRegisterAndValidate(t *testing.T) {
	src := NewMemoryFactSource()
	v := NewFactCheckValidator(src, FactJudgeFunc(
		func(context.Context, string, []FactClaim) (Verdict, float64, error) { return VerdictReview, 0.5, nil }))

	const n = 64
	var wg sync.WaitGroup
	for i := 0; i < n; i++ {
		wg.Add(2)
		go func(i int) {
			defer wg.Done()
			content := map[string]any{"body": "第 3 组实验记录。"}
			src.Register(digestOf(t, content), FactClaim{Kind: FactKindNumber, Value: "3"})
		}(i)
		go func(i int) {
			defer wg.Done()
			content := map[string]any{"body": "第 3 组实验记录。", "n": i % 8}
			r := v.Validate(context.Background(), Candidate{ArtifactType: "passage", Content: content})
			if r.Verdict != VerdictPass && r.Verdict != VerdictFail && r.Verdict != VerdictReview {
				t.Errorf("非法 verdict: %q", r.Verdict)
			}
			if r.Confidence < 0 || r.Confidence > 1 {
				t.Errorf("置信越界: %v", r.Confidence)
			}
		}(i)
	}
	wg.Wait()
}
