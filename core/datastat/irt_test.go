package datastat

import (
	"math"
	mrand "math/rand"
	"testing"
)

// irtTestCase 是闭式评级转换的手算地面真值单测（与 sandbox/symbol_alignment.py
// 11 项用例对齐；系数 K = 400·1.7·log10(e) = 295.32024769421124）.
func TestIrtDifficultyToRating_Formula(t *testing.T) {
	const K = 295.32024769421124
	const eps = 1e-6
	cases := []struct {
		name                string
		discrimination      float64
		difficulty          float64
		want                float64
		wantSymmetricRating float64 // rating(a,b)+rating(a,-b) 应 = 3000
	}{
		{"b=0_a=1_anchor", 1.0, 0.0, 1500.0, 3000.0},
		{"b=1_a=1_harder", 1.0, 1.0, 1500.0 + K, 3000.0},
		{"b=-1_a=1_easier", 1.0, -1.0, 1500.0 - K, 3000.0},
		{"b=1_a_2_scale", 2.0, 1.0, 1500.0 + 2*K, 3000.0},
	}
	for _, c := range cases {
		got := IrtDifficultyToRating(c.discrimination, c.difficulty)
		if math.Abs(got-c.want) > eps {
			t.Errorf("%s: IrtDifficultyToRating(%v,%v) = %v, want %v",
				c.name, c.discrimination, c.difficulty, got, c.want)
		}
		// 解析对称性：奇部相消，rating(a,b)+rating(a,-b) = 3000.
		gotNeg := IrtDifficultyToRating(c.discrimination, -c.difficulty)
		if math.Abs(got+gotNeg-c.wantSymmetricRating) > eps {
			t.Errorf("%s: symmetry broken, rating(b)+rating(-b)=%v, want %v",
				c.name, got+gotNeg, c.wantSymmetricRating)
		}
	}
	// 单调性：固定 a=1，b 越大 rating 越高（题越难评级越高，与 DifficultyToRating 同向）.
	prev := math.Inf(-1)
	for b := -3.0; b <= 3.0; b += 0.5 {
		r := IrtDifficultyToRating(1.0, b)
		if r <= prev && !math.IsInf(prev, 0) {
			t.Errorf("单调性破坏：b=%v rating=%v <= prev=%v", b, r, prev)
		}
		prev = r
	}
}

// irtSpearman 计算 Spearman 等级相关系数（含并列平均排名）；长度 < 3 返回 NaN.
func irtSpearman(xs, ys []float64) float64 {
	n := len(xs)
	if n < 3 || n != len(ys) {
		return math.NaN()
	}
	rx := rankWithTies(xs)
	ry := rankWithTies(ys)
	// Pearson of ranks.
	mx, my := 0.0, 0.0
	for i := 0; i < n; i++ {
		mx += rx[i]
		my += ry[i]
	}
	mx /= float64(n)
	my /= float64(n)
	var sxx, syy, sxy float64
	for i := 0; i < n; i++ {
		dx := rx[i] - mx
		dy := ry[i] - my
		sxx += dx * dx
		syy += dy * dy
		sxy += dx * dy
	}
	if sxx == 0 || syy == 0 {
		return math.NaN()
	}
	return sxy / math.Sqrt(sxx*syy)
}

// rankWithTies 返回平均排名（1-based）.
func rankWithTies(vs []float64) []float64 {
	n := len(vs)
	type vi struct {
		v   float64
		idx int
	}
	arr := make([]vi, n)
	for i, v := range vs {
		arr[i] = vi{v, i}
	}
	// 按值升序排列.
	for i := 0; i < n; i++ {
		for j := i + 1; j < n; j++ {
			if arr[j].v < arr[i].v {
				arr[i], arr[j] = arr[j], arr[i]
			}
		}
	}
	ranks := make([]float64, n)
	i := 0
	for i < n {
		j := i
		for j < n && arr[j].v == arr[i].v {
			j++
		}
		// i..j-1 并列，平均排名 = (i+1 + j)/2.
		avg := (float64(i+1) + float64(j)) / 2.0
		for k := i; k < j; k++ {
			ranks[arr[k].idx] = avg
		}
		i = j
	}
	return ranks
}

// makeSyntheticRecords 生成合成作答记录：trueA/trueB 逐题，θ~N(0,1)，
// 作答 ~ Bernoulli(P_ij)。固定种子保证确定性.
func makeSyntheticRecords(rng *mrand.Rand, nStudents int, trueA, trueB []float64) []ResponseRecord {
	Q := len(trueA)
	records := make([]ResponseRecord, 0, nStudents*Q)
	for i := 0; i < nStudents; i++ {
		theta := randn(rng)
		sid := mkid("s", i)
		for j := 0; j < Q; j++ {
			p := irtP(trueA[j], trueB[j], theta)
			correct := 0.0
			if rng.Float64() < p {
				correct = 1.0
			}
			records = append(records, ResponseRecord{
				ItemVersionID:  mkid("it", j),
				StudentAliasID: sid,
				Correct:        correct,
			})
		}
	}
	return records
}

// mkid 生成短 id（避免 fmt 导入；保持确定性）.
func mkid(prefix string, i int) string {
	return prefix + itoa(i)
}

func itoa(i int) string {
	if i == 0 {
		return "0"
	}
	neg := i < 0
	if neg {
		i = -i
	}
	var buf [12]byte
	pos := len(buf)
	for i > 0 {
		pos--
		buf[pos] = byte('0' + i%10)
		i /= 10
	}
	if neg {
		pos--
		buf[pos] = '-'
	}
	return string(buf[pos:])
}

// randn 用 Box-Muller 生成 N(0,1)（math/rand 源）.
func randn(rng *mrand.Rand) float64 {
	u1 := rng.Float64()
	u2 := rng.Float64()
	if u1 == 0 {
		u1 = 1e-12
	}
	return math.Sqrt(-2.0*math.Log(u1)) * math.Cos(2.0*math.Pi*u2)
}

// TestCalibrate2PL_Recovery 是验收主测试：合成数据参数恢复 Spearman |ρ| > 0.9
// （区分度 a 与难度 b 分别计算）. 规模 400 生 × 40 题，seed=42.
func TestCalibrate2PL_Recovery(t *testing.T) {
	const (
		nStudents = 400
		nItems    = 40
		seed      = 42
		threshold = 0.90
	)
	rng := mrand.New(mrand.NewSource(seed))

	// 真值：a ∈ [0.4, 2.5]，b ∈ [-2.5, 2.5]，均匀散布（区分度高/低、难/易题混合）.
	trueA := make([]float64, nItems)
	trueB := make([]float64, nItems)
	for j := 0; j < nItems; j++ {
		trueA[j] = 0.4 + 2.1*float64(j)/float64(nItems-1)
		trueB[j] = -2.5 + 5.0*float64(j)/float64(nItems-1)
	}

	records := makeSyntheticRecords(rng, nStudents, trueA, trueB)
	stats := Calibrate2PL(records)
	if len(stats) != nItems {
		t.Fatalf("期望 %d 题结果，得到 %d", nItems, len(stats))
	}

	// 按 item id 回排到真值序（it0..it{n-1} 已升序）.
	gotA := make([]float64, nItems)
	gotB := make([]float64, nItems)
	for _, s := range stats {
		j := parseItemIdx(s.ItemVersionID)
		if j < 0 || j >= nItems {
			t.Fatalf("未知 item id: %s", s.ItemVersionID)
		}
		gotA[j] = s.Discrimination
		gotB[j] = s.Difficulty
	}

	rhoA := math.Abs(irtSpearman(gotA, trueA))
	rhoB := math.Abs(irtSpearman(gotB, trueB))
	t.Logf("参数恢复：区分度 |ρ| = %.4f，难度 |ρ| = %.4f（阈值 %.2f）", rhoA, rhoB, threshold)

	if math.IsNaN(rhoA) || rhoA < threshold {
		t.Errorf("区分度恢复不足：|ρ|=%.4f < %.2f", rhoA, threshold)
	}
	if math.IsNaN(rhoB) || rhoB < threshold {
		t.Errorf("难度恢复不足：|ρ|=%.4f < %.2f", rhoB, threshold)
	}
}

// parseItemIdx 解析 mkid("it", j) 中的序号；格式不符返回 -1.
func parseItemIdx(id string) int {
	if len(id) < 3 || id[:2] != "it" {
		return -1
	}
	n := 0
	for k := 2; k < len(id); k++ {
		c := id[k]
		if c < '0' || c > '9' {
			return -1
		}
		n = n*10 + int(c-'0')
	}
	return n
}

// TestCalibrate2PL_Direction 验证方向铁律：b 越大的题，估计难度与评级越高；
// 用 6 题宽间距真值构造稠密矩阵（120 生），确认排序与真值一致.
func TestCalibrate2PL_Direction(t *testing.T) {
	trueA := []float64{1.0, 1.0, 1.0, 1.0, 1.0, 1.0}
	trueB := []float64{-2.5, -1.5, -0.5, 0.5, 1.5, 2.5}
	rng := mrand.New(mrand.NewSource(recordsSeed()))
	records := makeSyntheticRecords(rng, 120, trueA, trueB)
	stats := Calibrate2PL(records)
	if len(stats) != 6 {
		t.Fatalf("期望 6 题，得到 %d", len(stats))
	}
	for i := 1; i < len(stats); i++ {
		if stats[i].Difficulty <= stats[i-1].Difficulty {
			t.Errorf("方向破坏：b 升序被破坏 it%d(b=%.3f) <= it%d(b=%.3f)",
				i, stats[i].Difficulty, i-1, stats[i-1].Difficulty)
		}
		if stats[i].Rating <= stats[i-1].Rating {
			t.Errorf("评级方向破坏：rating 升序被破坏 at it%d", i)
		}
	}
}

// TestCalibrate2PL_Empty / single 边界.
func TestCalibrate2PL_Empty(t *testing.T) {
	if got := Calibrate2PL(nil); got != nil {
		t.Errorf("空输入应返回 nil，得到 %v", got)
	}
	if got := Calibrate2PL([]ResponseRecord{}); got != nil {
		t.Errorf("空切片应返回 nil，得到 %v", got)
	}
}

// recordsSeed 是方向测试的固定种子（独立于恢复测试）.
func recordsSeed() int64 { return 20260901 }
