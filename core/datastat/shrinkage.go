// shrinkage.go 承载先验/实测贝叶斯收缩三档融合策略引擎（T-W4-001；Python
// 冻结实现 src/core/data/bayesian_shrinkage.py 的 Go 重锚定）。
//
// 架构 v2 §4.7「参数标定」融合档：source=prior_*（先验）与 source=measured_*
// （实测）按样本量 n 三档收缩融合，产出可写入 item_param 的融合参数。
//
// 三档策略（BRIEF S1 / 验收 §2）：
//   - n < 200        ：先验主导（实测信息不足，向先验收缩保稳）
//   - 200 ≤ n ≤ 1000 ：精度加权收缩（先验与实测按精度加权融合）
//   - n > 1000       ：实测主导（实测信息充足，贴近实测，误差 < 1%）
//
// 实现为可替换纯函数（D6 估计器可替换）；同 (prior, measured, n, scope)
// 输入必得同输出（D6 可重放）。
//
// 宪法 D5 分场景禁混估：purpose_scope 必填单值，越域 fail-closed；参数体若
// 自带场景标记（PriorScope/MeasuredScope，对应冻结实现读取 bag 内
// purpose_scope 元数据键）必须与融合 scope 一致，显式拒绝混估。
package datastat

import (
	"errors"
	"fmt"
	"math"
	"sort"
)

// 融合产出来源标识与方法版本（D6：策略迭代时递增；对应冻结实现
// SHRINKAGE_SOURCE / SHRINKAGE_METHOD_VERSION）.
const (
	ShrinkageSource        = "measured_shrinkage"
	ShrinkageMethodVersion = "shrinkage-v1"
)

// 三档边界（架构 v2 §4.7 默认档；对应冻结实现 TIER_PRIOR_MAX / TIER_MID_MAX /
// DECAY_TAU / Z_95）.
const (
	TierPriorMax = 200   // n < 200：先验主导
	TierMidMax   = 1000  // 200 ≤ n ≤ 1000：精度加权；>1000 实测主导
	DecayTau     = 200.0 // 第三档指数衰减常数（n=1500 时 w≈0.9918，误差 <1%）
	Z95          = 1.959964
)

// paramRanges 是参数值域（CI 裁剪用；对应冻结实现 _RANGE）.
var paramRanges = map[string][2]float64{
	"difficulty":     {0.0, 1.0},  // 正确率 p ∈ [0,1]
	"discrimination": {-1.0, 1.0}, // Pearson r ∈ [-1,1]
}

// ErrShrinkScopeMismatch 表示参数体携带的场景标记与融合 scope 不一致
// （D5 显式拒绝跨场景混估）.
var ErrShrinkScopeMismatch = errors.New(
	"datastat: 参数体携带 purpose_scope 与融合 scope 不一致（D5 禁止跨场景混估）")

// ErrNegativeSampleSize 表示实测样本量为负（item_param CHECK 非负整数）.
var ErrNegativeSampleSize = errors.New("datastat: n 不能为负")

// WeightMeasured 返回实测权重 w(n) ∈ [0,1]（对应冻结实现 _weight_measured），
// 三档分段连续单调递增。
//
// 为什么分段而非单一 n/(n+τ)：单一 τ 无法同时满足「n<200 先验主导」与
// 「n>1000 实测误差<1%」——前者要 τ 大、后者要 τ 相对 n 可忽略。分段在三档
// 语义边界处连续拼接，保证单调且无跳变：
//   - n ≤ 200      ：w = 0.5·n/200（线性 0→0.5，先验主导）
//   - 200 < n ≤ 1000：w = 0.5 + 0.4·(n-200)/800（线性 0.5→0.9，精度加权）
//   - n > 1000     ：w = 1 - 0.1·exp(-(n-1000)/DecayTau)（指数趋近 1.0）
//
// 边界连续性：n=200 两侧均 0.5；n=1000 两侧均 0.9。n ≤ 0 返回 0.
func WeightMeasured(n int) float64 {
	switch {
	case n <= 0:
		return 0.0
	case n <= TierPriorMax:
		return 0.5 * (float64(n) / TierPriorMax)
	case n <= TierMidMax:
		return 0.5 + 0.4*float64(n-TierPriorMax)/(TierMidMax-TierPriorMax)
	default:
		return 1.0 - 0.1*math.Exp(-float64(n-TierMidMax)/DecayTau)
	}
}

// clipParam 按参数键裁剪到合法值域（对应冻结实现 _clip；未知键不裁剪）.
func clipParam(value float64, key string) float64 {
	r, ok := paramRanges[key]
	if !ok {
		return value
	}
	return math.Max(r[0], math.Min(r[1], value))
}

// paramCI 计算单参数 95% 置信区间（对应冻结实现 _param_ci）。
//
// 为什么按 key 区分：difficulty（正确率）与 discrimination（相关系数）的标准
// 误公式不同，错误套用会给出误导性 CI；n≤0 或 measured 缺失时返回该参数的
// 全值域（最大不确定性，不伪造精度）：
//   - difficulty：二项 se = √(p(1-p)/n)，p 取 measured（实测是精度来源）
//   - discrimination：Pearson r 的 se ≈ √((1-r²)/(n-2))（n>2；否则全值域）
//   - 其它数值：保守 se = 1/√max(n,1)
func paramCI(key string, shrunk float64, measured *float64, n int) (float64, float64) {
	lo, hi := math.Inf(-1), math.Inf(1)
	if r, ok := paramRanges[key]; ok {
		lo, hi = r[0], r[1]
	}
	if n <= 0 || measured == nil {
		return lo, hi
	}
	var se float64
	switch key {
	case "difficulty":
		p := math.Min(math.Max(*measured, 0.0), 1.0)
		se = math.Sqrt(p * (1.0 - p) / float64(n))
	case "discrimination":
		if n <= 2 {
			return lo, hi // se = inf → 全值域（不伪造精度）
		}
		r := math.Min(math.Max(*measured, -1.0), 1.0)
		se = math.Sqrt((1.0 - r*r) / math.Max(float64(n-2), 1.0))
	default:
		se = 1.0 / math.Sqrt(math.Max(float64(n), 1.0))
	}
	if math.IsInf(se, 0) || math.IsNaN(se) {
		return lo, hi
	}
	return clipParam(shrunk-Z95*se, key), clipParam(shrunk+Z95*se, key)
}

// ShrinkInput 是 Shrink 的输入（对应冻结实现 shrink(prior, measured, n,
// purpose_scope) 的入参面）.
type ShrinkInput struct {
	// Prior 先验参数体（source=prior_* 的 params，如 {"difficulty": 0.5}）.
	Prior map[string]float64
	// PriorScope 先验参数行自带的场景标记（对应冻结实现读取 bag 内
	// purpose_scope 元数据键）；空 = 无标记。非空且与 PurposeScope 不一致
	// 时 fail-closed（D5 显式拒绝混估）.
	PriorScope string
	// Measured 实测参数体（source=measured_* 的 params）；值指针 nil 表示
	// 该键不可计算（如 CTT 区分度 n<2 时为 None），该键回退先验.
	Measured map[string]*float64
	// MeasuredScope 实测参数行自带的场景标记；语义同 PriorScope.
	MeasuredScope string
	// N 实测样本量；0 = 无实测，纯先验输出；负值报错.
	N int
	// PurposeScope 场景（practice/diagnosis/measurement），D5 必填单值.
	PurposeScope string
	// MethodVersion 融合方法版本（D6 可替换）；空 = 默认 shrinkage-v1.
	MethodVersion string
}

// ShrinkResult 是融合结果（对应冻结实现 shrink 返回 dict 的类型化重锚定，
// 与 ItemParam 列对齐，额外携带 weight_measured 与 confidence_interval
// 供报告层使用）.
type ShrinkResult struct {
	// Params 融合参数（键 = prior 与 measured 键并集；source 落 item_param
	// 时满足 CHECK 正则 measured_.+）.
	Params map[string]float64
	// Source 融合产出来源（实测侧：收缩是实测参数的精炼手段，先验只是借力）.
	Source string
	// PurposeScope / SampleSize / MethodVersion 与 ItemParam 列对齐.
	PurposeScope  string
	SampleSize    int
	MethodVersion string
	// WeightMeasured 本次融合的实测权重 w(n) ∈ [0,1].
	WeightMeasured float64
	// ConfidenceInterval 各参数 95% 置信区间 [low, high]（不可计算时为全值域）.
	ConfidenceInterval map[string][2]float64
}

// Shrink 先验/实测贝叶斯收缩三档融合（纯函数，无副作用；对应冻结实现
// bayesian_shrinkage.shrink）。
//
// 融合键 = prior 与 measured 的键并集（升序，确定性）；逐键：
//   - 实测缺失（nil）或 n=0 → 回退先验（双侧都无该维度则跳过）
//   - 无先验、有实测 → 直接用实测（无先验可借力）
//   - 两侧都有 → shrunk = w·measured + (1-w)·prior，再裁剪到值域
//
// 输出 Source=measured_shrinkage 落 item_param 时满足 CHECK 正则 measured_.+；
// 先验行与实测行各自只增不改，融合产生新行（D1/D6）。
// purpose_scope 元数据键不参与数值融合（冻结实现显式跳过；Go 类型化 map
// 不会携带该键，此处保留同键防御）.
func Shrink(in ShrinkInput) (ShrinkResult, error) {
	if err := validatePurposeScope(in.PurposeScope); err != nil {
		return ShrinkResult{}, err
	}
	// 显式拒绝混估：参数体若自带 scope 标记，必须与本次融合 scope 一致
	if in.PriorScope != "" && in.PriorScope != in.PurposeScope {
		return ShrinkResult{}, fmt.Errorf("%w: prior 携带 purpose_scope=%q 与融合 scope=%q 不一致（D5 禁止跨场景混估）",
			ErrShrinkScopeMismatch, in.PriorScope, in.PurposeScope)
	}
	if in.MeasuredScope != "" && in.MeasuredScope != in.PurposeScope {
		return ShrinkResult{}, fmt.Errorf("%w: measured 携带 purpose_scope=%q 与融合 scope=%q 不一致（D5 禁止跨场景混估）",
			ErrShrinkScopeMismatch, in.MeasuredScope, in.PurposeScope)
	}
	if in.N < 0 {
		return ShrinkResult{}, fmt.Errorf("%w: %d", ErrNegativeSampleSize, in.N)
	}

	methodVersion := in.MethodVersion
	if methodVersion == "" {
		methodVersion = ShrinkageMethodVersion
	}

	w := WeightMeasured(in.N)
	params := make(map[string]float64)
	ci := make(map[string][2]float64)

	// 以 prior 与 measured 的键并集为融合键（升序——先验可能定义实测未覆盖的维度）
	keySet := make(map[string]bool, len(in.Prior)+len(in.Measured))
	for key := range in.Prior {
		keySet[key] = true
	}
	for key := range in.Measured {
		keySet[key] = true
	}
	keys := make([]string, 0, len(keySet))
	for key := range keySet {
		keys = append(keys, key)
	}
	sort.Strings(keys)

	for _, key := range keys {
		if key == "purpose_scope" {
			continue // 元数据键不参与数值融合
		}
		pVal, hasPrior := in.Prior[key]
		mVal := in.Measured[key] // 键缺失与值 nil 同语义（实测不可计算）
		var shrunk float64
		switch {
		case mVal == nil || in.N <= 0:
			if !hasPrior {
				continue // 双侧都无该维度，跳过
			}
			shrunk = clipParam(pVal, key)
		case !hasPrior:
			shrunk = clipParam(*mVal, key)
		default:
			shrunk = clipParam(w**mVal+(1.0-w)*pVal, key)
		}
		params[key] = shrunk
		lo, hi := paramCI(key, shrunk, mVal, in.N)
		ci[key] = [2]float64{lo, hi}
	}

	return ShrinkResult{
		Params:             params,
		Source:             ShrinkageSource,
		PurposeScope:       in.PurposeScope,
		SampleSize:         in.N,
		MethodVersion:      methodVersion,
		WeightMeasured:     w,
		ConfidenceInterval: ci,
	}, nil
}
