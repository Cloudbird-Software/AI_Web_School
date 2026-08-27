package registry

import (
	"context"
	"errors"
	"math"
	"strings"
	"testing"
)

// 本套件承载 T-W5-016 验收 #2 的注册期半边：评分器条目加载时校验契约面，
// 不合规条目注册即失败（fail-loud）；输出 verdict 形态在执行期由 ValidateResult
// 拦截。ADR-0005 对齐断言在 TestADR0005ArbitrationAlignment。

// fakeScorer 是可编程契约面的最小评分器替身（stub 生效面只在本套件）.
type fakeScorer struct {
	entry Entry
	spec  ScorerSpec
}

func (f fakeScorer) Entry() Entry { return f.entry }
func (f fakeScorer) Score(_ context.Context, _ string, _ map[string]any) (ScoreResult, error) {
	return ScoreResult{}, nil
}
func (f fakeScorer) ScorerContract() ScorerSpec { return f.spec }

// bareScorer 无契约声明面（不实现 Contracted）——装配门必须拒收的形态.
type bareScorer struct{ entry Entry }

func (b bareScorer) Entry() Entry { return b.entry }
func (b bareScorer) Score(_ context.Context, _ string, _ map[string]any) (ScoreResult, error) {
	return ScoreResult{}, nil
}

// exactMatchSpec 是 exact_match 形态的确定性契约面（scorer.yaml
// params_schema.required=[answer] 的 Go 投影）.
func exactMatchSpec() ScorerSpec {
	return ScorerSpec{
		Entry:         Entry{ID: "exact_match", Version: "1.0.0+platform"},
		InputSchema:   map[string]ParamKind{"answer": KindObject},
		Deterministic: true,
	}
}

func mustTable(t *testing.T) *ScorerTable {
	t.Helper()
	return NewScorerTable()
}

// TestRegisterRejectsMissingContractFace：无 ScorerContract 的评分器不予装配
// ——契约面缺失是最严重的残缺（无从校验、无从回放），fail-loud 不可协商.
func TestRegisterRejectsMissingContractFace(t *testing.T) {
	tb := mustTable(t)
	err := tb.Register("exact_match", bareScorer{entry: Entry{ID: "exact_match", Version: "1.0.0"}})
	if !errors.Is(err, ErrInvalidContract) {
		t.Fatalf("无契约面条目必须 ErrInvalidContract，得到 %v", err)
	}
	if tb.Len() != 0 {
		t.Fatalf("被拒条目不得入库: len=%d", tb.Len())
	}
}

// TestRegisterContractValidation 表驱动覆盖声明面校验全部分支（验收 #2 注册期）.
func TestRegisterContractValidation(t *testing.T) {
	base := exactMatchSpec()
	cases := []struct {
		name    string
		key     string
		mutate  func(*ScorerSpec)
		wantSub string // 错误文本必含的细分原因
	}{
		{
			name:    "声明 id 与登记键不一致",
			key:     "other_id",
			mutate:  func(*ScorerSpec) {},
			wantSub: "不一致",
		},
		{
			name: "version 为空",
			key:  "exact_match",
			mutate: func(s *ScorerSpec) {
				s.Entry.Version = ""
			},
			wantSub: "version 为空",
		},
		{
			name: "输入面双缺（无键表也无任意声明）",
			key:  "exact_match",
			mutate: func(s *ScorerSpec) {
				s.InputSchema = nil
			},
			wantSub: "恰一声明",
		},
		{
			name: "输入面双写（键表与任意声明并存）",
			key:  "exact_match",
			mutate: func(s *ScorerSpec) {
				s.AcceptsAnyInput = true
			},
			wantSub: "恰一声明",
		},
		{
			name: "入参键表含空键",
			key:  "exact_match",
			mutate: func(s *ScorerSpec) {
				s.InputSchema[""] = KindString
			},
			wantSub: "空键",
		},
		{
			name: "AI 评分缺 prompt 版本",
			key:  "exact_match",
			mutate: func(s *ScorerSpec) {
				s.Deterministic = false
			},
			wantSub: "prompt 版本必填",
		},
		{
			name: "确定性评分器反挂 prompt 版本",
			key:  "exact_match",
			mutate: func(s *ScorerSpec) {
				s.PromptVersion = "v1"
			},
			wantSub: "不得声明 prompt 版本",
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			spec := base
			// 深拷贝键表：各用例互不串扰（map 共享引用会泄漏前例的变异）.
			schema := make(map[string]ParamKind, len(base.InputSchema))
			for k, v := range base.InputSchema {
				schema[k] = v
			}
			spec.InputSchema = schema
			tc.mutate(&spec)
			tb := mustTable(t)
			err := tb.Register(tc.key, fakeScorer{entry: spec.Entry, spec: spec})
			if !errors.Is(err, ErrInvalidContract) || !strings.Contains(err.Error(), tc.wantSub) {
				t.Fatalf("err = %v, want ErrInvalidContract 含 %q", err, tc.wantSub)
			}
			if tb.Len() != 0 {
				t.Fatalf("被拒条目不得入库: len=%d", tb.Len())
			}
		})
	}
}

// TestRegisterAcceptsWellFormedEntries：合规条目（确定性键表型 + 任意作答型
// AI 仲裁型）注册成功且声明面随条目留存，重复注册仍 ErrDuplicate.
func TestRegisterAcceptsWellFormedEntries(t *testing.T) {
	tb := mustTable(t)
	em := exactMatchSpec()
	if err := tb.Register("exact_match", fakeScorer{entry: em.Entry, spec: em}); err != nil {
		t.Fatalf("合规确定性条目注册失败: %v", err)
	}
	arb := ScorerSpec{
		Entry:           Entry{ID: "model_arbiter", Version: "1.0.0+arbiter"},
		AcceptsAnyInput: true,
		Deterministic:   false,
		PromptVersion:   "v3",
	}
	if err := tb.Register("model_arbiter", fakeScorer{entry: arb.Entry, spec: arb}); err != nil {
		t.Fatalf("合规仲裁条目注册失败: %v", err)
	}
	if tb.Len() != 2 {
		t.Fatalf("条目数应为 2: %d", tb.Len())
	}
	if err := tb.Register("exact_match", fakeScorer{entry: em.Entry, spec: em}); !errors.Is(err, ErrDuplicate) {
		t.Fatalf("重复注册必须 ErrDuplicate，得到 %v", err)
	}
	gotScorer, gotSpec, ok := tb.Get("model_arbiter")
	if !ok || gotScorer.Entry().ID != "model_arbiter" || gotSpec.PromptVersion != "v3" {
		t.Fatalf("条目与声明面装配后必须可查: ok=%v spec=%+v", ok, gotSpec)
	}
	if _, _, ok := tb.Get("nope"); ok {
		t.Fatal("未注册条目不可查")
	}
}

// TestValidateResultVerdictShape：verdict 形态双向强制（D10）。
func TestValidateResultVerdictShape(t *testing.T) {
	det := exactMatchSpec()
	ai := ScorerSpec{
		Entry:           Entry{ID: "model_arbiter", Version: "1.0.0"},
		AcceptsAnyInput: true,
		PromptVersion:   "v3",
	}
	cases := []struct {
		name    string
		spec    ScorerSpec
		res     ScoreResult
		wantErr error
	}{
		{
			name: "确定性合规（无模型身份）",
			spec: det,
			res:  ScoreResult{Correct: true, Score: 1, Confidence: 1},
		},
		{
			name:    "确定性条目伪挂模型身份",
			spec:    det,
			res:     ScoreResult{Correct: true, Score: 1, Confidence: 1, Model: "gpt", ModelVersion: "2026-01"},
			wantErr: ErrInvalidResult,
		},
		{
			name: "AI 评分合规（模型身份齐全）",
			spec: ai,
			res:  ScoreResult{Correct: true, Score: 1, Confidence: 0.8, Model: "gpt", ModelVersion: "2026-01"},
		},
		{
			name:    "AI 评分缺模型版本",
			spec:    ai,
			res:     ScoreResult{Correct: true, Score: 1, Confidence: 0.8, Model: "gpt"},
			wantErr: ErrInvalidResult,
		},
		{
			name:    "AI 评分缺模型标识",
			spec:    ai,
			res:     ScoreResult{Correct: true, Score: 1, Confidence: 0.8, ModelVersion: "2026-01"},
			wantErr: ErrInvalidResult,
		},
		{
			name:    "置信度越上限",
			spec:    ai,
			res:     ScoreResult{Confidence: 1.1, Model: "gpt", ModelVersion: "v"},
			wantErr: ErrInvalidResult,
		},
		{
			name:    "置信度为 NaN",
			spec:    ai,
			res:     ScoreResult{Confidence: math.NaN(), Model: "gpt", ModelVersion: "v"},
			wantErr: ErrInvalidResult,
		},
		{
			name:    "聚合分为 NaN",
			spec:    ai,
			res:     ScoreResult{Confidence: 0.5, Score: math.NaN(), Model: "gpt", ModelVersion: "v"},
			wantErr: ErrInvalidResult,
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			err := ValidateResult(tc.spec, tc.res)
			if tc.wantErr == nil {
				if err != nil {
					t.Fatalf("合规结果被误拒: %v", err)
				}
				return
			}
			if !errors.Is(err, tc.wantErr) {
				t.Fatalf("err = %v, want %v", err, tc.wantErr)
			}
		})
	}
}

// TestADR0005ArbitrationAlignment：ADR-0005 判据的结构化落地——人工兜底形态
// （非确定性却无模型身份/prompt 版本，人工结论给不出这些台账要素）过不了装配
// 门；L3 模型仲裁形态（模型身份 + prompt 版本 + 任意作答声明）天然合规.
func TestADR0005ArbitrationAlignment(t *testing.T) {
	humanConfirm := ScorerSpec{
		Entry:           Entry{ID: "human_confirm", Version: "1.0.0"},
		AcceptsAnyInput: true,
		Deterministic:   false, // 人工兜底非确定性，却给不出 prompt 版本
	}
	tb := mustTable(t)
	if err := tb.Register("human_confirm", fakeScorer{entry: humanConfirm.Entry, spec: humanConfirm}); !errors.Is(err, ErrInvalidContract) {
		t.Fatalf("human_confirm 形态必须被装配门拒绝，得到 %v", err)
	}
	// 结果侧同样拦截：即便声明面合规，AI 结果缺模型身份也不得出分.
	ai := ScorerSpec{Entry: Entry{ID: "model_arbiter", Version: "1.0.0"}, AcceptsAnyInput: true, PromptVersion: "v3"}
	if err := ValidateResult(ai, ScoreResult{Confidence: 0.5}); !errors.Is(err, ErrInvalidResult) {
		t.Fatalf("AI 结果缺模型身份必须 ErrInvalidResult，得到 %v", err)
	}
}
