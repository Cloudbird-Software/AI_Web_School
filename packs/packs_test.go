package packs

import (
	"context"
	"strings"
	"testing"

	"github.com/Cloudbird-Software/AI_Web_School/registry"
)

// stubInteraction/stubScorer：注册表条目的最小桩（只用于验证解析通道，
// 不作为契约的一部分——契约只允许经注册表引用）。
type stubInteraction struct{ id string }

func (s stubInteraction) Entry() registry.Entry                { return registry.Entry{ID: s.id} }
func (s stubInteraction) Normalize(raw string) (string, error) { return raw, nil }

type stubScorer struct{ id string }

func (s stubScorer) Entry() registry.Entry { return registry.Entry{ID: s.id} }
func (s stubScorer) Score(_ context.Context, _ string, _ map[string]any) (registry.ScoreResult, error) {
	return registry.ScoreResult{}, nil
}

func TestResolveInteractions_RegisteredRefResolves(t *testing.T) {
	reg := registry.New[registry.Interaction]()
	if err := reg.Register("single_choice", stubInteraction{id: "single_choice"}); err != nil {
		t.Fatal(err)
	}
	got, err := ResolveInteractions(reg, []InteractionRef{{ID: "single_choice", Params: map[string]any{"max": 4}}})
	if err != nil {
		t.Fatalf("已注册条目解析失败: %v", err)
	}
	if got[0].Entry().ID != "single_choice" {
		t.Fatalf("解析结果不符: %+v", got)
	}
}

func TestResolveInteractions_UnregisteredRefFails(t *testing.T) {
	reg := registry.New[registry.Interaction]() // 空注册表：引用必为私造
	if _, err := ResolveInteractions(reg, []InteractionRef{{ID: "private_interaction"}}); err == nil {
		t.Fatal("未注册 id 必须装配失败（D4：禁止私造）")
	} else if !strings.Contains(err.Error(), "未在注册表登记") {
		t.Fatalf("错误语义不符: %v", err)
	}
}

func TestResolveScorers_RegisteredAndUnregistered(t *testing.T) {
	reg := registry.New[registry.Scorer]()
	if err := reg.Register("exact_match", stubScorer{id: "exact_match"}); err != nil {
		t.Fatal(err)
	}
	if _, err := ResolveScorers(reg, []ScorerRef{{ID: "exact_match"}}); err != nil {
		t.Fatalf("已注册条目解析失败: %v", err)
	}
	if _, err := ResolveScorers(reg, []ScorerRef{{ID: "private_scorer"}}); err == nil {
		t.Fatal("未注册 id 必须装配失败（D4：禁止私造）")
	}
}
