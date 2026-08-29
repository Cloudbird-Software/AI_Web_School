package report

import (
	"errors"
	"math"
	"testing"
	"time"
)

// approxClose 后验为连续量，用相对 1e-12 容差断言（float64 累加序与冻结实现
// 同序逐位一致，容差只为测试可读性）.
func approxClose(a, b float64) bool { return math.Abs(a-b) < 1e-12 }

func infer(errorTypeID string, confidence any) map[string]any {
	m := map[string]any{"error_type_id": errorTypeID}
	if confidence != nil {
		m["confidence"] = confidence
	}
	return m
}

// 地面真值：手算 Beta(1,1) 先验下的后验——3 条 0.8 证据 ⇒ α=3.4, β=1.6,
// 后验=0.68（与冻结实现 aggregator.py 同公式同序逐位一致）.
func TestAggregateInferencesGroundTruth(t *testing.T) {
	events := []InferenceEventView{
		{ItemVersionID: "iv-1", ErrorInferences: []map[string]any{infer("e-calc", 0.8)}},
		{ItemVersionID: "iv-2", ErrorInferences: []map[string]any{infer("e-calc", 0.8)}},
		{ItemVersionID: "iv-3", ErrorInferences: []map[string]any{infer("e-calc", 0.8)}},
	}
	evidences, err := AggregateInferences(events)
	if err != nil {
		t.Fatalf("AggregateInferences: %v", err)
	}
	if len(evidences) != 1 {
		t.Fatalf("应只含一个错误类型: %d", len(evidences))
	}
	ev := evidences["e-calc"]
	if ev.EvidenceCount != 3 {
		t.Fatalf("证据计数错: %d", ev.EvidenceCount)
	}
	if !approxClose(ev.Alpha, 3.4) || !approxClose(ev.Beta, 1.6) {
		t.Fatalf("Beta 后验参数错: α=%v β=%v", ev.Alpha, ev.Beta)
	}
	if !approxClose(ev.Posterior(), 0.68) {
		t.Fatalf("后验均值错: %v", ev.Posterior())
	}
	// 来源题去重集合
	if ids := ev.ContributingItemVersionIDs(); len(ids) != 3 || ids[0] != "iv-1" || ids[2] != "iv-3" {
		t.Fatalf("来源题集合错: %v", ids)
	}
}

func TestAggregateInferencesMixedEvidence(t *testing.T) {
	events := []InferenceEventView{
		{ItemVersionID: "iv-1", ErrorInferences: []map[string]any{infer("e-a", 0.9), {"error_type_id": "e-b"}}},
		{ItemVersionID: "iv-2", ErrorInferences: []map[string]any{infer("e-a", 0.9)}},
		{ItemVersionID: "iv-1", ErrorInferences: []map[string]any{infer("e-a", 0.5)}},
	}
	evidences, err := AggregateInferences(events)
	if err != nil {
		t.Fatalf("AggregateInferences: %v", err)
	}
	ea := evidences["e-a"]
	// α = 1 + 0.9 + 0.9 + 0.5 = 3.3；β = 1 + 0.1 + 0.1 + 0.5 = 1.7；后验 = 0.66
	if ea.EvidenceCount != 3 || !approxClose(ea.Alpha, 3.3) || !approxClose(ea.Beta, 1.7) {
		t.Fatalf("e-a 证据错: %+v", ea)
	}
	if !approxClose(ea.Posterior(), 0.66) {
		t.Fatalf("e-a 后验错: %v", ea.Posterior())
	}
	// 同一题两条证据：计数 2 次，来源题只记一次
	if ids := ea.ContributingItemVersionIDs(); len(ids) != 2 {
		t.Fatalf("来源题应去重: %v", ids)
	}
	// 无 confidence 键：缺省按 0.0（冻结实现 .get("confidence", 0.0)；
	// α 不动 β +1 ⇒ α=1, β=2）
	eb := evidences["e-b"]
	if !approxClose(eb.Alpha, 1.0) || !approxClose(eb.Beta, 2.0) {
		t.Fatalf("e-b 证据错: %+v", eb)
	}
}

func TestAggregateInferencesDirtyData(t *testing.T) {
	// 脏推断跳过而非炸报告；合法推断正常累积
	events := []InferenceEventView{
		{ItemVersionID: "iv-1", ErrorInferences: []map[string]any{
			{"confidence": 0.9},                      // 缺 error_type_id → 跳过
			{"error_type_id": "", "confidence": 0.9}, // 空 id → 跳过
			{"error_type_id": 7},                     // 非 str id → 跳过
			infer("e-ok", 0.9),
		}},
	}
	evidences, err := AggregateInferences(events)
	if err != nil {
		t.Fatalf("脏数据不应报错: %v", err)
	}
	if len(evidences) != 1 {
		t.Fatalf("应只含合法类型: %+v", evidences)
	}
	if evidences["e-ok"].EvidenceCount != 1 {
		t.Fatalf("合法推断应累积: %+v", evidences["e-ok"])
	}
}

func TestAggregateInferencesInvalidConfidence(t *testing.T) {
	tests := []struct {
		name  string
		badge map[string]any // 待判定的单条错误推断
	}{
		{"越上界", infer("e-x", 1.2)},
		{"越下界", infer("e-x", -0.1)},
		{"显式null（float(None) 同样失败）", map[string]any{"error_type_id": "e-x", "confidence": nil}},
		{"非数值", infer("e-x", []any{1})},
		{"布尔（Python float(True) 真值陷阱）", infer("e-x", true)},
		{"非数字字符串", infer("e-x", "high")},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			events := []InferenceEventView{
				{ItemVersionID: "iv-1", ErrorInferences: []map[string]any{tt.badge}},
			}
			if _, err := AggregateInferences(events); !errors.Is(err, ErrInvalidInference) {
				t.Fatalf("期望 ErrInvalidInference，得到 %v", err)
			}
		})
	}
	// 合法形态：数字字符串 / json 数值（float64）/ 整数
	valid := []any{"0.5", 0.5, 0, 1, float32(0.25)}
	for _, c := range valid {
		events := []InferenceEventView{
			{ItemVersionID: "iv-1", ErrorInferences: []map[string]any{infer("e-ok", c)}},
		}
		if _, err := AggregateInferences(events); err != nil {
			t.Fatalf("confidence %v 应接受: %v", c, err)
		}
	}
	// 置信度 0：合法证据（计数 +1，α 不动 β +1）
	evidences, err := AggregateInferences([]InferenceEventView{
		{ItemVersionID: "iv-1", ErrorInferences: []map[string]any{infer("e-z", 0.0)}},
	})
	if err != nil {
		t.Fatalf("0 置信度应合法: %v", err)
	}
	if !approxClose(evidences["e-z"].Alpha, 1.0) || !approxClose(evidences["e-z"].Beta, 2.0) {
		t.Fatalf("0 置信度累积错: %+v", evidences["e-z"])
	}
}

// 等级化输出（D8 不排名）：阈值两级 + 确定性排序 + 4 位小数.
func TestBuildWeaknessItemsLevels(t *testing.T) {
	evidences := map[string]*ErrorEvidence{
		"e-calc":  {ErrorTypeID: "e-calc", EvidenceCount: 3, Alpha: 3.4, Beta: 1.6},  // 后验 0.68
		"e-read":  {ErrorTypeID: "e-read", EvidenceCount: 2, Alpha: 2.9, Beta: 1.1},  // 后验 0.725
		"e-write": {ErrorTypeID: "e-write", EvidenceCount: 3, Alpha: 2.0, Beta: 2.0}, // 后验 0.5，与 e-calc 同计数
	}
	items := BuildWeaknessItems(evidences, MinEvidenceDefault)
	if len(items) != 3 {
		t.Fatalf("条目数错: %d", len(items))
	}
	// 排序：证据多在前；同计数按 error_type_id 字典序（展示确定性，非排名）
	if items[0].ErrorTypeID != "e-calc" || items[1].ErrorTypeID != "e-write" || items[2].ErrorTypeID != "e-read" {
		t.Fatalf("排序错: %+v", items)
	}
	// 阈值两级
	if items[0].Status != StatusConcluded || items[1].Status != StatusConcluded {
		t.Fatalf("达阈值应为 concluded: %+v", items[:2])
	}
	if items[2].Status != StatusInsufficientEvidence {
		t.Fatalf("未达阈值应为 insufficient_evidence: %+v", items[2])
	}
	// 置信度 4 位小数；后验仅供参考，非分数（e-write 后验 0.5，e-read 0.725）
	if items[0].Confidence != 0.68 || items[1].Confidence != 0.5 || items[2].Confidence != 0.725 {
		t.Fatalf("置信度错: %+v", items)
	}
	// 纯函数核不给推荐（IO 面由服务化接线填充）
	for _, it := range items {
		if len(it.RecommendedItemVersionIDs) != 0 {
			t.Fatalf("核心域不应产生推荐: %+v", it)
		}
	}
	if MinEvidenceDefault != 3 {
		t.Fatalf("证据阈值默认应为 3: %d", MinEvidenceDefault)
	}
}

func TestBuildWeaknessReport(t *testing.T) {
	now := time.Date(2026, 8, 30, 8, 0, 0, 0, time.UTC)
	evidences := map[string]*ErrorEvidence{
		"e-calc": {ErrorTypeID: "e-calc", EvidenceCount: 3, Alpha: 3.4, Beta: 1.6},
	}
	report := BuildWeaknessReport("student-1", SceneDiagnosis, 3, now, evidences)
	if report.StudentAliasID != "student-1" || report.Scene != SceneDiagnosis ||
		report.MinEvidence != 3 || !report.GeneratedAt.Equal(now) || len(report.Items) != 1 {
		t.Fatalf("报告头错: %+v", report)
	}
	if !ValidReportScene(report.Scene) || !ValidReportScene("") {
		t.Fatal("D5 三值域与空（未过滤）应合法")
	}
	if ValidReportScene("exam") {
		t.Fatal("域外场景应拒绝")
	}
}
