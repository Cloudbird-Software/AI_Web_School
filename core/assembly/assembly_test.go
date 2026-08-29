// assembly_test.go 组卷引擎域 Go 移植的验收测试。
//
// 测试策略：纯函数内核 + Memory 查询面，无 DB。每算法至少 1 正例 1 负例；
// 关键确定性输出（Profile digest / 选题序 / selection_digest / testlet_id /
// 细目表 JSON）为与 Python 冻结实现交叉验证的地面真值——运行冻结实现
// （pydantic 2.13.4 + ortools 环境）对相同 fixture 采样后固化于此。
package assembly

import (
	"context"
	"errors"
	"fmt"
	"reflect"
	"sort"
	"strings"
	"testing"
)

// ────────────────────────────────────────────────────────────────────
// 构造辅助（与冻结实现 tests/unit/test_assembly_solver.py 的 _mk 同构）
// ────────────────────────────────────────────────────────────────────

func strP(s string) *string { return &s }
func fP(f float64) *float64 { return &f }
func iP(i int) *int         { return &i }

// mkCandidate 构造候选题（对应 Python 测试 _mk：template 默认 tpl-<vid>）.
func mkCandidate(vid string, kp []string, p *float64, opts ...func(*CandidateItem)) CandidateItem {
	c := CandidateItem{
		ItemVersionID:     vid,
		ItemID:            fmt.Sprintf("item-%s", vid),
		TemplateVersionID: strP("tpl-" + vid),
		KpCodes:           kp,
		KpSetMode:         KpSetModeSingle,
		Gradeband:         GradebandM,
		InteractionID:     "single_choice",
		PCorrectPrior:     p,
		// 对齐 Python CandidateItem default_factory：缺省全场景许可.
		AllowedPurposes: []string{PurposePractice, PurposeDiagnosis, PurposeMeasurement},
	}
	for _, opt := range opts {
		opt(&c)
	}
	return c
}

func withTemplate(tpl *string) func(*CandidateItem) {
	return func(c *CandidateItem) { c.TemplateVersionID = tpl }
}
func withPurposes(ps []string) func(*CandidateItem) {
	return func(c *CandidateItem) { c.AllowedPurposes = ps }
}
func withMode(mode string) func(*CandidateItem)   { return func(c *CandidateItem) { c.KpSetMode = mode } }
func withMixTag(tag string) func(*CandidateItem)  { return func(c *CandidateItem) { c.MixTag = &tag } }
func withGroupID(gid string) func(*CandidateItem) { return func(c *CandidateItem) { c.GroupID = &gid } }
func withGradeband(gb string) func(*CandidateItem) {
	return func(c *CandidateItem) { c.Gradeband = gb }
}
func withNoTemplate() func(*CandidateItem) {
	return func(c *CandidateItem) { c.TemplateVersionID = nil }
}

// practiceProfile 对应 Python 测试 _practice_profile.
func practiceProfile(t *testing.T, kps []string, count [2]int) *AssemblyProfile {
	t.Helper()
	prof, err := CompileProfile(CompileInput{
		ProfileID:      "practice",
		ProfileVersion: "1.0.0",
		Purpose:        PurposePractice,
		Gradeband:      GradebandM,
		KpCodes:        kps,
		PurposeOverlay: map[string]any{"item_count_range": []any{count[0], count[1]}},
	})
	if err != nil {
		t.Fatalf("CompileProfile 失败: %v", err)
	}
	return prof
}

// idsOf 提取选题 id 序列.
func idsOf(items []CandidateItem) []string {
	out := make([]string, 0, len(items))
	for _, it := range items {
		out = append(out, it.ItemVersionID)
	}
	return out
}

func assembleInfeasible(t *testing.T, prof *AssemblyProfile, pool []CandidateItem, opts AssembleOptions) *InfeasibleError {
	t.Helper()
	if _, err := Assemble(prof, pool, opts); err == nil {
		t.Fatalf("期望组卷不可行，实际成功")
	} else {
		var inf *InfeasibleError
		if !errors.As(err, &inf) {
			t.Fatalf("期望 *InfeasibleError，实际 %T: %v", err, err)
		}
		return inf
	}
	panic("unreachable")
}

// ────────────────────────────────────────────────────────────────────
// 一、四维编译（profile.py）
// ────────────────────────────────────────────────────────────────────

// 地面真值（Python diagnosis_profile 同参 fixture 的 digest hexdigest）.
const wantDiagDigest = "8245a3a842e946370e53ac7a2661f39f87b78fd48dd4f9053e6d5b31c02a7fac"

// 地面真值（Python compile_profile 同参 fixture 的 digest hexdigest）.
const wantPracticeDigest = "039a8ca584eff4831ab6110c32cc3d1822cb1720b3e6c2c0136be6f38b3ed9d8"

func TestCompileProfilePracticeDefaults(t *testing.T) {
	prof, err := CompileProfile(CompileInput{
		ProfileID:      "practice-weekly",
		ProfileVersion: "1.0.0",
		Purpose:        PurposePractice,
		Gradeband:      GradebandM,
		KpCodes:        []string{"math.a", "math.b"},
		PurposeOverlay: map[string]any{"item_count_range": []any{10, 15}},
	})
	if err != nil {
		t.Fatalf("CompileProfile: %v", err)
	}
	c := &prof.Constraints
	if c.ItemCount.Min != 10 || c.ItemCount.Max != 15 || c.ItemCount.Soft {
		t.Fatalf("item_count 编译错: %+v", c.ItemCount)
	}
	want := []KpQuota{{KpCode: "math.a", MinCount: 1}, {KpCode: "math.b", MinCount: 1}}
	if !reflect.DeepEqual(c.KpQuotas, want) {
		t.Fatalf("kp_quotas 编译错: %+v", c.KpQuotas)
	}
	if !c.GradientMonotone || !c.ExposureMutexSameTemplate || !c.ExposureMutexCrossPeriod {
		t.Fatalf("默认开关编译错: %+v", c)
	}
	if c.MaxItemsPerGroup != 6 || c.RequireIsolatedItems {
		t.Fatalf("题组上限/诊断约束默认错: %+v", c)
	}
	if len(prof.Adjudications) != 0 {
		t.Fatalf("练习默认不应有裁决: %+v", prof.Adjudications)
	}
}

func TestCompileProfileOverlayMergePriorityGradebandWins(t *testing.T) {
	prof, err := CompileProfile(CompileInput{
		ProfileID: "p", ProfileVersion: "1.0.0",
		Purpose: PurposePractice, Gradeband: GradebandL,
		KpCodes:          []string{"math.a"},
		Base:             map[string]any{"item_count_range": []any{10, 20}},
		SubjectOverlay:   map[string]any{"item_count_range": []any{8, 15}},
		PurposeOverlay:   map[string]any{"item_count_range": []any{5, 12}},
		GradebandOverlay: map[string]any{"item_count_range": []any{4, 8}},
	})
	if err != nil {
		t.Fatalf("CompileProfile: %v", err)
	}
	if prof.Constraints.ItemCount.Min != 4 || prof.Constraints.ItemCount.Max != 8 {
		t.Fatalf("四维优先级错（gradeband 应最高）: %+v", prof.Constraints.ItemCount)
	}
}

func TestCompileProfileSubjectOverlayConstraints(t *testing.T) {
	subjectOverlay := map[string]any{
		"overlay_id":      "subject-math",
		"overlay_version": "1.0.0",
		"assembly_constraints": map[string]any{
			"require_gradient_monotone": true,
			"exposure_mutex": map[string]any{
				"same_template_different_paper": true,
				"cross_period_repeat":           false, // 不允许跨期重复 = 互斥开
			},
			"content_mix": map[string]any{
				"new_learning_ratio": []any{0.4, 0.6},
				"review_ratio":       []any{0.2, 0.4},
			},
		},
	}
	prof, err := CompileProfile(CompileInput{
		ProfileID: "p", ProfileVersion: "1.0.0",
		Purpose: PurposePractice, Gradeband: GradebandM,
		KpCodes:        []string{"math.a"},
		SubjectOverlay: subjectOverlay,
		PurposeOverlay: map[string]any{"item_count_range": []any{10, 15}},
	})
	if err != nil {
		t.Fatalf("CompileProfile: %v", err)
	}
	c := &prof.Constraints
	if !c.ExposureMutexSameTemplate || !c.ExposureMutexCrossPeriod {
		t.Fatalf("曝光互斥编译错: %+v", c)
	}
	if c.ContentMix == nil ||
		c.ContentMix.Ratios["new"] != [2]float64{0.4, 0.6} ||
		c.ContentMix.Ratios["review"] != [2]float64{0.2, 0.4} {
		t.Fatalf("content_mix 编译错: %+v", c.ContentMix)
	}
	if prof.OverlayRefs["subject"] != "subject-math@1.0.0" {
		t.Fatalf("overlay_refs 留档错: %+v", prof.OverlayRefs)
	}
}

func TestCompileProfileTargetPCorrectRange(t *testing.T) {
	prof, err := CompileProfile(CompileInput{
		ProfileID: "p", ProfileVersion: "1.0.0",
		Purpose: PurposePractice, Gradeband: GradebandM,
		KpCodes: []string{"math.a"},
		PurposeOverlay: map[string]any{
			"item_count_range": []any{8, 15},
			"difficulty_target": map[string]any{
				"target_p_correct_range": []any{0.70, 0.90},
				"uncertainty_margin":     0.05,
			},
		},
	})
	if err != nil {
		t.Fatalf("CompileProfile: %v", err)
	}
	if prof.Constraints.TargetPCorrectRange == nil || *prof.Constraints.TargetPCorrectRange != [2]float64{0.70, 0.90} {
		t.Fatalf("target_p_correct_range 编译错: %v", prof.Constraints.TargetPCorrectRange)
	}
	if prof.Constraints.PCorrectUncertaintyMargin != 0.05 {
		t.Fatalf("uncertainty_margin 编译错: %v", prof.Constraints.PCorrectUncertaintyMargin)
	}
}

func TestCompileProfileRejectsInvalidDomains(t *testing.T) {
	if _, err := CompileProfile(CompileInput{ProfileID: "p", ProfileVersion: "1", Purpose: "quiz", Gradeband: GradebandM}); err == nil {
		t.Fatalf("purpose 越域应报错")
	}
	if _, err := CompileProfile(CompileInput{ProfileID: "p", ProfileVersion: "1", Purpose: PurposePractice, Gradeband: "X"}); err == nil {
		t.Fatalf("gradeband 越域应报错")
	}
}

func TestDiagnosisProfileHardConstraints(t *testing.T) {
	prof, err := DiagnosisProfile(DiagnosisInput{
		ProfileID: "diag", ProfileVersion: "1.0.0", // profile_id 与地面真值 fixture 对齐
		Gradeband:      GradebandM,
		KpCodes:        []string{"math.a", "math.b"},
		ItemCountRange: &[2]int{20, 20},
	})
	if err != nil {
		t.Fatalf("DiagnosisProfile: %v", err)
	}
	c := &prof.Constraints
	if prof.Purpose != PurposeDiagnosis || !c.RequireIsolatedItems || !c.MultiPointRelationCheck {
		t.Fatalf("诊断硬约束缺省错: %+v", c)
	}
	for _, q := range c.KpQuotas {
		if !q.IsolatedOnly || q.MinCount != 3 {
			t.Fatalf("诊断配额应 isolated_only 且 ≥3: %+v", q)
		}
	}
	if len(prof.Adjudications) != 0 || c.ItemCount.Soft {
		t.Fatalf("2 点×3=6 ≤ 20 无冲突: %+v", prof.Adjudications)
	}
	// 地面真值：与冻结实现 digest 逐字节一致（跨实现指纹互验）
	if got := prof.Digest(); got != wantDiagDigest {
		t.Fatalf("diagnosis digest 与 Python 交叉验证不符:\n got %s\nwant %s", got, wantDiagDigest)
	}
}

func TestDiagnosisProfileConflictSoftTargetAdjudication(t *testing.T) {
	kps := []string{"math.a", "math.b", "math.c", "math.d", "math.e", "math.f", "math.g"}
	prof, err := DiagnosisProfile(DiagnosisInput{
		ProfileID: "diag-unit9", ProfileVersion: "1.0.0",
		Gradeband: GradebandM, KpCodes: kps,
		ItemCountRange: &[2]int{20, 20},
	})
	if err != nil {
		t.Fatalf("DiagnosisProfile: %v", err)
	}
	ic := prof.Constraints.ItemCount
	if !ic.Soft || ic.Max != 20 || ic.Min != 21 {
		t.Fatalf("已知冲突裁决错（soft 上限保留原值、下限上调至配额合计）: %+v", ic)
	}
	byID := map[string]Adjudication{}
	for _, a := range prof.Adjudications {
		byID[a.ConflictID] = a
	}
	adj, ok := byID["item_count_vs_kp_quota"]
	if !ok || adj.Decision != "soft_target" || adj.ConstraintA != "item_count.max" || adj.ConstraintB != "kp_quotas.min_count" {
		t.Fatalf("裁决留档错: %+v", prof.Adjudications)
	}
	if !strings.Contains(adj.Reason, "R-Z-03") || !strings.Contains(adj.Reason, "21") {
		t.Fatalf("裁决理由应含 R-Z-03 与 21: %s", adj.Reason)
	}
}

func TestCompileProfileConflictHardModeRaises(t *testing.T) {
	kps := []string{"math.a", "math.b", "math.c", "math.d", "math.e", "math.f", "math.g"}
	_, err := CompileProfile(CompileInput{
		ProfileID: "p", ProfileVersion: "1.0.0",
		Purpose: PurposeDiagnosis, Gradeband: GradebandM,
		KpCodes:            kps,
		PurposeOverlay:     map[string]any{"item_count_range": []any{20, 20}},
		AllowItemCountSoft: boolPtr(false),
	})
	if err == nil {
		t.Fatalf("严格模式应报 ProfileConflictError")
	}
	var pce *ProfileConflictError
	if !errors.As(err, &pce) || pce.ConflictID != "item_count_vs_kp_quota" {
		t.Fatalf("期望 item_count_vs_kp_quota，实际: %v", err)
	}
}

func TestProfileDigestDeterministicAndVersionSensitive(t *testing.T) {
	mk := func(version string) *AssemblyProfile {
		p, err := DiagnosisProfile(DiagnosisInput{
			ProfileID: "diag", ProfileVersion: version,
			Gradeband:      GradebandM,
			KpCodes:        []string{"math.a", "math.b"},
			ItemCountRange: &[2]int{20, 20},
		})
		if err != nil {
			t.Fatalf("DiagnosisProfile: %v", err)
		}
		return p
	}
	p1, p3 := mk("1.0.0"), mk("1.0.1")
	if p1.Digest() != p1.Digest() {
		t.Fatalf("同内容必同指纹")
	}
	if p1.Digest() == p3.Digest() {
		t.Fatalf("版本变化应改变指纹")
	}
}

// practiceWithDifficulty 构造带难度目标与学科 overlay 的练习 Profile（地面真值
// fixture；对应 Python test_assembly_profile.test_subject_overlay_assembly_constraints
// 与 test_target_p_correct_range_compiled 的合流参数）.
func practiceWithDifficulty(t *testing.T) *AssemblyProfile {
	t.Helper()
	prof, err := CompileProfile(CompileInput{
		ProfileID: "p", ProfileVersion: "1.0.0",
		Purpose: PurposePractice, Gradeband: GradebandM,
		KpCodes: []string{"math.a"},
		SubjectOverlay: map[string]any{
			"overlay_id":      "subject-math",
			"overlay_version": "1.0.0",
			"assembly_constraints": map[string]any{
				"require_gradient_monotone": true,
				"exposure_mutex": map[string]any{
					"same_template_different_paper": true,
					"cross_period_repeat":           false,
				},
				"content_mix": map[string]any{
					"new_learning_ratio": []any{0.4, 0.6},
					"review_ratio":       []any{0.2, 0.4},
				},
			},
		},
		PurposeOverlay: map[string]any{
			"item_count_range": []any{8, 15},
			"difficulty_target": map[string]any{
				"target_p_correct_range": []any{0.70, 0.90},
				"uncertainty_margin":     0.05,
			},
		},
	})
	if err != nil {
		t.Fatalf("CompileProfile: %v", err)
	}
	// 地面真值：与冻结实现 digest 逐字节一致（覆盖 content_mix/浮动 margin/
	// overlay_refs 全部序列化分支）
	if got := prof.Digest(); got != wantPracticeDigest {
		t.Fatalf("practice digest 与 Python 交叉验证不符:\n got %s\nwant %s", got, wantPracticeDigest)
	}
	return prof
}

func TestProfileDigestPracticeFixtureMatchesPython(t *testing.T) {
	practiceWithDifficulty(t)
}

// ────────────────────────────────────────────────────────────────────
// 二、确定性预算装填求解（heuristic.py；地面真值 = 冻结实现同 fixture 采样）
// ────────────────────────────────────────────────────────────────────

func TestAssemblePracticeSatisfiesConstraints(t *testing.T) {
	prof := practiceProfile(t, []string{"math.a", "math.b"}, [2]int{6, 10})
	pool := []CandidateItem{}
	for _, kp := range []string{"math.a", "math.b"} {
		for i := 0; i < 5; i++ {
			pool = append(pool, mkCandidate(fmt.Sprintf("%s%d", kp, i), []string{kp}, fP(0.40+0.05*float64(i))))
		}
	}
	res, err := Assemble(prof, pool, AssembleOptions{Seed: 7, SnapshotRef: "snap-1"})
	if err != nil {
		t.Fatalf("Assemble: %v", err)
	}
	if len(res.Items) < 6 || len(res.Items) > 10 {
		t.Fatalf("题量 %d 越出 [6,10]", len(res.Items))
	}
	for _, kp := range []string{"math.a", "math.b"} {
		n := 0
		for _, it := range res.Items {
			for _, k := range it.KpCodes {
				if k == kp {
					n++
				}
			}
		}
		if n < 1 {
			t.Fatalf("知识点 %s 配额未满足", kp)
		}
	}
	ps := make([]float64, 0, len(res.Items))
	for _, it := range res.Items {
		ps = append(ps, *it.PCorrectPrior)
	}
	for i := 1; i < len(ps); i++ {
		if ps[i-1] < ps[i] {
			t.Fatalf("序列梯度必须单调（由易到难）: %v", ps)
		}
	}
	if res.Seed != 7 || res.SnapshotRef != "snap-1" || res.ProfileVersion != "1.0.0" {
		t.Fatalf("确定性三要素留档错: %+v", res)
	}
	// 地面真值：冻结实现同 fixture 的选题序与 digest
	wantIDs := []string{"math.a4", "math.a3", "math.b2", "math.b1", "math.b0", "math.a0"}
	if !reflect.DeepEqual(idsOf(res.Items), wantIDs) {
		t.Fatalf("选题序与 Python 交叉验证不符:\n got %v\nwant %v", idsOf(res.Items), wantIDs)
	}
	wantDigest := "708d79c69d4aa2c2e511029c75dd283951efde6907acfd0b322184d59e509d2a"
	if res.SelectionDigest != wantDigest {
		t.Fatalf("selection_digest 与 Python 交叉验证不符:\n got %s\nwant %s", res.SelectionDigest, wantDigest)
	}
}

func TestAssembleGradientNonePriorGoesLast(t *testing.T) {
	prof := practiceProfile(t, []string{"math.a"}, [2]int{3, 5})
	pool := []CandidateItem{
		mkCandidate("a", []string{"math.a"}, fP(0.5)),
		mkCandidate("b", []string{"math.a"}, nil),
		mkCandidate("c", []string{"math.a"}, fP(0.8)),
	}
	res, err := Assemble(prof, pool, AssembleOptions{Seed: 1, SnapshotRef: "s"})
	if err != nil {
		t.Fatalf("Assemble: %v", err)
	}
	got := idsOf(res.Items)
	want := []string{"c", "a", "b"}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("无先验应排末尾:\n got %v\nwant %v", got, want)
	}
	wantDigest := "905a2b186675d3b34538b6c62cc8314db39db0a31379cb1bb3b3684429ad3b80"
	if res.SelectionDigest != wantDigest {
		t.Fatalf("digest 与 Python 交叉验证不符: %s", res.SelectionDigest)
	}
}

func TestAssembleTargetPCorrectRangeFiltersCandidates(t *testing.T) {
	prof, err := CompileProfile(CompileInput{
		ProfileID: "p", ProfileVersion: "1.0.0",
		Purpose: PurposePractice, Gradeband: GradebandM,
		KpCodes: []string{"math.a"},
		PurposeOverlay: map[string]any{
			"item_count_range": []any{2, 4},
			"difficulty_target": map[string]any{
				"target_p_correct_range": []any{0.70, 0.90},
				"uncertainty_margin":     0.10,
			},
		},
	})
	if err != nil {
		t.Fatalf("CompileProfile: %v", err)
	}
	pool := []CandidateItem{
		mkCandidate("in1", []string{"math.a"}, fP(0.75)), // 区间内
		mkCandidate("in2", []string{"math.a"}, fP(0.62)), // 区间外但加宽 [0.60,1.00] 内
		mkCandidate("out", []string{"math.a"}, fP(0.30)), // 加宽后仍区间外
	}
	res, err := Assemble(prof, pool, AssembleOptions{Seed: 1, SnapshotRef: "s"})
	if err != nil {
		t.Fatalf("Assemble: %v", err)
	}
	got := idsOf(res.Items)
	if !reflect.DeepEqual(got, []string{"in1", "in2"}) {
		t.Fatalf("冷启动加宽过滤错: %v", got)
	}
	wantDigest := "872897469f29d3d9662c9308aa56a8abbf1ee58f4cdf6ac4ab8c0c67576ecd94"
	if res.SelectionDigest != wantDigest {
		t.Fatalf("digest 与 Python 交叉验证不符: %s", res.SelectionDigest)
	}

	// 负例：只剩区间外候选 → 结构化报告
	inf := assembleInfeasible(t, prof, []CandidateItem{mkCandidate("out", []string{"math.a"}, fP(0.30))},
		AssembleOptions{Seed: 1, SnapshotRef: "s"})
	if inf.Report.DropReasons["p_correct_out_of_range"] != 1 {
		t.Fatalf("drop_reasons 错: %+v", inf.Report.DropReasons)
	}
	if inf.Report.PoolSize != 1 || inf.Report.EligibleSize != 0 {
		t.Fatalf("池规模留档错: %+v", inf.Report)
	}
	if len(inf.Report.Conflicts) != 2 ||
		inf.Report.Conflicts[0].ConstraintID != "kp_quota" ||
		inf.Report.Conflicts[0].KpCode == nil || *inf.Report.Conflicts[0].KpCode != "math.a" ||
		*inf.Report.Conflicts[0].Required != 1 ||
		inf.Report.Conflicts[1].ConstraintID != "item_count" || *inf.Report.Conflicts[1].Required != 2 {
		t.Fatalf("冲突序/字段与冻结实现不符: %+v", inf.Report.Conflicts)
	}
}

func TestAssembleMissingPriorDroppedWhenRangeRequired(t *testing.T) {
	prof, err := CompileProfile(CompileInput{
		ProfileID: "p", ProfileVersion: "1.0.0",
		Purpose: PurposePractice, Gradeband: GradebandM,
		KpCodes: []string{"math.a"},
		PurposeOverlay: map[string]any{
			"item_count_range":  []any{1, 2},
			"difficulty_target": map[string]any{"target_p_correct_range": []any{0.5, 0.9}},
		},
	})
	if err != nil {
		t.Fatalf("CompileProfile: %v", err)
	}
	inf := assembleInfeasible(t, prof, []CandidateItem{mkCandidate("x", []string{"math.a"}, nil)},
		AssembleOptions{Seed: 1, SnapshotRef: "s"})
	if inf.Report.DropReasons["missing_p_correct_prior"] != 1 {
		t.Fatalf("无先验应被淘汰并记录: %+v", inf.Report.DropReasons)
	}
}

func TestAssembleGroupSelectedAsUnitAndMaxSixEnforced(t *testing.T) {
	prof := practiceProfile(t, []string{"math.a"}, [2]int{3, 6})
	groupMembers := []CandidateItem{}
	for i := 0; i < 3; i++ {
		groupMembers = append(groupMembers, mkCandidate(fmt.Sprintf("g%d", i), []string{"math.a"}, fP(0.6), withGroupID("grp-1")))
	}
	res, err := Assemble(prof, groupMembers, AssembleOptions{Seed: 1, SnapshotRef: "s"})
	if err != nil {
		t.Fatalf("Assemble: %v", err)
	}
	gotIDs := idsOf(res.Items)
	sort.Strings(gotIDs)
	if !reflect.DeepEqual(gotIDs, []string{"g0", "g1", "g2"}) {
		t.Fatalf("题组应整体入选: %v", gotIDs)
	}

	// 负例：题组 >6 题报结构化冲突（R-Z-06）
	tooBig := []CandidateItem{}
	for i := 0; i < 7; i++ {
		tooBig = append(tooBig, mkCandidate(fmt.Sprintf("b%d", i), []string{"math.a"}, fP(0.6), withGroupID("grp-big")))
	}
	inf := assembleInfeasible(t, prof, tooBig, AssembleOptions{Seed: 1, SnapshotRef: "s"})
	found := false
	for _, c := range inf.Report.Conflicts {
		if c.ConstraintID == "max_items_per_group" {
			found = true
		}
	}
	if !found {
		t.Fatalf("应含 max_items_per_group 冲突: %+v", inf.Report.Conflicts)
	}
}

func TestAssembleInfeasibleKpQuotaStructuredReport(t *testing.T) {
	prof, err := DiagnosisProfile(DiagnosisInput{
		ProfileID: "diag", ProfileVersion: "1.0.0",
		Gradeband:      GradebandM,
		KpCodes:        []string{"math.a", "math.b"},
		ItemCountRange: &[2]int{6, 20},
	})
	if err != nil {
		t.Fatalf("DiagnosisProfile: %v", err)
	}
	pool := []CandidateItem{
		mkCandidate("a0", []string{"math.a"}, fP(0.5)),
		mkCandidate("a1", []string{"math.a"}, fP(0.5)),
		mkCandidate("a2", []string{"math.a"}, fP(0.5)),
		mkCandidate("b0", []string{"math.b"}, fP(0.5)), // math.b 只有 1 题孤立题，需 ≥3
	}
	inf := assembleInfeasible(t, prof, pool, AssembleOptions{Seed: 1, SnapshotRef: "snap-x"})
	r := inf.Report
	if r.SnapshotRef != "snap-x" || r.ProfileID != "diag" || r.Purpose != PurposeDiagnosis || r.PoolSize != 4 {
		t.Fatalf("报告三要素/池规模错: %+v", r)
	}
	quotaConflicts := []ConflictReason{}
	for _, c := range r.Conflicts {
		if c.ConstraintID == "kp_quota_isolated" {
			quotaConflicts = append(quotaConflicts, c)
		}
	}
	if len(quotaConflicts) != 1 || *quotaConflicts[0].KpCode != "math.b" ||
		*quotaConflicts[0].Required != 3 || *quotaConflicts[0].Available != 1 {
		t.Fatalf("kp_quota_isolated 结构化冲突错: %+v", quotaConflicts)
	}
}

func TestAssembleInfeasibleItemCountUnreachable(t *testing.T) {
	prof := practiceProfile(t, []string{"math.a"}, [2]int{5, 8})
	pool := []CandidateItem{
		mkCandidate("a0", []string{"math.a"}, fP(0.5)),
		mkCandidate("a1", []string{"math.a"}, fP(0.5)),
		mkCandidate("a2", []string{"math.a"}, fP(0.5)),
	}
	inf := assembleInfeasible(t, prof, pool, AssembleOptions{Seed: 1, SnapshotRef: "s"})
	found := false
	for _, c := range inf.Report.Conflicts {
		if c.ConstraintID == "item_count" {
			found = true
		}
	}
	if !found {
		t.Fatalf("题量下限不可达应报 item_count 冲突: %+v", inf.Report.Conflicts)
	}
}

func TestAssembleDeterministicReplaySameSeed(t *testing.T) {
	prof := practiceProfile(t, []string{"math.a", "math.b"}, [2]int{6, 8})
	pool := []CandidateItem{}
	for _, kp := range []string{"math.a", "math.b"} {
		for i := 0; i < 6; i++ {
			pool = append(pool, mkCandidate(fmt.Sprintf("%s%d", kp, i), []string{kp}, fP(0.4+0.04*float64(i))))
		}
	}
	r1, err1 := Assemble(prof, pool, AssembleOptions{Seed: 42, SnapshotRef: "snap"})
	r2, err2 := Assemble(prof, pool, AssembleOptions{Seed: 42, SnapshotRef: "snap"})
	if err1 != nil || err2 != nil {
		t.Fatalf("Assemble: %v %v", err1, err2)
	}
	if r1.SelectionDigest != r2.SelectionDigest || !reflect.DeepEqual(idsOf(r1.Items), idsOf(r2.Items)) {
		t.Fatalf("同种子重放必须同结果")
	}
}

func TestAssembleDifferentSeedChangesSelection(t *testing.T) {
	prof := practiceProfile(t, []string{"math.a"}, [2]int{4, 6})
	pool := []CandidateItem{}
	for i := 0; i < 10; i++ {
		pool = append(pool, mkCandidate(fmt.Sprintf("a%d", i), []string{"math.a"}, fP(0.5)))
	}
	r1, err := Assemble(prof, pool, AssembleOptions{Seed: 1, SnapshotRef: "s"})
	if err != nil {
		t.Fatalf("Assemble: %v", err)
	}
	r2, err := Assemble(prof, pool, AssembleOptions{Seed: 2, SnapshotRef: "s"})
	if err != nil {
		t.Fatalf("Assemble: %v", err)
	}
	if r1.SelectionDigest == r2.SelectionDigest {
		t.Fatalf("不同种子应产生不同选题")
	}
}

func TestAssembleSameTemplateMutexWithinPaper(t *testing.T) {
	prof := practiceProfile(t, []string{"math.a"}, [2]int{2, 4})
	pool := []CandidateItem{
		mkCandidate("v1", []string{"math.a"}, fP(0.6), withTemplate(strP("tpl-X"))),
		mkCandidate("v2", []string{"math.a"}, fP(0.6), withTemplate(strP("tpl-X"))),
		mkCandidate("v3", []string{"math.a"}, fP(0.6), withTemplate(strP("tpl-Y"))),
	}
	res, err := Assemble(prof, pool, AssembleOptions{Seed: 1, SnapshotRef: "s"})
	if err != nil {
		t.Fatalf("Assemble: %v", err)
	}
	// 地面真值：冻结实现同 fixture 选中 v2/v3（同卷内同母题至多一个）
	if !reflect.DeepEqual(idsOf(res.Items), []string{"v2", "v3"}) {
		t.Fatalf("同母题互斥错: %v", idsOf(res.Items))
	}
}

func TestAssembleCrossPeriodExclusionViaExposureSets(t *testing.T) {
	prof := practiceProfile(t, []string{"math.a"}, [2]int{1, 4})
	pool := []CandidateItem{
		mkCandidate("v1", []string{"math.a"}, fP(0.6), withTemplate(strP("tpl-X"))),
		mkCandidate("v2", []string{"math.a"}, fP(0.6), withTemplate(strP("tpl-Y"))),
		mkCandidate("v3", []string{"math.a"}, fP(0.6), withTemplate(strP("tpl-Z"))),
	}
	res, err := Assemble(prof, pool, AssembleOptions{
		Seed: 1, SnapshotRef: "s",
		ExcludedItemVersionIDs:     NewIDSet("v1"),
		ExcludedTemplateVersionIDs: NewIDSet("tpl-Y"),
	})
	if err != nil {
		t.Fatalf("Assemble: %v", err)
	}
	if !reflect.DeepEqual(idsOf(res.Items), []string{"v3"}) {
		t.Fatalf("曝光排除错: %v", idsOf(res.Items))
	}

	inf := assembleInfeasible(t, prof, pool, AssembleOptions{
		Seed: 1, SnapshotRef: "s",
		ExcludedItemVersionIDs:     NewIDSet("v1", "v2"),
		ExcludedTemplateVersionIDs: NewIDSet("tpl-Z"),
	})
	if inf.Report.DropReasons["exposed_item"] != 2 || inf.Report.DropReasons["exposed_template"] != 1 {
		t.Fatalf("淘汰计数与 Python 交叉验证不符: %+v", inf.Report.DropReasons)
	}
}

func TestAssembleDiagnosisIsolatedQuotaExcludesMultiKpItems(t *testing.T) {
	prof, err := DiagnosisProfile(DiagnosisInput{
		ProfileID: "diag", ProfileVersion: "1.0.0",
		Gradeband:      GradebandM,
		KpCodes:        []string{"math.a"},
		ItemCountRange: &[2]int{3, 10},
	})
	if err != nil {
		t.Fatalf("DiagnosisProfile: %v", err)
	}
	pool := []CandidateItem{
		mkCandidate("iso1", []string{"math.a"}, fP(0.5)),
		mkCandidate("iso2", []string{"math.a"}, fP(0.5)),
		// 多点题声明 all_required：合法但不算孤立题
		mkCandidate("multi1", []string{"math.a", "math.b"}, fP(0.5), withMode(KpSetModeAllRequired)),
	}
	inf := assembleInfeasible(t, prof, pool, AssembleOptions{Seed: 1, SnapshotRef: "s"})
	for _, c := range inf.Report.Conflicts {
		if c.ConstraintID == "kp_quota_isolated" && c.Available != nil && *c.Available == 2 {
			return
		}
	}
	t.Fatalf("多点题不应计入孤立配额（available=2）: %+v", inf.Report.Conflicts)
}

func TestAssembleDiagnosisRelationDeclarationCheck(t *testing.T) {
	prof, err := DiagnosisProfile(DiagnosisInput{
		ProfileID: "diag", ProfileVersion: "1.0.0",
		Gradeband:      GradebandM,
		KpCodes:        []string{"math.a"},
		ItemCountRange: &[2]int{3, 10},
	})
	if err != nil {
		t.Fatalf("DiagnosisProfile: %v", err)
	}
	bad := mkCandidate("bad", []string{"math.a", "math.b"}, fP(0.5), withMode(KpSetModeSingle))
	inf := assembleInfeasible(t, prof, []CandidateItem{bad}, AssembleOptions{Seed: 1, SnapshotRef: "s"})
	if inf.Report.DropReasons["relation_declaration_invalid"] != 1 {
		t.Fatalf("多点关系声明核验错: %+v", inf.Report.DropReasons)
	}
}

// 地面真值：诊断软目标化场景（7 知识点 × 3 = 21 > 约20题）的选题数、超出量
// 留档与 digest——运行冻结实现采样.
func TestAssembleDiagnosisSoftTargetAchievementRecorded(t *testing.T) {
	kps := []string{"math.a", "math.b", "math.c", "math.d", "math.e", "math.f", "math.g"}
	prof, err := DiagnosisProfile(DiagnosisInput{
		ProfileID: "diag", ProfileVersion: "1.0.0",
		Gradeband: GradebandM, KpCodes: kps,
		ItemCountRange: &[2]int{20, 20},
	})
	if err != nil {
		t.Fatalf("DiagnosisProfile: %v", err)
	}
	pool := []CandidateItem{}
	for _, kp := range kps {
		for i := 0; i < 3; i++ {
			pool = append(pool, mkCandidate(fmt.Sprintf("%s%d", kp, i), []string{kp}, fP(0.5)))
		}
	}
	res, err := Assemble(prof, pool, AssembleOptions{Seed: 1, SnapshotRef: "s"})
	if err != nil {
		t.Fatalf("Assemble: %v", err)
	}
	if len(res.Items) != 21 {
		t.Fatalf("软目标装填应达 21 题，实际 %d", len(res.Items))
	}
	ach, ok := res.SoftTargetAchievement["item_count"].(map[string]any)
	if !ok || ach["soft_max"] != 20 || ach["actual"] != 21 || ach["exceeded_by"] != 1 {
		t.Fatalf("soft_target_achievement 与 Python 交叉验证不符: %+v", res.SoftTargetAchievement)
	}
	softFound := false
	for _, a := range res.Adjudications {
		if a.Decision == "soft_target" {
			softFound = true
		}
	}
	if !softFound {
		t.Fatalf("裁决理由应随结果留档")
	}
	// 地面真值 digest（冻结实现 seed=1 同池采样）
	wantDigest := "5f1a9d13d16f087c1df278d8172dc90c26b595bd3530ce006722d781b887b438"
	if res.SelectionDigest != wantDigest {
		t.Fatalf("digest 与 Python 交叉验证不符:\n got %s\nwant %s", res.SelectionDigest, wantDigest)
	}
	wantFirst5 := []string{"math.d0", "math.e0", "math.f2", "math.b2", "math.d2"}
	if got := idsOf(res.Items[:5]); !reflect.DeepEqual(got, wantFirst5) {
		t.Fatalf("稳定哈希序与 Python 交叉验证不符:\n got %v\nwant %v", got, wantFirst5)
	}
}

func TestAssemblePurposeLicenseFiltersCandidates(t *testing.T) {
	prof, err := DiagnosisProfile(DiagnosisInput{
		ProfileID: "diag", ProfileVersion: "1.0.0",
		Gradeband:      GradebandM,
		KpCodes:        []string{"math.a"},
		ItemCountRange: &[2]int{3, 10},
	})
	if err != nil {
		t.Fatalf("DiagnosisProfile: %v", err)
	}
	pool := []CandidateItem{
		mkCandidate("ok1", []string{"math.a"}, fP(0.5)),
		mkCandidate("ok2", []string{"math.a"}, fP(0.5)),
		mkCandidate("ok3", []string{"math.a"}, fP(0.5)),
		mkCandidate("practice-only", []string{"math.a"}, fP(0.5), withPurposes([]string{PurposePractice})),
	}
	res, err := Assemble(prof, pool, AssembleOptions{Seed: 1, SnapshotRef: "s"})
	if err != nil {
		t.Fatalf("Assemble: %v", err)
	}
	for _, id := range idsOf(res.Items) {
		if id == "practice-only" {
			t.Fatalf("未许可 diagnosis 的题不应入选")
		}
	}
}

// ────────────────────────────────────────────────────────────────────
// 三、候选筛选与曝光账本（candidates.py / exposure.py；Memory 查询面）
// ────────────────────────────────────────────────────────────────────

func servingRow(vid, pack, gradeband string, objective map[string]any, params map[string]any) ServingRow {
	return ServingRow{
		PackID:            pack,
		ItemVersionID:     vid,
		ItemID:            "item-" + vid,
		TemplateVersionID: "tpl-" + vid,
		Objective:         objective,
		InteractionRef:    map[string]any{"interaction_id": "single_choice"},
		Lineage:           map[string]any{"params": params},
	}
}

func TestCandidateFromServingRow(t *testing.T) {
	objective := map[string]any{
		"kp_set":          []any{map[string]any{"dimension": "kp", "code": "math.a"}},
		"kp_set_mode":     "single",
		"gradeband":       "M",
		"cognitive_level": "understand",
	}
	params := map[string]any{"p_correct_prior": 0.6, "mix_tag": "new", "group_id": "grp-9"}
	c, err := CandidateFromServingRow(servingRow("iv-1", "subject-math", "M", objective, params))
	if err != nil {
		t.Fatalf("CandidateFromServingRow: %v", err)
	}
	if c.ItemVersionID != "iv-1" || c.ItemID != "item-iv-1" || c.TemplateVersionID == nil || *c.TemplateVersionID != "tpl-iv-1" {
		t.Fatalf("行解析错: %+v", c)
	}
	if len(c.KpCodes) != 1 || c.KpCodes[0] != "math.a" || !c.IsIsolated() {
		t.Fatalf("kp 解析/孤立判定错: %+v", c)
	}
	if c.PCorrectPrior == nil || *c.PCorrectPrior != 0.6 || c.MixTag == nil || *c.MixTag != "new" || c.GroupID == nil || *c.GroupID != "grp-9" {
		t.Fatalf("先验元数据解析错: %+v", c)
	}

	// 负例：kp_set 为空
	emptyObjective := map[string]any{"kp_set": []any{}, "gradeband": "M"}
	if _, err := CandidateFromServingRow(servingRow("iv-2", "subject-math", "M", emptyObjective, nil)); err == nil {
		t.Fatalf("kp_set 为空应报错")
	}

	// 负例：未知用途
	badObjective := map[string]any{
		"kp_set": []any{map[string]any{"code": "math.a"}}, "gradeband": "M",
	}
	badParams := map[string]any{"allowed_purposes": []any{"astral"}}
	if _, err := CandidateFromServingRow(servingRow("iv-3", "subject-math", "M", badObjective, badParams)); err == nil {
		t.Fatalf("allowed_purposes 含未知场景应报错")
	}

	// 缺省用途许可 = 全场景
	c2, err := CandidateFromServingRow(servingRow("iv-4", "subject-math", "M", objective, nil))
	if err != nil {
		t.Fatalf("CandidateFromServingRow: %v", err)
	}
	if !reflect.DeepEqual(c2.AllowedPurposes, []string{PurposePractice, PurposeDiagnosis, PurposeMeasurement}) {
		t.Fatalf("缺省用途许可应为全场景: %+v", c2.AllowedPurposes)
	}
}

func TestMemoryCandidateStoreFiltersPackAndGradeband(t *testing.T) {
	objectiveM := map[string]any{"kp_set": []any{map[string]any{"code": "math.a"}}, "gradeband": "M"}
	objectiveL := map[string]any{"kp_set": []any{map[string]any{"code": "math.a"}}, "gradeband": "L"}
	store := NewMemoryCandidateStore(
		servingRow("m1", "subject-math", "M", objectiveM, nil),
		servingRow("m2", "subject-math", "M", objectiveM, nil),
		servingRow("l1", "subject-math", "L", objectiveL, nil),
		servingRow("c1", "subject-chinese", "M", objectiveM, nil),
	)
	items, err := LoadCandidateItems(context.Background(), store, "subject-math", "M")
	if err != nil {
		t.Fatalf("LoadCandidateItems: %v", err)
	}
	if len(items) != 2 || items[0].ItemVersionID != "m1" || items[1].ItemVersionID != "m2" {
		t.Fatalf("候选池过滤错: %+v", items)
	}
}

func TestExposureMemoryDoubleTrack(t *testing.T) {
	store := NewMemoryExposureStore()
	itemA := mkCandidate("iv-a", []string{"math.a"}, fP(0.5))
	itemB := mkCandidate("iv-b", []string{"math.b"}, fP(0.5))
	ctx := context.Background()

	// 静态轨预留 + 查询
	n, err := store.RecordPaperExposures(ctx, PaperExposureInput{
		Channel: "web", SubjectPackID: "subject-math", Gradeband: GradebandM,
		WeekLabel: "2026-W36", Items: []CandidateItem{itemA, itemB}, PaperID: strP("paper-1"),
	})
	if err != nil || n != 2 {
		t.Fatalf("静态轨预留错: n=%d err=%v", n, err)
	}
	items, err := store.QueueExposedItemVersionIDs(ctx, "web", "subject-math", "2026-W36")
	if err != nil || len(items) != 2 || !items.Has("iv-a") {
		t.Fatalf("静态轨题目查询错: %v %v", items, err)
	}
	tpls, err := store.QueueExposedTemplateVersionIDs(ctx, "web", "subject-math", "2026-W36")
	if err != nil || len(tpls) != 2 {
		t.Fatalf("静态轨母题查询错: %v %v", tpls, err)
	}
	// 其他周队列不可见
	items2, _ := store.QueueExposedItemVersionIDs(ctx, "web", "subject-math", "2026-W37")
	if len(items2) != 0 {
		t.Fatalf("周队列隔离失效: %v", items2)
	}

	// 在线轨预留 + 查询（模板 nil 不入母题集）
	bare := mkCandidate("iv-bare", []string{"math.a"}, fP(0.5), withNoTemplate())
	n, err = store.RecordStudentExposures(ctx, StudentExposureInput{
		StudentAliasID: "alias-1", Purpose: PurposePractice, Items: []CandidateItem{itemA, bare},
	})
	if err != nil || n != 2 {
		t.Fatalf("在线轨预留错: n=%d err=%v", n, err)
	}
	stu, _ := store.StudentExposedItemVersionIDs(ctx, "alias-1")
	if len(stu) != 2 || !stu.Has("iv-bare") {
		t.Fatalf("在线轨题目查询错: %v", stu)
	}
	stuTpls, _ := store.StudentExposedTemplateVersionIDs(ctx, "alias-1")
	if len(stuTpls) != 1 || !stuTpls.Has("tpl-iv-a") {
		t.Fatalf("在线轨母题查询（NULL 模板不计）错: %v", stuTpls)
	}

	// 兜底语义：周队列 UNIQUE 重复登记报错（迁移 0010 的 23505 同义）
	if _, err := store.RecordPaperExposures(ctx, PaperExposureInput{
		Channel: "web", SubjectPackID: "subject-math", Gradeband: GradebandM,
		WeekLabel: "2026-W36", Items: []CandidateItem{itemA},
	}); err == nil {
		t.Fatalf("重复曝光应报 UNIQUE 冲突")
	}
	// 学生级 UNIQUE
	if _, err := store.RecordStudentExposures(ctx, StudentExposureInput{
		StudentAliasID: "alias-1", Purpose: PurposePractice, Items: []CandidateItem{itemA},
	}); err == nil {
		t.Fatalf("学生轨重复曝光应报 UNIQUE 冲突")
	}
	// gradeband 值域 ck
	if _, err := store.RecordPaperExposures(ctx, PaperExposureInput{
		Channel: "web", SubjectPackID: "subject-math", Gradeband: "X",
		WeekLabel: "2026-W38", Items: []CandidateItem{itemA},
	}); err == nil {
		t.Fatalf("gradeband 越域应报错")
	}
}

// ────────────────────────────────────────────────────────────────────
// 四、学段约束 overlay（gradeband_constraints.py）
// ────────────────────────────────────────────────────────────────────

func TestGradebandConstraintsTable(t *testing.T) {
	if got := ValidGradebands(); !reflect.DeepEqual(got, []string{"H", "L", "M"}) {
		t.Fatalf("学段值域错: %v", got)
	}
	c := GradebandConstraints()
	if c["L"]["max_items"] != 10 || c["L"]["time_limit_min"] != 15 || c["L"]["session_form"] != "game" {
		t.Fatalf("L 段政策错: %v", c["L"])
	}
	if c["M"]["max_items"] != 20 || c["M"]["session_form"] != "standard" {
		t.Fatalf("M 段政策错: %v", c["M"])
	}
	if c["H"]["max_items"] != 30 || c["H"]["time_limit_min"] != 60 {
		t.Fatalf("H 段政策错: %v", c["H"])
	}
}

func TestApplyGradebandOverlayInjectsLowBandPolicy(t *testing.T) {
	res, err := ApplyGradebandOverlay(map[string]any{}, GradebandL, nil, false)
	if err != nil {
		t.Fatalf("ApplyGradebandOverlay: %v", err)
	}
	if res.PaperSpec["max_items"] != 10 || res.PaperSpec["gradeband"] != GradebandL ||
		res.PaperSpec["time_limit_min"] != 15 || res.PaperSpec["session_form"] != "game" {
		t.Fatalf("L 段注入错: %v", res.PaperSpec)
	}
	want := map[string]any{"max_items": 10, "time_limit_min": 15, "session_form": "game"}
	if !reflect.DeepEqual(res.OverlayApplied, want) {
		t.Fatalf("overlay_applied 审计字段错: %v", res.OverlayApplied)
	}
	// M/H 常规形态
	resM, _ := ApplyGradebandOverlay(map[string]any{}, GradebandM, nil, false)
	if resM.PaperSpec["session_form"] != "standard" || resM.PaperSpec["max_items"] != 20 {
		t.Fatalf("M 段注入错: %v", resM.PaperSpec)
	}
	resH, _ := ApplyGradebandOverlay(map[string]any{}, GradebandH, nil, false)
	if resH.PaperSpec["max_items"] != 30 {
		t.Fatalf("H 段注入错: %v", resH.PaperSpec)
	}
}

func TestApplyGradebandOverlayConflicts(t *testing.T) {
	// 不可行示例：请求 20 题低段卷（地面真值冲突文案）
	res, err := ApplyGradebandOverlay(map[string]any{"item_count": 20}, GradebandL, nil, false)
	if err != nil {
		t.Fatalf("ApplyGradebandOverlay: %v", err)
	}
	if res.Feasible {
		t.Fatalf("20 题低段卷应不可行")
	}
	wantConflict := "L 段题量上限 10，请求 20 超出"
	if res.Conflict != wantConflict {
		t.Fatalf("冲突文案与冻结实现不符:\n got %q\nwant %q", res.Conflict, wantConflict)
	}
	// 注入仍发生（调用方可见目标约束）
	if res.PaperSpec["max_items"] != 10 {
		t.Fatalf("冲突时也应注入学段约束: %v", res.PaperSpec)
	}
	// 显式抛错模式
	if _, err := ApplyGradebandOverlay(map[string]any{"item_count": 20}, GradebandL, nil, true); err == nil {
		t.Fatalf("raise_on_conflict 应报错")
	} else if !strings.Contains(err.Error(), wantConflict) {
		t.Fatalf("错误信息应含冲突原因: %v", err)
	}
	// item_count_range 取上界
	res2, _ := ApplyGradebandOverlay(map[string]any{"item_count_range": []any{1, 25}}, GradebandL, nil, false)
	if res2.Feasible {
		t.Fatalf("range 上界 25 > 10 应不可行")
	}
	// 时长冲突
	res3, _ := ApplyGradebandOverlay(map[string]any{"time_limit_min": 90}, GradebandL, nil, false)
	if res3.Feasible || !strings.Contains(res3.Conflict, "段时长上限 15 分钟，请求 90 超出") {
		t.Fatalf("时长冲突错: %q", res3.Conflict)
	}
	// 未知学段
	if _, err := ApplyGradebandOverlay(map[string]any{}, "X", nil, false); err == nil {
		t.Fatalf("未知学段应报错")
	}
}

func TestApplyGradebandOverlayPackOverrides(t *testing.T) {
	// pack config 注入 overlay 覆盖核心默认；未覆盖字段保留默认
	res, err := ApplyGradebandOverlay(map[string]any{}, GradebandL,
		map[string]any{"max_items": 8, "session_duration_max_min": 12}, false)
	if err != nil {
		t.Fatalf("ApplyGradebandOverlay: %v", err)
	}
	if res.PaperSpec["max_items"] != 8 || res.PaperSpec["time_limit_min"] != 12 || res.PaperSpec["session_form"] != "game" {
		t.Fatalf("pack overlay 覆盖错: %v", res.PaperSpec)
	}
	// session_form_game=false → standard
	res2, _ := ApplyGradebandOverlay(map[string]any{}, GradebandL, map[string]any{"session_form_game": false}, false)
	if res2.PaperSpec["session_form"] != "standard" {
		t.Fatalf("pack overlay 关闭闯关错: %v", res2.PaperSpec)
	}
}

func TestBuildGradebandOverlayShape(t *testing.T) {
	ov, err := BuildGradebandOverlay(GradebandL, nil)
	if err != nil {
		t.Fatalf("BuildGradebandOverlay: %v", err)
	}
	if ov["overlay_id"] != "gradeband-l" || ov["overlay_version"] != "1.0.0" {
		t.Fatalf("overlay 元数据错: %v", ov)
	}
	if !reflect.DeepEqual(ov["item_count_range"], []any{1, 10}) {
		t.Fatalf("item_count_range 错: %v", ov["item_count_range"])
	}
	if ov["time_limit_max_minutes"] != 15 || ov["session_form"] != "game" {
		t.Fatalf("学段约束键错: %v", ov)
	}
	// 注入 CompileProfile 的 gradeband 维度
	prof, err := CompileProfile(CompileInput{
		ProfileID: "p", ProfileVersion: "1.0.0",
		Purpose: PurposePractice, Gradeband: GradebandL,
		KpCodes:          []string{"math.a"},
		GradebandOverlay: ov,
	})
	if err != nil {
		t.Fatalf("CompileProfile: %v", err)
	}
	if prof.Constraints.ItemCount.Max != 10 {
		t.Fatalf("gradeband overlay 应约束题量上限: %+v", prof.Constraints.ItemCount)
	}
}

// ────────────────────────────────────────────────────────────────────
// 五、听力 overlay（listening_overlay.py；地面真值 = 冻结实现同参采样）
// ────────────────────────────────────────────────────────────────────

func listeningProfile(t *testing.T) *AssemblyProfile {
	t.Helper()
	prof, err := CompileProfile(CompileInput{
		ProfileID: "test-profile", ProfileVersion: "1.0.0",
		Purpose: PurposePractice, Gradeband: GradebandM,
		KpCodes: []string{"eng.listen", "eng.vocab", "eng.grammar"},
	})
	if err != nil {
		t.Fatalf("CompileProfile: %v", err)
	}
	return prof
}

func TestListeningOverlayFeasibleInjectsConstraints(t *testing.T) {
	prof := listeningProfile(t)
	spec, err := NewListeningOverlaySpec("audio:bundle-001", nil, nil)
	if err != nil {
		t.Fatalf("NewListeningOverlaySpec: %v", err)
	}
	res, err := ApplyListeningOverlay(prof, 10, spec)
	if err != nil {
		t.Fatalf("ApplyListeningOverlay: %v", err)
	}
	if !res.Feasible || res.Overlay == nil || len(res.Conflicts) != 0 {
		t.Fatalf("素材充足应可行: %+v", res)
	}
	if res.Overlay.ListeningItemCountRange != [2]int{6, 8} {
		t.Fatalf("听力题量范围与 Python 交叉验证不符（ceil 20×0.30=6, floor 20×0.40=8）: %v", res.Overlay.ListeningItemCountRange)
	}
	// 地面真值：确定性 testlet_id（sha256("listening:audio:bundle-001")[:16]）
	wantTestlet := "testlet:listening:eba7160d82562f06"
	if res.Overlay.TestletID != wantTestlet {
		t.Fatalf("testlet_id 与 Python 交叉验证不符:\n got %s\nwant %s", res.Overlay.TestletID, wantTestlet)
	}
	if res.Overlay.Spec.RatioRange != [2]float64{ListeningRatioMin, ListeningRatioMax} || res.Overlay.Spec.Position != ListeningPosition {
		t.Fatalf("默认占比/位置错: %+v", res.Overlay.Spec)
	}
	// 确定性：同 audio_context_ref 同 testlet_id
	res2, _ := ApplyListeningOverlay(prof, 10, spec)
	if res2.Overlay.TestletID != wantTestlet {
		t.Fatalf("testlet_id 应确定性")
	}
	// 不同 audio_context_ref 不同 testlet_id
	specB, _ := NewListeningOverlaySpec("audio:B", nil, nil)
	resB, _ := ApplyListeningOverlay(prof, 10, specB)
	if resB.Overlay.TestletID == wantTestlet {
		t.Fatalf("不同音频上下文应产生不同 testlet_id")
	}
	// 自定义占比 20%–50% → [4, 10]
	custom, _ := NewListeningOverlaySpec("audio:X", &[2]float64{0.20, 0.50}, nil)
	resC, _ := ApplyListeningOverlay(prof, 10, custom)
	if resC.Overlay.ListeningItemCountRange != [2]int{4, 10} {
		t.Fatalf("自定义占比范围错: %v", resC.Overlay.ListeningItemCountRange)
	}
}

func TestListeningOverlayInfeasibleReturnsConflicts(t *testing.T) {
	prof := listeningProfile(t)
	spec, _ := NewListeningOverlaySpec("audio:bundle-001", nil, nil)
	// 20 题需至少 6 道听力（30%），只提供 3 道
	res, err := ApplyListeningOverlay(prof, 3, spec)
	if err != nil {
		t.Fatalf("ApplyListeningOverlay: %v", err)
	}
	if res.Feasible || res.Overlay != nil || len(res.Conflicts) != 1 {
		t.Fatalf("听力素材不足应不可行: %+v", res)
	}
	c := res.Conflicts[0]
	if c.ConstraintID != "listening_ratio_min" || c.Required == nil || *c.Required != 6 || c.Available == nil || *c.Available != 3 {
		t.Fatalf("冲突 required/available 与 Python 交叉验证不符: %+v", c)
	}
	if !strings.Contains(c.Detail, "听力题占比下限 30%") {
		t.Fatalf("冲突详情文案错: %s", c.Detail)
	}
	// 零素材
	res0, _ := ApplyListeningOverlay(prof, 0, spec)
	if res0.Feasible || *res0.Conflicts[0].Available != 0 {
		t.Fatalf("零听力候选应不可行: %+v", res0)
	}
	// 恰好满足下限 → 可行（边界）
	res6, _ := ApplyListeningOverlay(prof, 6, spec)
	if !res6.Feasible {
		t.Fatalf("恰好满足下限应可行: %+v", res6)
	}
	// spec 为 nil → 显式错误
	if _, err := ApplyListeningOverlay(prof, 10, nil); err == nil {
		t.Fatalf("spec 为 nil 应报错")
	}
	// ratio_range 非法
	if _, err := NewListeningOverlaySpec("a", &[2]float64{0.5, 0.3}, nil); err == nil {
		t.Fatalf("min > max 应报错")
	}
	if _, err := NewListeningOverlaySpec("a", &[2]float64{0.0, 0.4}, nil); err == nil {
		t.Fatalf("min = 0 应报错")
	}
	if _, err := NewListeningOverlaySpec("", nil, nil); err == nil {
		t.Fatalf("audio_context_ref 为空应报错")
	}
}

func TestMarkListeningTestletReordersAndRecomputesDigest(t *testing.T) {
	items := []CandidateItem{}
	for i := 0; i < 3; i++ {
		items = append(items, mkCandidate(fmt.Sprintf("item-plain-%d", i), []string{"eng.a"}, nil, withNoTemplate()))
	}
	for i := 0; i < 3; i++ {
		items = append(items, mkCandidate(fmt.Sprintf("item-listen-%d", i), []string{"eng.listen"}, nil, withNoTemplate()))
	}
	for i := 0; i < 4; i++ {
		items = append(items, mkCandidate(fmt.Sprintf("item-plain-%d", i+3), []string{"eng.a"}, nil, withNoTemplate()))
	}
	result := &AssemblyResult{
		Items:           items,
		SnapshotRef:     "snap-001",
		ProfileID:       "test-profile",
		ProfileVersion:  "1.0.0",
		Purpose:         PurposePractice,
		Seed:            42,
		SelectionDigest: "original-digest",
	}
	spec, _ := NewListeningOverlaySpec("audio:bundle-001", nil, nil)
	overlay := &ListeningOverlay{
		TestletID:               "testlet:listening:abc123",
		ListeningItemCountRange: [2]int{3, 8},
		Spec:                    *spec,
	}
	listeningIDs := NewIDSet("item-listen-0", "item-listen-1", "item-listen-2")

	out, err := MarkListeningTestlet(result, overlay, listeningIDs)
	if err != nil {
		t.Fatalf("MarkListeningTestlet: %v", err)
	}
	// 听力置卷首
	for i := 0; i < 3; i++ {
		if !listeningIDs.Has(out.Items[i].ItemVersionID) {
			t.Fatalf("前 3 题应为听力题: %v", idsOf(out.Items))
		}
	}
	// 非听力保持原序
	nonListening := []string{}
	for _, it := range out.Items {
		if !listeningIDs.Has(it.ItemVersionID) {
			nonListening = append(nonListening, it.ItemVersionID)
		}
	}
	wantPlain := []string{"item-plain-0", "item-plain-1", "item-plain-2", "item-plain-3", "item-plain-4", "item-plain-5", "item-plain-6"}
	if !reflect.DeepEqual(nonListening, wantPlain) {
		t.Fatalf("非听力原序错: %v", nonListening)
	}
	// testlet 标记
	for _, it := range out.Items {
		if listeningIDs.Has(it.ItemVersionID) != (it.GroupID != nil && *it.GroupID == "testlet:listening:abc123") {
			t.Fatalf("testlet 标记错: %v", it)
		}
	}
	// digest 重算（sha256 64 位十六进制）
	if out.SelectionDigest == "original-digest" || len(out.SelectionDigest) != 64 {
		t.Fatalf("digest 应重算: %s", out.SelectionDigest)
	}
	// 原结果不被改写（返回副本）
	if result.Items[0].GroupID != nil {
		t.Fatalf("入参 result 不应被改写")
	}

	// 负例：听力题数量不在 overlay 范围 → 显式错误（不静默放松）
	strict := &ListeningOverlay{TestletID: "t", ListeningItemCountRange: [2]int{6, 8}}
	if _, err := MarkListeningTestlet(result, strict, listeningIDs); err == nil {
		t.Fatalf("数量越界应报错")
	}
}

// ────────────────────────────────────────────────────────────────────
// 六、双向细目表（spec_table.py；ToJSON 地面真值 = 冻结实现 to_json 采样）
// ────────────────────────────────────────────────────────────────────

func specCell(content, cognitive string, count int, dmin, dmax float64) SpecCell {
	return SpecCell{ContentCode: content, CognitiveLevel: cognitive, TargetCount: count, DifficultyMin: dmin, DifficultyMax: dmax}
}

func uniqueSpecTable(t *testing.T) *SpecTable {
	t.Helper()
	st, err := NewSpecTable("spec-unique", "1.0.0", GradebandM, "g",
		[]SpecCell{specCell("math.u", "apply", 2, 0.40, 0.60)})
	if err != nil {
		t.Fatalf("NewSpecTable: %v", err)
	}
	return st
}

func TestSpecTableValidations(t *testing.T) {
	// 正例：任意层级深度 + Bloom 六级
	cells := []SpecCell{
		specCell("math", "apply", 1, 0.3, 0.6),
		specCell("math.nal", "apply", 1, 0.3, 0.6),
		specCell("math.nal.decimal", "apply", 1, 0.3, 0.6),
		specCell("math.nal.decimal.compare", "apply", 1, 0.3, 0.6),
		specCell("math.nal.decimal.compare.sign", "apply", 1, 0.3, 0.6),
	}
	levels := []string{"remember", "understand", "apply", "analyze", "evaluate", "create"}
	for i, lvl := range levels {
		cells = append(cells, specCell(fmt.Sprintf("kp.lvl%d", i), lvl, 1, 0.3, 0.6))
	}
	st, err := NewSpecTable("spec-ok", "1.0.0", GradebandM, "g", cells)
	if err != nil {
		t.Fatalf("合法细目表应通过: %v", err)
	}
	if st.TotalCount() != 11 {
		t.Fatalf("total_count 派生错: %d", st.TotalCount())
	}
	// difficulty_min == difficulty_max 合法（单点区间）
	if _, err := NewSpecTable("s", "1", GradebandM, "g", []SpecCell{specCell("math.a", "apply", 1, 0.5, 0.5)}); err != nil {
		t.Fatalf("单点区间应合法: %v", err)
	}
	// 负例族
	negatives := []struct {
		name  string
		cells []SpecCell
	}{
		{"total=0", []SpecCell{specCell("a", "apply", 0, 0.3, 0.6), specCell("b", "apply", 0, 0.3, 0.6)}},
		{"min>max", []SpecCell{specCell("math.a", "remember", 2, 0.7, 0.3)}},
		{"duplicate cell", []SpecCell{specCell("math.a", "apply", 1, 0.3, 0.6), specCell("math.a", "apply", 2, 0.3, 0.6)}},
		{"invalid cognitive", []SpecCell{specCell("math.a", "synthesis", 1, 0.3, 0.6)}},
		{"negative count", []SpecCell{specCell("math.a", "apply", -1, 0.3, 0.6)}},
		{"empty cells", nil},
	}
	for _, tc := range negatives {
		if _, err := NewSpecTable("spec-bad", "1.0.0", GradebandM, "g", tc.cells); err == nil {
			t.Fatalf("%s 应拒绝", tc.name)
		}
	}
	if _, err := NewSpecTable("spec-bad", "1.0.0", "X", "g", []SpecCell{specCell("math.a", "apply", 1, 0.3, 0.6)}); err == nil {
		t.Fatalf("gradeband 越域应拒绝")
	}
}

func TestSpecTableValidateAgainstGraphAndCellAt(t *testing.T) {
	st := uniqueSpecTable(t)
	// 正例：全部存在
	unknown, err := st.ValidateAgainstGraph([]string{"math.u", "math.v"})
	if err != nil || len(unknown) != 0 {
		t.Fatalf("全部存在时不应报错: %v %v", unknown, err)
	}
	// 负例：未知编码（排序输出）
	unknown, err = st.ValidateAgainstGraph([]string{"math.v"})
	if err == nil || !reflect.DeepEqual(unknown, []string{"math.u"}) {
		t.Fatalf("未知编码应报错并排序返回: %v %v", unknown, err)
	}
	// CellAt
	if st.CellAt("math.u", "apply") == nil || st.CellAt("math.u", "remember") != nil {
		t.Fatalf("CellAt 错")
	}
}

func TestSpecTableJSONGroundTruthAndRoundTrip(t *testing.T) {
	st := uniqueSpecTable(t)
	// 地面真值：冻结实现 st.to_json()（json.dumps sort_keys, ensure_ascii=False）逐字节
	want := `{"cells": [{"cognitive_level": "apply", "content_code": "math.u", "difficulty_max": 0.6, "difficulty_min": 0.4, "target_count": 2}], "gradeband": "M", "graph_release": "g", "spec_table_id": "spec-unique", "spec_table_version": "1.0.0"}`
	if got := st.ToJSON(); got != want {
		t.Fatalf("ToJSON 与 Python 交叉验证不符:\n got %s\nwant %s", got, want)
	}
	// 往返
	back, err := FromJSON(st.ToJSON())
	if err != nil || back.ToJSON() != want {
		t.Fatalf("JSON 往返失败: %v", err)
	}
	// YAML 往返
	y, err := st.ToYAML()
	if err != nil {
		t.Fatalf("ToYAML: %v", err)
	}
	backY, err := FromYAML(y)
	if err != nil || backY.TotalCount() != 2 {
		t.Fatalf("YAML 往返失败: %v", err)
	}
	// ToDict 形状
	d := st.ToDict()
	if d["spec_table_id"] != "spec-unique" || d["gradeband"] != GradebandM {
		t.Fatalf("ToDict 错: %v", d)
	}
}

// ────────────────────────────────────────────────────────────────────
// 七、测量卷求解（cpsat_solver.py 的语义等价实现；地面真值 = 冻结实现采样）
// ────────────────────────────────────────────────────────────────────

func mCand(vid, kp, cognitive string, p float64, opts ...func(*MeasurementCandidate)) MeasurementCandidate {
	c := MeasurementCandidate{
		ItemVersionID:     vid,
		KpCodes:           []string{kp},
		CognitiveLevel:    cognitive,
		PCorrect:          p,
		TemplateVersionID: strP("tpl-" + vid),
	}
	for _, opt := range opts {
		opt(&c)
	}
	return c
}

func oneCellSpecTable(t *testing.T, id, kp, cognitive string, count int, dmin, dmax float64) *SpecTable {
	t.Helper()
	st, err := NewSpecTable(id, "1.0.0", GradebandM, "g",
		[]SpecCell{specCell(kp, cognitive, count, dmin, dmax)})
	if err != nil {
		t.Fatalf("NewSpecTable: %v", err)
	}
	return st
}

func TestSolveUniqueSolutionMatchesPythonGroundTruth(t *testing.T) {
	st := oneCellSpecTable(t, "spec-unique", "math.u", "apply", 2, 0.40, 0.60)
	pool := []MeasurementCandidate{mCand("u-1", "math.u", "apply", 0.50), mCand("u-2", "math.u", "apply", 0.55)}
	res := Solve(st, pool, SolveOptions{Seed: 0})
	sol, ok := res.(*CpSatSolution)
	if !ok {
		t.Fatalf("可行案例应返回 CpSatSolution: %T", res)
	}
	if !sol.IsFeasible() || len(sol.Selected) != 2 {
		t.Fatalf("应恰好选 2 题: %+v", sol)
	}
	// 地面真值：唯一可行解下与 CP-SAT 选题与 cell_assignment 一致
	if !reflect.DeepEqual(sol.CellAssignment["math.u/apply"], []string{"u-1", "u-2"}) {
		t.Fatalf("cell_assignment 与 Python 交叉验证不符: %v", sol.CellAssignment)
	}
	wantDigest := "f4704965678de6800f1ec41bd589cd7dc0e952f13f34ca456907a38e80435fd7"
	if sol.SelectionDigest != wantDigest {
		t.Fatalf("selection_digest 与 Python 交叉验证不符:\n got %s\nwant %s", sol.SelectionDigest, wantDigest)
	}
	// 确定性重放
	res2 := Solve(st, pool, SolveOptions{Seed: 0})
	sol2 := res2.(*CpSatSolution)
	if sol2.SelectionDigest != wantDigest {
		t.Fatalf("同输入同种子必须同 digest")
	}
}

func TestSolveInfeasibleDifficultyBandMismatch(t *testing.T) {
	st := oneCellSpecTable(t, "spec-diff-mismatch", "math.x", "apply", 2, 0.70, 0.90)
	pool := []MeasurementCandidate{}
	for i := 0; i < 5; i++ {
		pool = append(pool, mCand(fmt.Sprintf("v%d", i), "math.x", "apply", 0.30+0.05*float64(i)))
	}
	res := Solve(st, pool, SolveOptions{Seed: 0})
	inf, ok := res.(*CpSatInfeasible)
	if !ok {
		t.Fatalf("难度全越域应不可行: %T", res)
	}
	if len(inf.Conflicts) < 1 || inf.Conflicts[0].ConstraintID != "cell_quota" ||
		inf.Conflicts[0].CellContentCode == nil || *inf.Conflicts[0].CellContentCode != "math.x" ||
		*inf.Conflicts[0].Required != 2 || *inf.Conflicts[0].Available != 0 {
		t.Fatalf("cell_quota 冲突字段与 Python 交叉验证不符: %+v", inf.Conflicts)
	}
	// 人类可读摘要（地面真值首行）
	if !strings.HasPrefix(inf.Summary(), "CP-SAT 不可行（1 条冲突）：") {
		t.Fatalf("summary 与冻结实现不符: %s", inf.Summary())
	}
}

func TestSolveInfeasiblePoolShortage(t *testing.T) {
	st := eightCellSpecTable(t)
	pool := feasibleEightCellPool(t)[:6]
	res := Solve(st, pool, SolveOptions{Seed: 0})
	inf, ok := res.(*CpSatInfeasible)
	if !ok {
		t.Fatalf("池不足应不可行: %T", res)
	}
	if inf.CandidatePoolSize != 6 || inf.SpecTableTotalCount != 12 {
		t.Fatalf("不可行报告规模错: %+v", inf)
	}
	quotaFound := false
	for _, c := range inf.Conflicts {
		if c.ConstraintID == "cell_quota" && c.Required != nil && c.Available != nil && *c.Required > *c.Available {
			quotaFound = true
		}
	}
	if !quotaFound {
		t.Fatalf("应含 cell_quota 缺额冲突: %+v", inf.Conflicts)
	}
}

func TestSolveGroupIntegrityAllOrNone(t *testing.T) {
	st := oneCellSpecTable(t, "spec-group", "math.g", "apply", 2, 0.40, 0.60)
	pool := []MeasurementCandidate{
		mCand("single-1", "math.g", "apply", 0.50),
		mCand("single-2", "math.g", "apply", 0.55),
		mCand("grp-a", "math.g", "apply", 0.45, func(c *MeasurementCandidate) { c.GroupID = strP("G1") }),
		mCand("grp-b", "math.g", "apply", 0.55, func(c *MeasurementCandidate) { c.GroupID = strP("G1") }),
	}
	res := Solve(st, pool, SolveOptions{Seed: 0})
	sol, ok := res.(*CpSatSolution)
	if !ok {
		t.Fatalf("应可行: %+v", res)
	}
	inGroup := 0
	for _, c := range sol.Selected {
		if c.GroupID != nil && *c.GroupID == "G1" {
			inGroup++
		}
	}
	if inGroup != 0 && inGroup != 2 {
		t.Fatalf("题组 G1 必须整体入选/排除，实际 %d", inGroup)
	}
}

func TestSolveGroupTooLargeForQuotaInfeasible(t *testing.T) {
	st := oneCellSpecTable(t, "spec-group-infeasible", "math.h", "apply", 2, 0.40, 0.60)
	pool := []MeasurementCandidate{
		mCand("grp-1", "math.h", "apply", 0.50, func(c *MeasurementCandidate) { c.GroupID = strP("G1") }),
		mCand("grp-2", "math.h", "apply", 0.50, func(c *MeasurementCandidate) { c.GroupID = strP("G1") }),
		mCand("grp-3", "math.h", "apply", 0.50, func(c *MeasurementCandidate) { c.GroupID = strP("G1") }),
	}
	res := Solve(st, pool, SolveOptions{Seed: 0})
	inf, ok := res.(*CpSatInfeasible)
	if !ok {
		t.Fatalf("3 题组 vs 配额 2 应不可行: %T", res)
	}
	// 冻结实现同 fixture：无 cell 配额缺口 → generic constraint_conflict
	if inf.Conflicts[0].ConstraintID != "constraint_conflict" {
		t.Fatalf("应记 generic conflict（题组整体入选）: %+v", inf.Conflicts)
	}
}

func TestSolveInfeasibleExposureMutex(t *testing.T) {
	st := oneCellSpecTable(t, "spec-exposure", "math.y", "apply", 2, 0.40, 0.60)
	pool := []MeasurementCandidate{}
	for i := 0; i < 3; i++ {
		pool = append(pool, mCand(fmt.Sprintf("v%d", i), "math.y", "apply", 0.50))
	}
	excluded := IDSet{}
	for _, c := range pool {
		excluded[c.ItemVersionID] = struct{}{}
	}
	res := Solve(st, pool, SolveOptions{Seed: 0, ExcludedItemVersionIDs: excluded})
	inf, ok := res.(*CpSatInfeasible)
	if !ok {
		t.Fatalf("全部排除应不可行: %T", res)
	}
	if *inf.Conflicts[0].Available != 0 || *inf.Conflicts[0].Required != 2 {
		t.Fatalf("曝光互斥缺额错: %+v", inf.Conflicts[0])
	}
}

// eightCellSpecTable 8 单元格共 12 题（4 kp × 2 认知层级；Python 测试同构 fixture）.
func eightCellSpecTable(t *testing.T) *SpecTable {
	t.Helper()
	cells := []SpecCell{}
	for _, kp := range []string{"math.a", "math.b", "math.c", "math.d"} {
		cells = append(cells,
			specCell(kp, "remember", 2, 0.50, 0.80),
			specCell(kp, "apply", 1, 0.30, 0.60),
		)
	}
	st, err := NewSpecTable("spec-cpsat-test", "1.0.0", GradebandM, "graph-math-2026q1", cells)
	if err != nil {
		t.Fatalf("NewSpecTable: %v", err)
	}
	return st
}

// feasibleEightCellPool 每 cell 3 个合格候选（24 个）.
func feasibleEightCellPool(t *testing.T) []MeasurementCandidate {
	t.Helper()
	pool := []MeasurementCandidate{}
	for idx, cell := range eightCellSpecTable(t).Cells {
		mid := (cell.DifficultyMin + cell.DifficultyMax) / 2
		for j := 0; j < 3; j++ {
			p := mid + 0.02*float64(j-1)
			if p < 0 {
				p = 0
			}
			if p > 1 {
				p = 1
			}
			pool = append(pool, mCand(fmt.Sprintf("item-%d-%d", idx, j), cell.ContentCode, cell.CognitiveLevel, p))
		}
	}
	return pool
}

func TestSolveEightCellsFeasibleAndCompliant(t *testing.T) {
	st := eightCellSpecTable(t)
	pool := feasibleEightCellPool(t)
	res := Solve(st, pool, SolveOptions{Seed: 42})
	sol, ok := res.(*CpSatSolution)
	if !ok {
		t.Fatalf("8 cell 可行案例应成功: %+v", res)
	}
	// 每 cell 题数 == target_count；难度全部合规
	pMap := map[string]float64{}
	for _, c := range pool {
		pMap[c.ItemVersionID] = c.PCorrect
	}
	for _, cell := range st.Cells {
		key := cellKeyOf(cell.ContentCode, cell.CognitiveLevel)
		vids := sol.CellAssignment[key]
		if len(vids) != cell.TargetCount {
			t.Fatalf("cell %s 入选 %d 题，target=%d", key, len(vids), cell.TargetCount)
		}
		for _, vid := range vids {
			if p := pMap[vid]; p < cell.DifficultyMin || p > cell.DifficultyMax {
				t.Fatalf("候选 %s p_correct=%v 越出 %s 难度区间", vid, p, key)
			}
		}
	}
	if len(sol.Selected) != st.TotalCount() {
		t.Fatalf("入选总数应 == total_count")
	}
	// 同输入同种子同输出
	sol2 := Solve(st, pool, SolveOptions{Seed: 42}).(*CpSatSolution)
	if sol2.SelectionDigest != sol.SelectionDigest {
		t.Fatalf("确定性重放失败")
	}
}

func TestMeasurementCandidateFromServingRow(t *testing.T) {
	objective := map[string]any{
		"kp_set":          []any{map[string]any{"dimension": "kp", "code": "math.z"}},
		"cognitive_level": "apply",
		"gradeband":       "M",
	}
	params := map[string]any{"p_correct_prior": 0.55, "group_id": "grp-7"}
	c, err := MeasurementCandidateFromServingRow(servingRow("iv-1", "subject-math", "M", objective, params))
	if err != nil {
		t.Fatalf("MeasurementCandidateFromServingRow: %v", err)
	}
	if c.ItemVersionID != "iv-1" || c.CognitiveLevel != "apply" || c.PCorrect != 0.55 ||
		c.GroupID == nil || *c.GroupID != "grp-7" || c.TemplateVersionID == nil {
		t.Fatalf("测量候选解析错: %+v", c)
	}
	if !c.MatchesCell("math.z", "apply", 0.4, 0.6) || c.MatchesCell("math.y", "apply", 0.4, 0.6) ||
		c.MatchesCell("math.z", "remember", 0.4, 0.6) || c.MatchesCell("math.z", "apply", 0.6, 0.9) {
		t.Fatalf("MatchesCell 语义错")
	}
	// 闭区间：p_correct == difficulty_max 合法
	if !c.MatchesCell("math.z", "apply", 0.55, 0.55) {
		t.Fatalf("闭区间端点应匹配")
	}
	// 负例族
	if _, err := MeasurementCandidateFromServingRow(servingRow("iv-2", "subject-math", "M",
		map[string]any{"kp_set": []any{}, "cognitive_level": "apply"}, params)); err == nil {
		t.Fatalf("kp_set 为空应报错")
	}
	noCog := map[string]any{"kp_set": []any{map[string]any{"code": "math.z"}}}
	if _, err := MeasurementCandidateFromServingRow(servingRow("iv-3", "subject-math", "M", noCog, params)); err == nil {
		t.Fatalf("缺 cognitive_level 应报错")
	}
	badCog := map[string]any{"kp_set": []any{map[string]any{"code": "math.z"}}, "cognitive_level": "synthesis"}
	if _, err := MeasurementCandidateFromServingRow(servingRow("iv-4", "subject-math", "M", badCog, params)); err == nil {
		t.Fatalf("cognitive_level 越域应报错")
	}
	okCog := map[string]any{"kp_set": []any{map[string]any{"code": "math.z"}}, "cognitive_level": "apply"}
	if _, err := MeasurementCandidateFromServingRow(servingRow("iv-5", "subject-math", "M", okCog, nil)); err == nil {
		t.Fatalf("缺 p_correct_prior 应报错")
	}
}

// ────────────────────────────────────────────────────────────────────
// 八、测量卷产出与合规校验（measurement_paper.py；地面真值 = 冻结实现采样）
// ────────────────────────────────────────────────────────────────────

func TestBuildMeasurementPaperAndVerifyCompliance(t *testing.T) {
	st := uniqueSpecTable(t)
	pool := []MeasurementCandidate{mCand("u-1", "math.u", "apply", 0.50), mCand("u-2", "math.u", "apply", 0.55)}
	sol := Solve(st, pool, SolveOptions{Seed: 0}).(*CpSatSolution)

	paper, err := BuildMeasurementPaper(sol, st, nil)
	if err != nil {
		t.Fatalf("BuildMeasurementPaper: %v", err)
	}
	// 地面真值：题序按 cell 序聚合 + 默认作答说明文案
	if !reflect.DeepEqual(paper.OrderedItemVersionIDs, []string{"u-1", "u-2"}) {
		t.Fatalf("题序与 Python 交叉验证不符: %v", paper.OrderedItemVersionIDs)
	}
	wantInstructions := "本测量卷共 2 题。请仔细阅读每题要求后作答：选择题答案填涂在作答卡对应题号处，填空题答案写在题内空位；每题作答完毕请检查，考试期间翻页无效，禁止交头接耳。"
	if paper.AnswerInstructions != wantInstructions {
		t.Fatalf("默认作答说明与 Python 交叉验证不符:\n got %s\nwant %s", paper.AnswerInstructions, wantInstructions)
	}
	if paper.SpecTableID != "spec-unique" || paper.Seed != 0 || len(paper.CellMappings) != 1 {
		t.Fatalf("溯源字段错: %+v", paper)
	}

	// 合规校验：产出卷偏差为 0（验收 #2）
	report := VerifyCompliance(paper, st)
	if !report.IsCompliant || report.TotalCountDeviation != 0 || len(report.Violations) != 0 {
		t.Fatalf("可行解应全合规: %+v", report)
	}

	// 负例：下游篡改 → count_mismatch + 偏差累加
	tampered := *paper
	tampered.CellMappings = []MeasurementCellMapping{paper.CellMappings[0]}
	tampered.CellMappings[0].ActualCount = 1
	rep2 := VerifyCompliance(&tampered, st)
	if rep2.IsCompliant || rep2.TotalCountDeviation != 1 || rep2.Violations[0].Kind != ViolationCountMismatch {
		t.Fatalf("篡改应检出 count_mismatch: %+v", rep2)
	}

	// 负例：难度越域 → difficulty_out_of_range
	drifty := *paper
	drifty.CellMappings = []MeasurementCellMapping{paper.CellMappings[0]}
	drifty.CellMappings[0].ItemPCorrects = []float64{0.9, 0.9} // 越出 [0.40, 0.60]
	rep3 := VerifyCompliance(&drifty, st)
	if rep3.IsCompliant || rep3.Violations[0].Kind != ViolationDifficultyOutOfRange {
		t.Fatalf("难度越域应检出: %+v", rep3)
	}

	// 负例：cell 缺失 → cell_missing + 偏差按 target 计
	empty := &MeasurementPaper{CellMappings: []MeasurementCellMapping{}}
	rep4 := VerifyCompliance(empty, st)
	if rep4.IsCompliant || rep4.TotalCountDeviation != 2 || rep4.Violations[0].Kind != ViolationCellMissing {
		t.Fatalf("cell 缺失应检出: %+v", rep4)
	}

	// 负例：不可行解不能组卷（§4.4 铁律）
	inf := Solve(oneCellSpecTable(t, "s2", "math.x", "apply", 2, 0.70, 0.90), []MeasurementCandidate{mCand("v0", "math.x", "apply", 0.3)}, SolveOptions{})
	if _, err := BuildMeasurementPaper(inf, st, nil); err == nil {
		t.Fatalf("不可行解组卷应报错")
	}
}
