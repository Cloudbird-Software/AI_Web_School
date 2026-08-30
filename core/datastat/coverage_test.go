package datastat

import (
	"errors"
	"testing"
)

// 手算示例（冻结实现 coverage_gap.compute_coverage_gap 语义）：
// 同一 4 轴池（kp1×remember×M×subject-math）现有 4 题服务全部用途；
// practice 需 10 → 缺 6；measurement 需 3 → 已满足（actual>target 封顶 100%）；
// kp2 目标 0 → 覆盖率 100%（无需求不除零）。
func coverageFixture() ([]CoverageTarget, map[CoverageKey]int) {
	profile := []CoverageTarget{
		{KpCode: "kp1", CognitiveLevel: "remember", Purpose: ScopePractice, Gradeband: "M", Subject: "subject-math", Target: 10},
		{KpCode: "kp1", CognitiveLevel: "remember", Purpose: ScopeMeasurement, Gradeband: "M", Subject: "subject-math", Target: 3},
		{KpCode: "kp2", CognitiveLevel: "understand", Purpose: ScopePractice, Gradeband: "M", Subject: "subject-math", Target: 0},
	}
	actuals := map[CoverageKey]int{
		{KpCode: "kp1", CognitiveLevel: "remember", Gradeband: "M", Subject: "subject-math"}: 4,
	}
	return profile, actuals
}

func TestComputeCoverageGap_HandExample(t *testing.T) {
	profile, actuals := coverageFixture()
	snap := "snap-1"
	m, err := ComputeCoverageGap(profile, actuals, &snap)
	if err != nil {
		t.Fatalf("ComputeCoverageGap 失败：%v", err)
	}
	if len(m.Cells) != 3 {
		t.Fatalf("单元格数 = %d，期望 3", len(m.Cells))
	}
	c1 := m.Cells[0]
	if c1.Target != 10 || c1.Actual != 4 || c1.Gap != 6 {
		t.Errorf("cell1 target/actual/gap = %d/%d/%d，期望 10/4/6", c1.Target, c1.Actual, c1.Gap)
	}
	if c1.CoveragePct != 40.0 {
		t.Errorf("cell1 覆盖率 = %v，期望精确 40.0", c1.CoveragePct)
	}
	// 用途无关：measurement 单元格 actual 同池 4；gap = max(0, 3-4) = 0
	c2 := m.Cells[1]
	if c2.Actual != 4 || c2.Gap != 0 {
		t.Errorf("cell2 actual/gap = %d/%d，期望 4/0（同池服务全部用途）", c2.Actual, c2.Gap)
	}
	if c2.CoveragePct != 100.0 {
		t.Errorf("cell2 覆盖率 = %v，期望精确 100.0（超额封顶）", c2.CoveragePct)
	}
	// target=0 → 覆盖率 100.0（避免除零）
	c3 := m.Cells[2]
	if c3.CoveragePct != 100.0 || c3.Gap != 0 {
		t.Errorf("cell3 覆盖率/缺口 = %v/%d，期望 100.0/0", c3.CoveragePct, c3.Gap)
	}
	// 汇总：total_actual = min(4,10)+min(4,3)+min(0,0) = 7（手算）
	if m.TotalTarget != 13 || m.TotalActual != 7 || m.TotalGap != 6 {
		t.Errorf("汇总 target/actual/gap = %d/%d/%d，期望 13/7/6", m.TotalTarget, m.TotalActual, m.TotalGap)
	}
	// 全局覆盖率 = 7/13·100 = 53.84615384615385（手算）
	assertApproxF(t, "overall", m.OverallCoveragePct, 53.84615384615385, 1e-12)
	if m.SnapshotID == nil || *m.SnapshotID != "snap-1" {
		t.Errorf("snapshot_id = %v，期望 snap-1", m.SnapshotID)
	}
}

func TestComputeCoverageGap_Errors(t *testing.T) {
	profile, actuals := coverageFixture()
	bad := append([]CoverageTarget(nil), profile...)
	bad[0].Purpose = "exam"
	if _, err := ComputeCoverageGap(bad, actuals, nil); !errors.Is(err, ErrInvalidPurpose) {
		t.Errorf("越域 purpose 应报 ErrInvalidPurpose，得到 %v", err)
	}
	bad = append([]CoverageTarget(nil), profile...)
	bad[0].Target = -1
	if _, err := ComputeCoverageGap(bad, actuals, nil); !errors.Is(err, ErrNegativeTarget) {
		t.Errorf("负 target 应报 ErrNegativeTarget，得到 %v", err)
	}
}

func TestComputeCoverageGap_EmptyProfile(t *testing.T) {
	m, err := ComputeCoverageGap(nil, nil, nil)
	if err != nil {
		t.Fatalf("ComputeCoverageGap 失败：%v", err)
	}
	if len(m.Cells) != 0 {
		t.Errorf("空 profile 应无单元格，得到 %d", len(m.Cells))
	}
	if m.TotalTarget != 0 || m.TotalActual != 0 || m.TotalGap != 0 {
		t.Errorf("空 profile 汇总应全 0，得到 %d/%d/%d", m.TotalTarget, m.TotalActual, m.TotalGap)
	}
	if m.OverallCoveragePct != 100.0 {
		t.Errorf("空 profile 全局覆盖率 = %v，期望 100.0", m.OverallCoveragePct)
	}
}

func TestComputeCoverageGap_MissingActualKeyIsZero(t *testing.T) {
	profile := []CoverageTarget{{KpCode: "kp-x", CognitiveLevel: "apply", Purpose: ScopePractice, Gradeband: "L", Subject: "subject-math", Target: 5}}
	m, err := ComputeCoverageGap(profile, nil, nil)
	if err != nil {
		t.Fatalf("ComputeCoverageGap 失败：%v", err)
	}
	if m.Cells[0].Actual != 0 || m.Cells[0].Gap != 5 || m.Cells[0].CoveragePct != 0.0 {
		t.Errorf("缺失键应视为 0：cell = %+v", m.Cells[0])
	}
}

func TestCoverageGapMatrix_ToCSV(t *testing.T) {
	profile, actuals := coverageFixture()
	m, err := ComputeCoverageGap(profile, actuals, nil)
	if err != nil {
		t.Fatalf("ComputeCoverageGap 失败：%v", err)
	}
	// 逐字节地面真值：\r\n 行终止（Python csv.writer 默认 lineterminator），
	// coverage_pct 保留 2 位、整值浮点带 .0（Python str(float) 口径）
	want := "kp_code,cognitive_level,purpose,gradeband,subject,target,actual,gap,coverage_pct\r\n" +
		"kp1,remember,practice,M,subject-math,10,4,6,40.0\r\n" +
		"kp1,remember,measurement,M,subject-math,3,4,0,100.0\r\n" +
		"kp2,understand,practice,M,subject-math,0,0,0,100.0\r\n"
	if got := m.ToCSV(); got != want {
		t.Errorf("ToCSV 不匹配：\n%q\n期望：\n%q", got, want)
	}
}

func TestCoverageGapMatrix_ToJSON(t *testing.T) {
	profile, actuals := coverageFixture()
	snap := "snap-1"
	m, err := ComputeCoverageGap(profile, actuals, &snap)
	if err != nil {
		t.Fatalf("ComputeCoverageGap 失败：%v", err)
	}
	// 逐字节地面真值（Python json.dumps(ensure_ascii=False, indent=2) 同构：
	// 键序 = 冻结实现 to_dict；overall 保留 2 位；整值浮点带 .0）
	want := `{
  "snapshot_id": "snap-1",
  "summary": {
    "total_target": 13,
    "total_actual": 7,
    "total_gap": 6,
    "overall_coverage_pct": 53.85,
    "cell_count": 3
  },
  "cells": [
    {
      "kp_code": "kp1",
      "cognitive_level": "remember",
      "purpose": "practice",
      "gradeband": "M",
      "subject": "subject-math",
      "target": 10,
      "actual": 4,
      "gap": 6,
      "coverage_pct": 40.0
    },
    {
      "kp_code": "kp1",
      "cognitive_level": "remember",
      "purpose": "measurement",
      "gradeband": "M",
      "subject": "subject-math",
      "target": 3,
      "actual": 4,
      "gap": 0,
      "coverage_pct": 100.0
    },
    {
      "kp_code": "kp2",
      "cognitive_level": "understand",
      "purpose": "practice",
      "gradeband": "M",
      "subject": "subject-math",
      "target": 0,
      "actual": 0,
      "gap": 0,
      "coverage_pct": 100.0
    }
  ]
}`
	got, err := m.ToJSON()
	if err != nil {
		t.Fatalf("ToJSON 失败：%v", err)
	}
	if string(got) != want {
		t.Errorf("ToJSON 不匹配：\n%s\n期望：\n%s", got, want)
	}
}

func TestCoverageGapMatrix_GapCells(t *testing.T) {
	profile := []CoverageTarget{
		{KpCode: "a", CognitiveLevel: "r", Purpose: ScopePractice, Gradeband: "M", Subject: "s", Target: 10},
		{KpCode: "b", CognitiveLevel: "r", Purpose: ScopePractice, Gradeband: "M", Subject: "s", Target: 2},
		{KpCode: "c", CognitiveLevel: "r", Purpose: ScopePractice, Gradeband: "M", Subject: "s", Target: 5},
		{KpCode: "d", CognitiveLevel: "r", Purpose: ScopePractice, Gradeband: "M", Subject: "s", Target: 0},
	}
	actuals := map[CoverageKey]int{
		{KpCode: "a", CognitiveLevel: "r", Gradeband: "M", Subject: "s"}: 8, // 缺 2
		{KpCode: "b", CognitiveLevel: "r", Gradeband: "M", Subject: "s"}: 2, // 无缺口
		{KpCode: "c", CognitiveLevel: "r", Gradeband: "M", Subject: "s"}: 0, // 缺 5
	}
	m, err := ComputeCoverageGap(profile, actuals, nil)
	if err != nil {
		t.Fatalf("ComputeCoverageGap 失败：%v", err)
	}
	gaps := m.GapCells()
	if len(gaps) != 2 {
		t.Fatalf("缺口单元格数 = %d，期望 2", len(gaps))
	}
	// 按 gap 降序：c(5) 在前，a(2) 在后
	if gaps[0].KpCode != "c" || gaps[0].Gap != 5 {
		t.Errorf("首个缺口 = %s(%d)，期望 c(5)", gaps[0].KpCode, gaps[0].Gap)
	}
	if gaps[1].KpCode != "a" || gaps[1].Gap != 2 {
		t.Errorf("次个缺口 = %s(%d)，期望 a(2)", gaps[1].KpCode, gaps[1].Gap)
	}
}

func TestBuildProfileFromGrid(t *testing.T) {
	targets := BuildProfileFromGrid(
		[]string{"math.nal.decimal.compare", "math.nal.fractal.add"},
		[]string{"remember", "apply"},
		[]string{"M"},
		"subject-math",
		map[string]int{ScopePractice: 10, ScopeMeasurement: 3},
	)
	// 笛卡尔积 2×2×1×2 = 8；purpose 按 Purposes 规范序（practice 先于
	// measurement——固定规范序保证确定性，D6 可重放）
	if len(targets) != 8 {
		t.Fatalf("profile 长度 = %d，期望 8", len(targets))
	}
	if targets[0].KpCode != "math.nal.decimal.compare" || targets[0].CognitiveLevel != "remember" ||
		targets[0].Purpose != ScopePractice || targets[0].Target != 10 {
		t.Errorf("首格 = %+v", targets[0])
	}
	if targets[1].Purpose != ScopeMeasurement || targets[1].Target != 3 {
		t.Errorf("次格 = %+v", targets[1])
	}
	// 同 kp 下第二个认知层级
	if targets[2].KpCode != "math.nal.decimal.compare" || targets[2].CognitiveLevel != "apply" ||
		targets[2].Purpose != ScopePractice {
		t.Errorf("第三格 = %+v", targets[2])
	}
	// 第二个 kp
	if targets[4].KpCode != "math.nal.fractal.add" || targets[4].CognitiveLevel != "remember" {
		t.Errorf("第五格 = %+v", targets[4])
	}
}

func TestPyFloatFormat(t *testing.T) {
	// Python str(float) 口径：整值补 .0；最短往返
	cases := map[float64]string{
		40.0:  "40.0",
		100.0: "100.0",
		53.85: "53.85",
		0.0:   "0.0",
		12.5:  "12.5",
	}
	for v, want := range cases {
		if got := pyFloat(v); got != want {
			t.Errorf("pyFloat(%v) = %q，期望 %q", v, got, want)
		}
	}
	if got := pyFloat(round2(53.84615384615385)); got != "53.85" {
		t.Errorf("pyFloat(round2(7/13·100)) = %q，期望 53.85", got)
	}
}
