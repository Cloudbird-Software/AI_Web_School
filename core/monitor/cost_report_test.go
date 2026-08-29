package monitor

import (
	"math"
	"reflect"
	"testing"

	"github.com/Cloudbird-Software/AI_Web_School/core/ai"
)

// 地面真值：三条台账（两题一 ad_hoc），手工算好期望值。
func fixtureEntries() []ai.LedgerEntry {
	return []ai.LedgerEntry{
		{Model: "deepseek-v4-flash", TaskName: "gen_item", CostCNY: 0.10, ArtifactRef: "item-1"},
		{Model: "deepseek-v4-flash", TaskName: "gen_item", CostCNY: 0.20, ArtifactRef: "item-2"},
		{Model: "gpt-x", TaskName: "tts", CostCNY: 0.05}, // ad_hoc：无 artifact_ref
	}
}

func TestBuildCostReportGroundTruth(t *testing.T) {
	r, err := BuildCostReport(fixtureEntries(), nil)
	if err != nil {
		t.Fatal(err)
	}
	// 手算：total = 0.10+0.20+0.05 = 0.35
	if math.Abs(r.TotalCostCNY-0.35) > 1e-9 {
		t.Fatalf("total = %v, want 0.35", r.TotalCostCNY)
	}
	if math.Abs(r.ByModel["deepseek-v4-flash"]-0.30) > 1e-9 || math.Abs(r.ByModel["gpt-x"]-0.05) > 1e-9 {
		t.Fatalf("by_model = %v", r.ByModel)
	}
	if math.Abs(r.ByTask["gen_item"]-0.30) > 1e-9 || math.Abs(r.ByTask["tts"]-0.05) > 1e-9 {
		t.Fatalf("by_task = %v", r.ByTask)
	}
	// nil extractor → 全归 unknown
	if math.Abs(r.BySubject["unknown"]-0.35) > 1e-9 {
		t.Fatalf("by_subject = %v", r.BySubject)
	}
	// 唯一 artifact_ref = 2 → avg = 0.35/2 = 0.175
	if r.ItemCount != 2 || math.Abs(r.AvgCostPerItem-0.175) > 1e-9 {
		t.Fatalf("item_count=%d avg=%v", r.ItemCount, r.AvgCostPerItem)
	}
	if r.CallCount != 3 {
		t.Fatalf("call_count = %d", r.CallCount)
	}
}

func TestBuildCostReportWithDimension(t *testing.T) {
	r, err := BuildCostReport(fixtureEntries(), func(artifactRef string) string {
		if artifactRef == "item-1" {
			return "math"
		}
		return "chinese"
	})
	if err != nil {
		t.Fatal(err)
	}
	// item-1 → math(0.10)；item-2 → chinese(0.20)；ad_hoc → unassigned(0.05)
	if math.Abs(r.BySubject["math"]-0.10) > 1e-9 || math.Abs(r.BySubject["chinese"]-0.20) > 1e-9 || math.Abs(r.BySubject["unassigned"]-0.05) > 1e-9 {
		t.Fatalf("by_subject = %v", r.BySubject)
	}
}

func TestBuildCostReportEmpty(t *testing.T) {
	r, err := BuildCostReport([]ai.LedgerEntry{}, nil)
	if err != nil {
		t.Fatal(err)
	}
	if r.TotalCostCNY != 0 || r.ItemCount != 0 || r.AvgCostPerItem != 0 {
		t.Fatalf("空台账应全零: %+v", r)
	}
}

func TestBuildCostReportNilFailClosed(t *testing.T) {
	if _, err := BuildCostReport(nil, nil); err == nil {
		t.Fatal("nil entries 应 fail-closed")
	}
}

func TestSortedKeysDeterministic(t *testing.T) {
	m := map[string]float64{"b": 1, "a": 2, "c": 3}
	got := SortedKeys(m)
	if !reflect.DeepEqual(got, []string{"a", "b", "c"}) {
		t.Fatalf("keys = %v", got)
	}
}
