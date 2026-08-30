package datastat

import (
	"errors"
	"math"
	"testing"
)

// 权重函数地面真值（与冻结实现 Python 逐值交叉验证，见波次报告）.
func TestWeightMeasured_Tiers(t *testing.T) {
	cases := []struct {
		n    int
		want float64
	}{
		{0, 0.0},
		{1, 0.0025},
		{50, 0.125},
		{100, 0.25},
		{199, 0.4975},
		{200, 0.5}, // 档界：两侧均 0.5
		{201, 0.5005},
		{500, 0.65},
		{600, 0.7},
		{999, 0.8995},
		{1000, 0.9}, // 档界：两侧均 0.9
		{1001, 0.9004987520807317},
		{1500, 0.9917915001376101}, // 验收 §2：n=1500 实测主导，误差 <1%
		{2000, 0.9993262053000914},
	}
	for _, c := range cases {
		if got := WeightMeasured(c.n); got != c.want {
			t.Errorf("WeightMeasured(%d) = %v，期望 %v（逐位一致）", c.n, got, c.want)
		}
	}
}

func TestWeightMeasured_Monotonic(t *testing.T) {
	prev := WeightMeasured(0)
	for n := 1; n <= 2000; n++ {
		w := WeightMeasured(n)
		if w < prev {
			t.Fatalf("w(%d)=%v < w(%d)=%v：违反单调递增", n, w, n-1, prev)
		}
		if w < 0 || w > 1 {
			t.Fatalf("w(%d)=%v 越出 [0,1]", n, w)
		}
		prev = w
	}
}

func TestShrink_MidTierFusionHandExample(t *testing.T) {
	// 手算：n=500 → w=0.65；prior=0.5, measured=0.8
	// shrunk = 0.65·0.8 + 0.35·0.5 = 0.695
	// CI（difficulty，se 用实测 p）：se = √(0.8·0.2/500) = √0.00032
	//   = 0.017888543819998316（Python 交叉验证）
	//   半宽 = 1.959964·se = 0.03506090189961918
	res, err := Shrink(ShrinkInput{
		Prior:        map[string]float64{"difficulty": 0.5},
		Measured:     map[string]*float64{"difficulty": wantFloat(0.8)},
		N:            500,
		PurposeScope: ScopeDiagnosis,
	})
	if err != nil {
		t.Fatalf("Shrink 失败：%v", err)
	}
	if res.WeightMeasured != 0.65 {
		t.Errorf("w = %v，期望 0.65", res.WeightMeasured)
	}
	assertApproxF(t, "shrunk difficulty", res.Params["difficulty"], 0.695, 1e-12)
	assertApproxF(t, "CI low", res.ConfidenceInterval["difficulty"][0], 0.6599390981003808, 1e-12)
	assertApproxF(t, "CI high", res.ConfidenceInterval["difficulty"][1], 0.7300609018996191, 1e-12)
	if res.Source != ShrinkageSource {
		t.Errorf("source = %q，期望 %q", res.Source, ShrinkageSource)
	}
	if res.MethodVersion != "shrinkage-v1" {
		t.Errorf("method_version = %q", res.MethodVersion)
	}
	if res.PurposeScope != ScopeDiagnosis || res.SampleSize != 500 {
		t.Errorf("scope/n = %q/%d", res.PurposeScope, res.SampleSize)
	}
}

func TestShrink_PriorDominance(t *testing.T) {
	// n=50 → w=0.125：shrunk = 0.125·0.8 + 0.875·0.5 = 0.5375（手算）
	res, err := Shrink(ShrinkInput{
		Prior:        map[string]float64{"difficulty": 0.5},
		Measured:     map[string]*float64{"difficulty": wantFloat(0.8)},
		N:            50,
		PurposeScope: ScopePractice,
	})
	if err != nil {
		t.Fatalf("Shrink 失败：%v", err)
	}
	assertApproxF(t, "先验主导融合", res.Params["difficulty"], 0.5375, 1e-12)
}

func TestShrink_MeasuredDominance(t *testing.T) {
	// n=1500 → w≈0.9918：与实测 0.8 误差 < 1%（验收 §2）
	res, err := Shrink(ShrinkInput{
		Prior:        map[string]float64{"difficulty": 0.5},
		Measured:     map[string]*float64{"difficulty": wantFloat(0.8)},
		N:            1500,
		PurposeScope: ScopeMeasurement,
	})
	if err != nil {
		t.Fatalf("Shrink 失败：%v", err)
	}
	assertApproxF(t, "实测主导融合", res.Params["difficulty"], 0.8, 0.01)
	assertApproxF(t, "w(1500)", res.WeightMeasured, 0.9917915001376101, 1e-15)
}

func TestShrink_ZeroN_PurePrior(t *testing.T) {
	// n=0：纯先验输出，CI 为全值域（不伪造精度）
	res, err := Shrink(ShrinkInput{
		Prior:        map[string]float64{"difficulty": 0.5},
		Measured:     map[string]*float64{"difficulty": wantFloat(0.8)},
		N:            0,
		PurposeScope: ScopePractice,
	})
	if err != nil {
		t.Fatalf("Shrink 失败：%v", err)
	}
	if res.WeightMeasured != 0 {
		t.Errorf("w = %v，期望 0", res.WeightMeasured)
	}
	if res.Params["difficulty"] != 0.5 {
		t.Errorf("shrunk = %v，期望精确 0.5（纯先验）", res.Params["difficulty"])
	}
	ci := res.ConfidenceInterval["difficulty"]
	if ci[0] != 0.0 || ci[1] != 1.0 {
		t.Errorf("CI = %v，期望全值域 [0,1]", ci)
	}
}

func TestShrink_MeasuredMissingKey_FallbackPrior(t *testing.T) {
	// 键并集：difficulty 仅先验（回退先验，CI 全值域）；
	// discrimination 仅实测（直接用实测）。
	// 手算 CI（discrimination，r=0.4, n=100）：se = √((1-0.16)/98)
	//   = 0.09258200997725514（Python 交叉验证）
	res, err := Shrink(ShrinkInput{
		Prior:        map[string]float64{"difficulty": 0.5},
		Measured:     map[string]*float64{"discrimination": wantFloat(0.4)},
		N:            100,
		PurposeScope: ScopePractice,
	})
	if err != nil {
		t.Fatalf("Shrink 失败：%v", err)
	}
	if res.Params["difficulty"] != 0.5 {
		t.Errorf("difficulty = %v，期望 0.5（回退先验）", res.Params["difficulty"])
	}
	if res.Params["discrimination"] != 0.4 {
		t.Errorf("discrimination = %v，期望 0.4（无先验直接用实测）", res.Params["discrimination"])
	}
	ci := res.ConfidenceInterval["difficulty"]
	if ci[0] != 0.0 || ci[1] != 1.0 {
		t.Errorf("difficulty CI = %v，期望 [0,1]（measured 缺失）", ci)
	}
	ci = res.ConfidenceInterval["discrimination"]
	assertApproxF(t, "disc CI low", ci[0], 0.21854259339693913, 1e-12)
	assertApproxF(t, "disc CI high", ci[1], 0.5814574066030609, 1e-12)
}

func TestShrink_Clip(t *testing.T) {
	// 先验越出 [0,1] → 裁剪到 1.0（值域裁剪）
	res, err := Shrink(ShrinkInput{
		Prior:        map[string]float64{"difficulty": 1.5},
		N:            0,
		PurposeScope: ScopePractice,
	})
	if err != nil {
		t.Fatalf("Shrink 失败：%v", err)
	}
	if res.Params["difficulty"] != 1.0 {
		t.Errorf("difficulty = %v，期望裁剪到 1.0", res.Params["difficulty"])
	}

	// 实测 p=1.5 → se 用裁剪后 p=1 → se=0 → CI [1,1]
	res, err = Shrink(ShrinkInput{
		Measured:     map[string]*float64{"difficulty": wantFloat(1.5)},
		N:            100,
		PurposeScope: ScopePractice,
	})
	if err != nil {
		t.Fatalf("Shrink 失败：%v", err)
	}
	ci := res.ConfidenceInterval["difficulty"]
	if ci[0] != 1.0 || ci[1] != 1.0 {
		t.Errorf("difficulty CI = %v，期望 [1,1]（p 裁剪后 se=0）", ci)
	}
}

func TestShrink_DiscriminationCISmallN(t *testing.T) {
	// n=2（≤2）→ discrimination se 无定义 → CI 全值域 [-1,1]（不伪造精度）
	res, err := Shrink(ShrinkInput{
		Measured:     map[string]*float64{"discrimination": wantFloat(0.5)},
		N:            2,
		PurposeScope: ScopePractice,
	})
	if err != nil {
		t.Fatalf("Shrink 失败：%v", err)
	}
	ci := res.ConfidenceInterval["discrimination"]
	if ci[0] != -1.0 || ci[1] != 1.0 {
		t.Errorf("discrimination CI = %v，期望 [-1,1]", ci)
	}
}

func TestShrink_UnknownKeyConservativeCI(t *testing.T) {
	// 未知键有实测值：保守 se = 1/√n；n=10 → se=0.31622776601683794
	// （Python 交叉验证）；CI 不裁剪（未知键无值域）
	res, err := Shrink(ShrinkInput{
		Prior:        map[string]float64{"error_hit_rate": 2.0},
		Measured:     map[string]*float64{"error_hit_rate": wantFloat(2.0)},
		N:            10,
		PurposeScope: ScopePractice,
	})
	if err != nil {
		t.Fatalf("Shrink 失败：%v", err)
	}
	ci := res.ConfidenceInterval["error_hit_rate"]
	assertApproxF(t, "未知键 CI low", ci[0], 1.3802049628065742, 1e-12)
	assertApproxF(t, "未知键 CI high", ci[1], 2.619795037193426, 1e-12)

	// 未知键无实测值（measured=None）：CI 全值域 (-Inf,+Inf)——与冻结实现
	// _param_ci 的 lo,hi 缺省一致（不伪造精度）
	res, err = Shrink(ShrinkInput{
		Prior:        map[string]float64{"error_hit_rate": 2.0},
		N:            10,
		PurposeScope: ScopePractice,
	})
	if err != nil {
		t.Fatalf("Shrink 失败：%v", err)
	}
	ci = res.ConfidenceInterval["error_hit_rate"]
	if !math.IsInf(ci[0], -1) || !math.IsInf(ci[1], 1) {
		t.Errorf("未知键无实测 CI = %v，期望 (-Inf,+Inf)", ci)
	}
}

func TestShrink_MetadataKeySkipped(t *testing.T) {
	// purpose_scope 元数据键不参与数值融合（冻结实现显式跳过）
	res, err := Shrink(ShrinkInput{
		Prior:        map[string]float64{"purpose_scope": 1.0, "difficulty": 0.5},
		N:            0,
		PurposeScope: ScopePractice,
	})
	if err != nil {
		t.Fatalf("Shrink 失败：%v", err)
	}
	if _, ok := res.Params["purpose_scope"]; ok {
		t.Error("purpose_scope 元数据键不应参与融合")
	}
	if res.Params["difficulty"] != 0.5 {
		t.Errorf("difficulty = %v", res.Params["difficulty"])
	}
}

func TestShrink_Errors(t *testing.T) {
	_, err := Shrink(ShrinkInput{PurposeScope: "exam"})
	if !errors.Is(err, ErrInvalidPurposeScope) {
		t.Errorf("越域 scope 应报 ErrInvalidPurposeScope，得到 %v", err)
	}
	_, err = Shrink(ShrinkInput{PurposeScope: ScopePractice, PriorScope: "diagnosis"})
	if !errors.Is(err, ErrShrinkScopeMismatch) {
		t.Errorf("先验 scope 不一致应报 ErrShrinkScopeMismatch，得到 %v", err)
	}
	_, err = Shrink(ShrinkInput{PurposeScope: ScopePractice, MeasuredScope: ScopeMeasurement})
	if !errors.Is(err, ErrShrinkScopeMismatch) {
		t.Errorf("实测 scope 不一致应报 ErrShrinkScopeMismatch，得到 %v", err)
	}
	_, err = Shrink(ShrinkInput{PurposeScope: ScopePractice, N: -1})
	if !errors.Is(err, ErrNegativeSampleSize) {
		t.Errorf("n<0 应报 ErrNegativeSampleSize，得到 %v", err)
	}
	// 一致的 scope 标记不报错
	if _, err = Shrink(ShrinkInput{
		PurposeScope: ScopePractice, PriorScope: ScopePractice,
		MeasuredScope: ScopePractice, N: 10,
	}); err != nil {
		t.Errorf("一致 scope 不应报错：%v", err)
	}
}

func TestShrink_Deterministic(t *testing.T) {
	in := ShrinkInput{
		Prior:        map[string]float64{"difficulty": 0.5, "discrimination": 0.3},
		Measured:     map[string]*float64{"difficulty": wantFloat(0.8), "error_hit_rate": wantFloat(0.1)},
		N:            500,
		PurposeScope: ScopePractice,
	}
	a, err := Shrink(in)
	if err != nil {
		t.Fatalf("Shrink 失败：%v", err)
	}
	b, err := Shrink(in)
	if err != nil {
		t.Fatalf("Shrink 失败：%v", err)
	}
	if len(a.Params) != 3 || len(b.Params) != 3 {
		t.Fatalf("键并集应为 3 维，得到 %d/%d", len(a.Params), len(b.Params))
	}
	for k, v := range a.Params {
		if b.Params[k] != v || a.ConfidenceInterval[k] != b.ConfidenceInterval[k] {
			t.Errorf("同输入不同输出（D6 违约）：%s", k)
		}
	}
	if math.Abs(a.Params["discrimination"]-0.3) > 1e-15 || math.Abs(a.Params["error_hit_rate"]-0.1) > 1e-15 {
		t.Errorf("仅实测键应直接取实测值：%v", a.Params)
	}
}
