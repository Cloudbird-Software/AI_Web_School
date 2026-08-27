package validators

import (
	"context"
	"encoding/json"
	"errors"
	"sync"
	"testing"

	"github.com/Cloudbird-Software/AI_Web_School/registry"
)

// 表驱动：验证器三值判定与 fail-closed 语义（冻结实现的失实路径在本包不存在：
// 判定面是内容摘要登记视图，绝不触碰主键列）。
func TestDuplicateValidatorVerdicts(t *testing.T) {
	published := map[string]any{
		"stem":   "3/4 和 1/2 哪个大？",
		"answer": "3/4",
	}
	pubDigest, err := ContentDigest(published)
	if err != nil {
		t.Fatalf("摘要计算失败: %v", err)
	}

	tests := []struct {
		name      string
		content   string // 送检内容（JSON）；空串表示用 go 字面量 contentGo
		contentGo any
		want      Verdict
	}{
		{
			name:    "完全相同内容_仅键序空白不同_判重拒绝",
			content: `{ "answer" : "3/4" , "stem": "3/4 和 1/2 哪个大？" }`,
			want:    VerdictFail,
		},
		{
			name:    "完全相同内容_逐字相同_判重拒绝",
			content: `{"stem":"3/4 和 1/2 哪个大？","answer":"3/4"}`,
			want:    VerdictFail,
		},
		{
			name:    "仅参数不同的合法变式不误判",
			content: `{"stem":"1/5 和 1/2 哪个大？","answer":"1/2"}`,
			want:    VerdictPass,
		},
		{
			name:      "nil内容_fail_closed",
			contentGo: nil,
			want:      VerdictFail,
		},
		{
			name:      "空对象_fail_closed",
			contentGo: map[string]any{},
			want:      VerdictFail,
		},
		{
			name:      "空数组_fail_closed",
			contentGo: []any{},
			want:      VerdictFail,
		},
		{
			name:      "标量根_非结构化_fail_closed",
			contentGo: "纯文本",
			want:      VerdictFail,
		},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			src := NewMemoryDigestSource()
			src.Publish("item", pubDigest)
			v := NewDuplicateValidator(src)

			var content any = tt.contentGo
			if tt.content != "" {
				if err := json.Unmarshal([]byte(tt.content), &content); err != nil {
					t.Fatalf("fixture 解码失败: %v", err)
				}
			}
			got := v.Validate(context.Background(), Candidate{ArtifactType: "item", Content: content})
			if got.Verdict != tt.want {
				t.Fatalf("verdict = %s (%v), want %s", got.Verdict, got.Evidence, tt.want)
			}
			switch tt.want {
			case VerdictFail:
				if len(got.Evidence["reason"].(string)) == 0 {
					t.Fatalf("fail 必须携带 reason 证据")
				}
			case VerdictPass:
				if got.Digest == "" || got.HitDigest != "" || got.Confidence != 1.0 {
					t.Fatalf("pass 应带摘要、无命中、置信 1: %+v", got)
				}
			}
		})
	}
}

// 发布路径闭环：真实写入路径（先发布再查重）而非伪造 ID 写入（X11 反例锚定）。
func TestDuplicateSecondPublishRejectedVariantPasses(t *testing.T) {
	src := NewMemoryDigestSource()
	v := NewDuplicateValidator(src)

	candidate := Candidate{ArtifactType: "item", Content: map[string]any{
		"stem": "比一比：0.8 与 3/4", "answer": "0.8",
	}}

	first := v.Validate(context.Background(), candidate)
	if first.Verdict != VerdictPass {
		t.Fatalf("首次入库应 pass: %+v", first)
	}
	// 模拟发布事务落库：内容摘要在发布时登记（迁移 0028 语义的进程内对偶）。
	src.Publish(candidate.ArtifactType, first.Digest)

	second := v.Validate(context.Background(), candidate)
	if second.Verdict != VerdictFail || second.HitDigest != first.Digest {
		t.Fatalf("同内容第二次入库必须判重且命中原摘要: %+v", second)
	}

	variant := Candidate{ArtifactType: "item", Content: map[string]any{
		"stem": "比一比：0.9 与 3/4", "answer": "0.9",
	}}
	third := v.Validate(context.Background(), variant)
	if third.Verdict != VerdictPass || third.Digest == first.Digest {
		t.Fatalf("参数变式不得误判为重复: %+v", third)
	}
}

// 不放行语义：未挂接源或源故障一律 review，绝不伪造 pass。
func TestDuplicateValidatorFailsClosedOnUnknownSourceState(t *testing.T) {
	failing := DigestSourceFunc(func(_ context.Context, _, _ string) (bool, error) {
		return false, errors.New("db down")
	})
	tests := []struct {
		name string
		src  DigestSource
	}{{name: "未挂接源", src: nil}, {name: "源查询失败", src: failing}}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			v := NewDuplicateValidator(tt.src)
			got := v.Validate(context.Background(), Candidate{
				ArtifactType: "item",
				Content:      map[string]any{"stem": "任何内容"},
			})
			if got.Verdict != VerdictReview || got.Confidence != 0 {
				t.Fatalf("%s: 必须 review 且置信 0，实得 %+v", tt.name, got)
			}
		})
	}
}

// artifact_type 为空 fail-closed（无法定位查重登记面）。
func TestDuplicateValidatorEmptyArtifactTypeFailsClosed(t *testing.T) {
	v := NewDuplicateValidator(NewMemoryDigestSource())
	got := v.Validate(context.Background(), Candidate{
		ArtifactType: " ", Content: map[string]any{"stem": "x"},
	})
	if got.Verdict != VerdictFail {
		t.Fatalf("空 artifact_type 必须 fail-closed: %+v", got)
	}
}

// 并发安全：登记与判定并发交错，结果恒为 pass/fail 且无数据竞争（-race）。
func TestDuplicateValidatorConcurrentPublishAndValidate(t *testing.T) {
	src := NewMemoryDigestSource()
	v := NewDuplicateValidator(src)

	const n = 64
	var wg sync.WaitGroup
	for i := 0; i < n; i++ {
		wg.Add(2)
		go func(i int) {
			defer wg.Done()
			src.Publish("item", digestOf(t, map[string]any{"i": i}))
		}(i)
		go func(i int) {
			defer wg.Done()
			r := v.Validate(context.Background(), Candidate{
				ArtifactType: "item",
				Content:      map[string]any{"i": i % 8}, // 与登记项概率性重叠
			})
			if r.Verdict != VerdictPass && r.Verdict != VerdictFail && r.Verdict != VerdictReview {
				t.Errorf("非法 verdict: %q", r.Verdict)
			}
			if r.Digest != "" && len(r.Digest) != len(DigestPrefix)+64 {
				t.Errorf("digest 格式漂移: %q", r.Digest)
			}
		}(i)
	}
	wg.Wait()
	if got := src.Len("item"); got != n {
		t.Fatalf("登记条数 = %d, want %d", got, n)
	}
}

// 注册挂接：PlatformRegistry 复用 registry.Registry 形态——id 冲突报
// ErrDuplicate，条目可 Get 回取（D4 门侧延伸）。
func TestPlatformRegistryInstall(t *testing.T) {
	r, err := PlatformRegistry(NewMemoryDigestSource(), NewMemoryFactSource(), nil)
	if err != nil {
		t.Fatalf("装配失败: %v", err)
	}
	entry, ok := r.Get(DuplicateValidatorID)
	if !ok {
		t.Fatalf("duplicate 验证器应已注册")
	}
	if entry.Entry().ID != DuplicateValidatorID || entry.Entry().Version != DuplicateValidatorVersion {
		t.Fatalf("条目身份漂移: %+v", entry.Entry())
	}
	// T-W5-021：语篇事实核查验证器同表挂接，id 沿用冻结策略矩阵的
	// passage_fact_check，W6 策略链按此 id 取用。
	fc, ok := r.Get(FactCheckValidatorID)
	if !ok {
		t.Fatalf("passage_fact_check 验证器应已注册")
	}
	if fc.Entry().ID != FactCheckValidatorID || fc.Entry().Version != FactCheckValidatorVersion {
		t.Fatalf("条目身份漂移: %+v", fc.Entry())
	}
	if r.Len() != 2 {
		t.Fatalf("平台通用注册表应只含已评审条目，实际 %d", r.Len())
	}
	if err := r.Register(DuplicateValidatorID, entry); !errors.Is(err, registry.ErrDuplicate) {
		t.Fatalf("重复注册必须失败（ErrDuplicate），实得 %v", err)
	}
	if err := r.Register(FactCheckValidatorID, fc); !errors.Is(err, registry.ErrDuplicate) {
		t.Fatalf("重复注册必须失败（ErrDuplicate），实得 %v", err)
	}
}

func digestOf(t *testing.T, v any) string {
	t.Helper()
	d, err := ContentDigest(v)
	if err != nil {
		t.Fatalf("摘要计算失败: %v", err)
	}
	return d
}
