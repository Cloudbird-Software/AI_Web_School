package datastat

import (
	"math"
	"strings"
	"testing"
	"time"
)

// 手算地面真值：3 学生 × 2 题（A: i1=1,i2=1；B: i1=1,i2=0；C: i1=0,i2=0）.
// 逐项手算：
//
//	i1: xs=[1,1,0] d=2/3；修正总分 ys=[1,0,0]；r=(1/3)/√((2/3)(2/3))=0.5
//	i2: xs=[1,0,0] d=1/3；ys=[1,1,0]；r=0.5（与 i1 对称）
func twoItemBatch() []ResponseRecord {
	return []ResponseRecord{
		{ItemVersionID: "i2", StudentAliasID: "A", Correct: 1},
		{ItemVersionID: "i1", StudentAliasID: "A", Correct: 1},
		{ItemVersionID: "i2", StudentAliasID: "B", Correct: 0},
		{ItemVersionID: "i1", StudentAliasID: "B", Correct: 1},
		{ItemVersionID: "i2", StudentAliasID: "C", Correct: 0},
		{ItemVersionID: "i1", StudentAliasID: "C", Correct: 0},
	}
}

func wantFloat(v float64) *float64 { return &v }

func assertApproxF(t *testing.T, name string, got, want, tol float64) {
	t.Helper()
	if math.Abs(got-want) > tol {
		t.Errorf("%s = %v，期望 %v（允差 %g）", name, got, want, tol)
	}
}

func assertNilF(t *testing.T, name string, got *float64) {
	t.Helper()
	if got != nil {
		t.Errorf("%s = %v，期望 nil（不可计算不伪造）", name, *got)
	}
}

func assertValueF(t *testing.T, name string, got *float64, want, tol float64) {
	t.Helper()
	if got == nil {
		t.Fatalf("%s = nil，期望 %v", name, want)
	}
	assertApproxF(t, name, *got, want, tol)
}

// ────────────────────────────────────────────────────────────────────
// ComputeCtt（冻结实现 ctt.compute_ctt）
// ────────────────────────────────────────────────────────────────────

// 任务卡示例：10 题（记录）8 对 → p=0.8.
func TestComputeCtt_DifficultyTenOfEight(t *testing.T) {
	records := make([]ResponseRecord, 0, 10)
	for i := 0; i < 10; i++ {
		records = append(records, ResponseRecord{
			ItemVersionID:  "iv-10",
			StudentAliasID: "s" + string(rune('a'+i)),
			Correct:        boolToFloat(i < 8),
		})
	}
	stats := ComputeCtt(records)
	if len(stats) != 1 {
		t.Fatalf("题数 = %d，期望 1", len(stats))
	}
	s := stats[0]
	if s.SampleSize != 10 {
		t.Errorf("sample_size = %d，期望 10", s.SampleSize)
	}
	// 8.0/10 = 0.8（浮点精确）
	if s.Difficulty != 0.8 {
		t.Errorf("difficulty = %v，期望精确 0.8", s.Difficulty)
	}
	// 单题批：学生总分=本题得分 → 修正总分全 0 → 零方差 → 不可计算
	assertNilF(t, "discrimination", s.Discrimination)
}

func boolToFloat(b bool) float64 {
	if b {
		return 1
	}
	return 0
}

func TestComputeCtt_TwoItemsHandExample(t *testing.T) {
	stats := ComputeCtt(twoItemBatch())
	if len(stats) != 2 {
		t.Fatalf("题数 = %d，期望 2", len(stats))
	}
	// 按 item_version_id 升序（确定性）
	if stats[0].ItemVersionID != "i1" || stats[1].ItemVersionID != "i2" {
		t.Fatalf("排序 = [%s, %s]，期望 [i1, i2]", stats[0].ItemVersionID, stats[1].ItemVersionID)
	}
	assertApproxF(t, "i1.difficulty", stats[0].Difficulty, 2.0/3.0, 1e-15)
	assertValueF(t, "i1.discrimination", stats[0].Discrimination, 0.5, 1e-12)
	assertApproxF(t, "i2.difficulty", stats[1].Difficulty, 1.0/3.0, 1e-15)
	assertValueF(t, "i2.discrimination", stats[1].Discrimination, 0.5, 1e-12)
	if stats[0].SampleSize != 3 || stats[1].SampleSize != 3 {
		t.Errorf("sample_size = [%d, %d]，期望 [3, 3]", stats[0].SampleSize, stats[1].SampleSize)
	}
}

func TestComputeCtt_Empty(t *testing.T) {
	stats := ComputeCtt(nil)
	if len(stats) != 0 {
		t.Errorf("空输入应返回空列表，得到 %d 项", len(stats))
	}
}

func TestComputeCtt_SingleRecord(t *testing.T) {
	stats := ComputeCtt([]ResponseRecord{{ItemVersionID: "iv", StudentAliasID: "s", Correct: 1}})
	if len(stats) != 1 {
		t.Fatalf("题数 = %d，期望 1", len(stats))
	}
	if stats[0].Difficulty != 1 {
		t.Errorf("difficulty = %v，期望 1", stats[0].Difficulty)
	}
	// n<2：区分度不可计算
	assertNilF(t, "discrimination", stats[0].Discrimination)
}

func TestComputeCtt_ZeroVarianceDiscrimination(t *testing.T) {
	// 全员答对 → xs 零方差 → 区分度不可计算
	records := []ResponseRecord{
		{ItemVersionID: "iv", StudentAliasID: "a", Correct: 1},
		{ItemVersionID: "iv", StudentAliasID: "b", Correct: 1},
		{ItemVersionID: "iv", StudentAliasID: "c", Correct: 1},
	}
	stats := ComputeCtt(records)
	assertNilF(t, "discrimination（xs 零方差）", stats[0].Discrimination)
	if stats[0].Difficulty != 1 {
		t.Errorf("difficulty = %v，期望 1", stats[0].Difficulty)
	}
}

// ────────────────────────────────────────────────────────────────────
// ComputeDiscrimination（冻结实现 ctt.compute_discrimination，T-W4-047）
// ────────────────────────────────────────────────────────────────────

func TestComputeDiscrimination_MinSampleGate(t *testing.T) {
	batch := twoItemBatch() // i1 有 3 条记录
	// n=3 < minSample=4 → None（样本不足不伪造）
	assertNilF(t, "n<minSample", ComputeDiscrimination(batch, "i1", 4))
	// n=3 ≥ minSample=3 → 修正点二列 = 0.5（与 ComputeCtt 一致）
	assertValueF(t, "n≥minSample", ComputeDiscrimination(batch, "i1", 3), 0.5, 1e-12)
}

func TestComputeDiscrimination_DefaultThresholdIs30(t *testing.T) {
	if CTTMinSampleDefault != 30 {
		t.Errorf("CTTMinSampleDefault = %d，期望 30（T-W4-047）", CTTMinSampleDefault)
	}
	if CTTMethodVersion != "ctt-v1" || CTTSource != "measured_ctt" {
		t.Errorf("方法版本/来源标识漂移：%q/%q", CTTMethodVersion, CTTSource)
	}
}

func TestComputeDiscrimination_UsesWholeBatchTotals(t *testing.T) {
	// 学生总分 = 批内全部题之和（含非 key 题）——用 2 题批验证与手算一致
	assertValueF(t, "批内总分口径", ComputeDiscrimination(twoItemBatch(), "i2", 2), 0.5, 1e-12)
}

func TestPearson_PerfectCorrelation(t *testing.T) {
	// xs=[0,1,2], ys=[0,2,4] → r=1（同向完全相关）
	r := pearson([]float64{0, 1, 2}, []float64{0, 2, 4})
	assertValueF(t, "r=1", r, 1.0, 1e-12)
	// ys 反向 → r=-1（负值如实返回）
	r = pearson([]float64{0, 1, 2}, []float64{4, 2, 0})
	assertValueF(t, "r=-1", r, -1.0, 1e-12)
}

// ────────────────────────────────────────────────────────────────────
// 报告（冻结实现 ctt_report.py：α / SEM / 难度分布 / 警示）
// ────────────────────────────────────────────────────────────────────

func TestSampleVariance(t *testing.T) {
	// [1,2,3]：均值 2，ss=2，样本方差 = 2/2 = 1（手算）
	assertValueF(t, "var[1,2,3]", sampleVariance([]float64{1, 2, 3}), 1.0, 1e-15)
	assertNilF(t, "n<2", sampleVariance([]float64{5}))
	assertNilF(t, "零方差", sampleVariance([]float64{5, 5, 5}))
}

func TestCronbachAlpha_HandExample(t *testing.T) {
	// 手算：题方差各 1/3；总分 [2,1,0] 方差 1 → α = 2·(1-2/3) = 2/3
	matrix := [][]float64{{1, 1}, {1, 0}, {0, 0}}
	alpha := cronbachAlpha(matrix)
	assertValueF(t, "α", alpha, 2.0/3.0, 1e-12)
}

func TestCronbachAlpha_FailClosed(t *testing.T) {
	cases := []struct {
		name   string
		matrix [][]float64
	}{
		{"n<2", [][]float64{{1, 1}}},
		{"k<2", [][]float64{{1}, {0}}},
		{"行不等长", [][]float64{{1, 1}, {1}}},
		{"总分零方差", [][]float64{{1, 1}, {1, 1}, {1, 1}}},
	}
	for _, c := range cases {
		if got := cronbachAlpha(c.matrix); got != nil {
			t.Errorf("%s：α = %v，期望 nil", c.name, *got)
		}
	}
}

func TestBinDifficulty(t *testing.T) {
	cases := []struct {
		p    float64
		band string
		ok   bool
	}{
		{0.0, "hard", true},
		{0.29, "hard", true},
		{0.3, "somewhat_hard", true},
		{0.5, "medium", true},
		{0.7, "somewhat_easy", true},
		{0.9, "easy", true},
		{1.0, "easy", true}, // 最后一桶含 1.0
		{1.1, "", false},
		{-0.1, "", false},
	}
	for _, c := range cases {
		band, ok := binDifficulty(c.p)
		if band != c.band || ok != c.ok {
			t.Errorf("binDifficulty(%v) = (%q,%v)，期望 (%q,%v)", c.p, band, ok, c.band, c.ok)
		}
	}
}

func TestGenerateCttReport_FullHandExample(t *testing.T) {
	now := time.Date(2026, 8, 30, 8, 0, 0, 0, time.UTC)
	rep := GenerateCttReport(twoItemBatch(), "paper-1", CTTMinSampleDefault, now)

	if rep.PaperID != "paper-1" {
		t.Errorf("paper_id = %q", rep.PaperID)
	}
	if rep.SampleSize != 3 || rep.ItemCount != 2 {
		t.Errorf("n/k = %d/%d，期望 3/2", rep.SampleSize, rep.ItemCount)
	}
	if !rep.SmallSampleWarning {
		t.Error("n=3 < 30 应标记小样本警示（验收 #2）")
	}
	if len(rep.Notes) == 0 || !strings.HasPrefix(rep.Notes[0], "样本不足，结果仅供参考（n=") {
		t.Errorf("首条备注应含小样本警示文案，得到 %q", rep.Notes)
	}
	// α = 2/3（手算），SEM = √(2/3)·√(1/3) = 1/√3 = 0.5773502691896258（冻结实现一致）
	assertValueF(t, "α", rep.CronbachAlpha, 2.0/3.0, 1e-12)
	assertValueF(t, "SEM", rep.Sem, 0.5773502691896258, 1e-12)
	// 每题统计与 ComputeCtt 一致（验收 #3）
	if len(rep.ItemStats) != 2 {
		t.Fatalf("item_stats 长度 = %d，期望 2", len(rep.ItemStats))
	}
	assertApproxF(t, "i1.difficulty", rep.ItemStats[0].Difficulty, 2.0/3.0, 1e-15)
	assertValueF(t, "i1.discrimination", rep.ItemStats[0].Discrimination, 0.5, 1e-12)
	// 难度分布：d1=2/3→medium，d2=1/3→somewhat_hard（手算）
	want := map[string]int{"hard": 0, "somewhat_hard": 1, "medium": 1, "somewhat_easy": 0, "easy": 0}
	for _, b := range rep.DifficultyDistribution {
		if b.Count != want[b.Band] {
			t.Errorf("分布桶 %s = %d，期望 %d", b.Band, b.Count, want[b.Band])
		}
	}
	if len(rep.DifficultyDistribution) != 5 {
		t.Errorf("分布桶数 = %d，期望 5", len(rep.DifficultyDistribution))
	}
	if !rep.GeneratedAt.Equal(now) {
		t.Errorf("generated_at = %v，期望 %v", rep.GeneratedAt, now)
	}
	// 区分度可计算 → 不应有「全部不可计算」备注
	for _, note := range rep.Notes {
		if strings.HasPrefix(note, "所有题目") {
			t.Errorf("区分度可计算时不应出现备注 %q", note)
		}
	}
}

func TestGenerateCttReport_Empty(t *testing.T) {
	rep := GenerateCttReport(nil, "paper-empty", CTTMinSampleDefault, time.Time{})
	if rep.SampleSize != 0 || rep.ItemCount != 0 {
		t.Errorf("n/k = %d/%d，期望 0/0", rep.SampleSize, rep.ItemCount)
	}
	assertNilF(t, "α（空数据）", rep.CronbachAlpha)
	assertNilF(t, "SEM（空数据）", rep.Sem)
	if !rep.SmallSampleWarning {
		t.Error("空数据应标记小样本警示")
	}
	// notes：小样本警示 + 无作答数据（两条）
	if len(rep.Notes) != 2 {
		t.Fatalf("notes = %v，期望 2 条", rep.Notes)
	}
	if rep.Notes[1] != "无作答数据：无法计算 α / SEM（n=0 或 k=0）。" {
		t.Errorf("第二条备注 = %q", rep.Notes[1])
	}
	if len(rep.ItemStats) != 0 {
		t.Errorf("item_stats 应为空，得到 %d 项", len(rep.ItemStats))
	}
}

func TestGenerateCttReport_SingleItemAlphaNotComputable(t *testing.T) {
	// 2 学生 × 1 题：k=1 → α 不可计算；修正总分全 0 → 区分度全 nil
	records := []ResponseRecord{
		{ItemVersionID: "iv", StudentAliasID: "a", Correct: 1},
		{ItemVersionID: "iv", StudentAliasID: "b", Correct: 0},
	}
	rep := GenerateCttReport(records, "p", CTTMinSampleDefault, time.Time{})
	assertNilF(t, "α（k=1）", rep.CronbachAlpha)
	assertNilF(t, "SEM（α 不可计算）", rep.Sem)
	if len(rep.Notes) != 3 {
		t.Fatalf("notes = %v，期望 3 条", rep.Notes)
	}
	if !strings.HasPrefix(rep.Notes[0], "样本不足") {
		t.Errorf("第一条备注 = %q", rep.Notes[0])
	}
	if !strings.HasPrefix(rep.Notes[1], "Cronbach's α 不可计算") {
		t.Errorf("第二条备注 = %q", rep.Notes[1])
	}
	if rep.Notes[2] != "所有题目区分度均不可计算（n<2 或题分零方差）；不伪造 0。" {
		t.Errorf("第三条备注 = %q", rep.Notes[2])
	}
	// d=0.5 → medium
	if rep.DifficultyDistribution[2].Band != "medium" || rep.DifficultyDistribution[2].Count != 1 {
		t.Errorf("medium 桶 = %+v，期望 count=1", rep.DifficultyDistribution[2])
	}
}

func TestGenerateCttReport_DedupLastWins(t *testing.T) {
	// 同一学生同题多条记录 → 取最后一条（防御性去重）
	records := []ResponseRecord{
		{ItemVersionID: "iv", StudentAliasID: "a", Correct: 0},
		{ItemVersionID: "iv", StudentAliasID: "a", Correct: 1},
	}
	rep := GenerateCttReport(records, "p", 1, time.Time{})
	if rep.SampleSize != 1 || rep.ItemCount != 1 {
		t.Errorf("n/k = %d/%d，期望 1/1", rep.SampleSize, rep.ItemCount)
	}
	if len(rep.ItemStats) != 1 || rep.ItemStats[0].SampleSize != 2 {
		t.Fatalf("item_stats 应含 2 条原始记录，得到 %+v", rep.ItemStats)
	}
	if rep.ItemStats[0].Difficulty != 0.5 {
		t.Errorf("difficulty = %v，期望 0.5（0+1 两条记录）", rep.ItemStats[0].Difficulty)
	}
	// α：n=1 < 2 → 不可计算
	assertNilF(t, "α（n=1）", rep.CronbachAlpha)
}

func TestGenerateCttReport_NoWarningAboveMinSample(t *testing.T) {
	rep := GenerateCttReport(twoItemBatch(), "p", 2, time.Time{})
	if rep.SmallSampleWarning {
		t.Error("n=3 ≥ minSample=2 不应警示")
	}
	if len(rep.Notes) != 0 {
		t.Errorf("不应有备注，得到 %v", rep.Notes)
	}
}
