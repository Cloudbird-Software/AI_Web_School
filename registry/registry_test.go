package registry

import (
	"context"
	"testing"
)

// T-W5-031 验收 #5：双注册表类型落点 + 条目只增不改（重复注册被拒）。
// 下方 demoInteraction/demoScorer 是接口的编译级演示实现：
// var _ 断言在编译期锁定接口形状（D4 的类型落点），不是生产实现。

type demoInteraction struct{}

func (demoInteraction) Entry() Entry                         { return Entry{ID: "demo_choice", Version: "1.0.0"} }
func (demoInteraction) Normalize(raw string) (string, error) { return raw, nil }

type demoScorer struct{}

func (demoScorer) Entry() Entry { return Entry{ID: "demo_exact", Version: "1.0.0"} }
func (demoScorer) Score(_ context.Context, answer string, _ map[string]any) (ScoreResult, error) {
	return ScoreResult{Correct: answer != "", Score: 1, Confidence: 1}, nil
}

var (
	_ Interaction = demoInteraction{}
	_ Scorer      = demoScorer{}
)

// TestDemoEntriesRegistered：演示实现经双注册表装配可查（D4 装配路径最小实证）。
func TestDemoEntriesRegistered(t *testing.T) {
	interactions := New[Interaction]()
	if err := interactions.Register("demo_choice", demoInteraction{}); err != nil {
		t.Fatalf("注册交互失败: %v", err)
	}
	scorers := New[Scorer]()
	if err := scorers.Register("demo_exact", demoScorer{}); err != nil {
		t.Fatalf("注册评分器失败: %v", err)
	}
	if it, ok := interactions.Get("demo_choice"); !ok || it.Entry().Version != "1.0.0" {
		t.Fatalf("交互条目装配后必须可查，得到 %+v ok=%v", it, ok)
	}
	res, ok := scorers.Get("demo_exact")
	if !ok {
		t.Fatalf("评分器条目装配后必须可查")
	}
	sr, serr := res.Score(context.Background(), "42", nil)
	if serr != nil || !sr.Correct || sr.ModelVersion != "" {
		t.Fatalf("确定性评分器 Score 契约异常: %+v %v", sr, serr)
	}
}

func TestRegistryDuplicateRejected(t *testing.T) {
	reg := New[Entry]()
	if err := reg.Register("single_choice", Entry{ID: "single_choice", Version: "1.0.0"}); err != nil {
		t.Fatalf("首次注册不应失败: %v", err)
	}
	if err := reg.Register("single_choice", Entry{ID: "single_choice", Version: "1.0.1"}); err != ErrDuplicate {
		t.Fatalf("重复注册必须 ErrDuplicate，得到 %v", err)
	}
	if reg.Len() != 1 {
		t.Fatalf("条目数应为 1，得到 %d", reg.Len())
	}
	if got, ok := reg.Get("single_choice"); !ok || got.Version != "1.0.0" {
		t.Fatalf("已注册条目必须可查且保持原版本，得到 %+v ok=%v", got, ok)
	}
}

func TestRegistryGetMissing(t *testing.T) {
	reg := New[Entry]()
	if _, ok := reg.Get("nope"); ok {
		t.Fatal("未注册条目不可查")
	}
}
