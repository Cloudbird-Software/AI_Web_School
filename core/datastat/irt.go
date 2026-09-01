// irt.go 承载 IRT 2PL 参数估计核（W3 S8 / C 流标定第二波；Python 冻结实现
// 无直接对应——本核是 girth.twopl_mml 的 Go 原生重锚定，纯函数、零新依赖）.
//
// 模型：2PL logistic，P(θ) = 1 / (1 + exp(-D·a·(θ - b)))，D = 1.7。
//   - a = 区分度（discrimination），a > 0；值越大题越能区分高低能力学生。
//   - b = 难度（difficulty），θ = b 时 P = 0.5；b 越大题越难。
//
// 估计法：边际最大似然（MML）+ Bock-Aitkin EM 交替估计。
//   - 能力 θ 的先验取 N(0,1)，用 41 点 Gauss-Hermite 求积离散化。
//   - E 步：按当前题目参数计算每个学生在各求积点的后验权重，汇总
//     期望统计量 n_k（点 k 的期望学生数）与 r_jk（题 j 在点 k 的期望答对数）。
//   - M 步：逐题用 Newton-Raphson 最大化期望完全数据对数似然，更新 (a, b)。
//   - 外层 EM 迭代至参数最大变化 < tol。
//
// 符号对齐铁律（reports/cpu-benchmark-irt-fsrs.md §3）：
//
//	girth Difficulty b 与本仓库 DifficultyToRating 方向一致（均「越大越难」），
//	无需翻转。闭式评级转换 rating = 1500 + K·a·b，
//	K = 400·1.7·log10(e) = 295.32024769421124。
//
// 分场景禁混估（宪法 D5）：本核消费已按场景过滤的 []ResponseRecord，结构上
// 不存在跨场景聚合路径；source 标签由调用方（标定 CLI）按「真实/合成」分账。
//
// 产出对齐 item_param 列（迁移 0013）：
//   - params.difficulty      = b（IRT 难度，越大越难）
//   - params.discrimination  = a（IRT 区分度）
//   - params.rating          = 闭式 Elo 难度评级
//   - sample_size            = 参与估计的作答事件数 n
//   - source                 = measured_irt（真实）/ measured_sim_irt（合成）
//   - method_version         = irt-2pl-v1
package datastat

import (
	"math"
	"sort"
)

// 估计方法版本与实测/合成来源标识（D6：方法迭代时递增）.
const (
	IRTMethodVersion = "irt-2pl-v1"
	IRTSource        = "measured_irt"     // 真实学生作答标定
	IRTSourceSim     = "measured_sim_irt" // 合成学生作答标定（严格分账）
)

// 2PL / EM 算法超参（与 girth 默认量级对齐）.
const (
	irtD          = 1.7  // logistic 缩放常数（与 girth/rpy2 一致）
	irtQuadPoints = 41   // Gauss-Hermite 求积点数
	irtMaxIter    = 200  // EM 最大外层迭代
	irtTol        = 1e-4 // EM 收敛阈值（参数最大绝对变化）
	irtNewtonIter = 6    // M 步 Newton 迭代数
	irtAMin       = 0.05 // 区分度下界（防退化）
	irtAMax       = 5.0  // 区分度上界（girth 默认截断量级）
	irtBMin       = -6.0 // 难度下界
	irtBMax       = 6.0  // 难度上界
	irtLogPEps    = 1e-12
)

// 区分度先验（girth 对 a 用 log-normal 先验；等价于对 α=log a 用正态先验）.
// α ~ N(irtPriorAlphaMean, irtPriorAlphaStd²)。先验把区分度拉向 1.0 附近，
// 稳定 MML 中区分度不可识别导致的恢复退化；std 较宽以免过度收缩.
const (
	irtPriorAlphaMean = 0.0 // log a 先验均值 → a 先验中位数 1.0
	irtPriorAlphaStd  = 0.5 // log a 先验标准差（弱信息，girth 量级）
)

// irtQuadNodes / irtQuadWeights 是 N(0,1) 先验的 41 点 Gauss-Hermite 求积
// 节点与权重（由 sandbox 脚本从 numpy.polynomial.hermite.hermgauss 生成，
// 经 √2 伸缩 + 1/√π 归一化；权重和 = 1，二阶矩 ≈ 1）.
var (
	irtQuadNodes = [irtQuadPoints]float64{
		-11.614937254337466, -10.647536786319336, -9.843433249157997,
		-9.123069907984474, -8.45609908326939, -7.82688200405387,
		-7.226022663732789, -6.6473084707471894, -6.086349164878476,
		-5.539884440458125, -5.0053966834041255, -4.480878331594007,
		-3.9646840280332665, -3.455432217780993, -2.9519370163811907,
		-2.4531593459070486, -1.9581707119772915, -1.4661254572959668,
		-0.9762387671800494, -0.48776856931943463, 0.0,
		0.48776856931943463, 0.9762387671800494, 1.4661254572959668,
		1.9581707119772915, 2.4531593459070486, 2.9519370163811907,
		3.455432217780993, 3.9646840280332665, 4.480878331594007,
		5.0053966834041255, 5.539884440458125, 6.086349164878476,
		6.6473084707471894, 7.226022663732789, 7.82688200405387,
		8.45609908326939, 9.123069907984474, 9.843433249157997,
		10.647536786319336, 11.614937254337466,
	}
	irtQuadWeights = [irtQuadPoints]float64{
		2.257863956583067e-30, 8.308558938782494e-26, 2.7468912285222457e-22,
		2.3263841455871576e-19, 7.655982291966891e-17, 1.2203348742027814e-14,
		1.0778183949358868e-12, 5.769853428092103e-11, 1.994794756757333e-09,
		4.667347708107311e-08, 7.65818607798233e-07, 9.058608622432976e-06,
		7.89471931950464e-05, 0.0005158014443431869, 0.002561642428649784,
		0.00977790273820827, 0.02893721174793441, 0.0668476593544664,
		0.1211489170115105, 0.17284953105060155, 0.19454502775360036,
		0.17284953105060155, 0.1211489170115105, 0.0668476593544664,
		0.02893721174793441, 0.00977790273820827, 0.002561642428649784,
		0.0005158014443431869, 7.89471931950464e-05, 9.058608622432976e-06,
		7.65818607798233e-07, 4.667347708107311e-08, 1.994794756757333e-09,
		5.769853428092103e-11, 1.0778183949358868e-12, 1.2203348742027814e-14,
		7.655982291966891e-17, 2.3263841455871576e-19, 2.7468912285222457e-22,
		8.308558938782494e-26, 2.257863956583067e-30,
	}
)

// irtRatingK = Scale · D · log10(e) = 295.32024769421124（闭式评级系数）.
func irtRatingK() float64 { return Scale * irtD * math.Log10(math.E) }

// IrtDifficultyToRating 将 IRT 2PL 参数 (a, b) 转为 Elo 难度评级（闭式，纯算术）.
//
// rating = 1500 + K·a·b，K = 400·1.7·log10(e)。
// 方向：b 越大（题越难）→ rating 越高，与 DifficultyToRating 一致（无符号翻转）.
func IrtDifficultyToRating(discrimination, difficulty float64) float64 {
	return BaseRating + irtRatingK()*discrimination*difficulty
}

// ItemIrtStats 是单题 IRT 2PL 估计量（与 item_param 列对齐）.
//
// Difficulty：b（IRT 难度，越大越难；girth 约定，与 DifficultyToRating 同向）.
// Discrimination：a（区分度，> 0）.
// Rating：闭式 Elo 难度评级 = IrtDifficultyToRating(a, b).
type ItemIrtStats struct {
	ItemVersionID  string
	SampleSize     int
	Difficulty     float64
	Discrimination float64
	Rating         float64
}

// irtP 计算 2PL 答对概率 P(θ) = 1/(1+exp(-D·a·(θ-b)))，输出裁剪到
// [irtLogPEps, 1-irtLogPEps] 避免 log(0)；指数饱和区直接截断防溢出.
func irtP(a, b, theta float64) float64 {
	x := irtD * a * (theta - b)
	if x >= 30.0 {
		return 1.0 - irtLogPEps
	}
	if x <= -30.0 {
		return irtLogPEps
	}
	p := 1.0 / (1.0 + math.Exp(-x))
	if p < irtLogPEps {
		return irtLogPEps
	}
	if p > 1.0-irtLogPEps {
		return 1.0 - irtLogPEps
	}
	return p
}

// irtQitem 计算单题 j 的期望完全数据对数似然 Q_j(a,b) 加 α=log a 先验
// （用于 M 步回溯线搜索判定步长是否提升目标）.
func irtQitem(j int, aa, bb float64, n []float64, rjk [][]float64, priorPrec float64) float64 {
	q := 0.0
	for k := 0; k < irtQuadPoints; k++ {
		p := irtP(aa, bb, irtQuadNodes[k])
		if rjk[j][k] > 0 {
			q += rjk[j][k] * math.Log(p)
		}
		diff := n[k] - rjk[j][k]
		if diff > 0 {
			q += diff * math.Log(1.0-p)
		}
	}
	// 先验 α~N(μ,σ²)：log prior = -(α-μ)²/(2σ²) = -0.5·prec·(α-μ)².
	alpha := math.Log(aa)
	q += -0.5 * priorPrec * (alpha - irtPriorAlphaMean) * (alpha - irtPriorAlphaMean)
	return q
}

// Calibrate2PL 对一批作答记录做 IRT 2PL MML 估计（纯函数，无副作用）.
//
// 算法（Bock-Aitkin EM）：
//  1. 构建学生×题作答矩阵（缺失记 -1，不参与似然）。
//  2. 初始化：a=1，b=logit(p_correct)/D（正确率 logit 初值，物理意义明确）。
//  3. EM 循环：
//     - E 步：按当前 (a,b) 计算各学生在 41 个求积点的后验权重，汇总 n_k/r_jk。
//     - M 步：逐题 Newton-Raphson 更新 (a,b)。
//     - 收敛：参数最大变化 < irtTol。
//  4. 产出逐题 ItemIrtStats（按 item_version_id 升序，确定性）.
//
// 边界：空输入返回 nil；单题 / 单学生仍可估计（退化解由 a/b 界约束）.
// 全对 / 全错题目的信息矩阵奇异时，Newton 步自动中断，参数停在边界
// （与 girth 同阶行为，不伪造有限值）.
func Calibrate2PL(records []ResponseRecord) []ItemIrtStats {
	if len(records) == 0 {
		return nil
	}

	// —— 1. 构建索引与稠密作答矩阵 ——
	sIdx := make(map[string]int)
	studentIDs := make([]string, 0)
	iIdx := make(map[string]int)
	itemIDs := make([]string, 0)
	for _, r := range records {
		if _, ok := sIdx[r.StudentAliasID]; !ok {
			sIdx[r.StudentAliasID] = len(studentIDs)
			studentIDs = append(studentIDs, r.StudentAliasID)
		}
		if _, ok := iIdx[r.ItemVersionID]; !ok {
			iIdx[r.ItemVersionID] = len(itemIDs)
			itemIDs = append(itemIDs, r.ItemVersionID)
		}
	}
	N, Q := len(studentIDs), len(itemIDs)

	// u[i][j] = correct (0/1)；缺失 = -1.
	u := make([][]float64, N)
	for i := range u {
		u[i] = make([]float64, Q)
		for j := range u[i] {
			u[i][j] = -1
		}
	}
	itemN := make([]int, Q)
	for _, r := range records {
		ii, jj := sIdx[r.StudentAliasID], iIdx[r.ItemVersionID]
		u[ii][jj] = r.Correct
		itemN[jj]++
	}

	// —— 2. 初始化题目参数 ——
	a := make([]float64, Q)
	b := make([]float64, Q)
	for j := 0; j < Q; j++ {
		a[j] = 1.0
		pc, cnt := 0.0, 0
		for i := 0; i < N; i++ {
			if u[i][j] >= 0 {
				pc += u[i][j]
				cnt++
			}
		}
		if cnt > 0 {
			pc /= float64(cnt)
		}
		pc = math.Min(math.Max(pc, irtLogPEps), 1.0-irtLogPEps)
		b[j] = math.Log(pc/(1.0-pc)) / irtD
		b[j] = math.Min(math.Max(b[j], irtBMin), irtBMax)
	}

	// 求积点 log 权重（E 步复用）.
	logW := make([]float64, irtQuadPoints)
	for k := 0; k < irtQuadPoints; k++ {
		logW[k] = math.Log(irtQuadWeights[k])
	}

	// —— 3. EM 迭代 ——
	for iter := 0; iter < irtMaxIter; iter++ {
		aOld := make([]float64, Q)
		bOld := make([]float64, Q)
		copy(aOld, a)
		copy(bOld, b)

		// —— E 步：后验权重 → n_k, r_jk ——
		n := make([]float64, irtQuadPoints)
		rjk := make([][]float64, Q)
		for j := range rjk {
			rjk[j] = make([]float64, irtQuadPoints)
		}

		logL := make([]float64, irtQuadPoints)
		for i := 0; i < N; i++ {
			for k := 0; k < irtQuadPoints; k++ {
				ll := 0.0
				for j := 0; j < Q; j++ {
					if u[i][j] < 0 {
						continue
					}
					p := irtP(a[j], b[j], irtQuadNodes[k])
					if u[i][j] == 1.0 {
						ll += math.Log(p)
					} else {
						ll += math.Log(1.0 - p)
					}
				}
				logL[k] = ll + logW[k]
			}
			// log-sum-exp 数值稳定归一化.
			maxLog := logL[0]
			for k := 1; k < irtQuadPoints; k++ {
				if logL[k] > maxLog {
					maxLog = logL[k]
				}
			}
			sum := 0.0
			for k := 0; k < irtQuadPoints; k++ {
				sum += math.Exp(logL[k] - maxLog)
			}
			logZ := maxLog + math.Log(sum)
			for k := 0; k < irtQuadPoints; k++ {
				post := math.Exp(logL[k] - logZ)
				n[k] += post
				for j := 0; j < Q; j++ {
					if u[i][j] < 0 {
						continue
					}
					rjk[j][k] += post * u[i][j]
				}
			}
		}

		// —— M 步：逐题 Fisher scoring，α=log(a) 参数化 + 正态先验 ——
		//
		// 对每题 j 最大化 Q_j(α,b) + log prior(α)，α = log a。
		//   Q_j = Σ_k [ r_jk·log P_k + (n_k-r_jk)·log(1-P_k) ],  η_k = D·a·(θ_k-b).
		// 记分函数 s_k = r_jk - n_k·P_k，W_k = n_k·P_k·(1-P_k)：
		//   g_α   = Σ s_k·η_k           g_b   = Σ s_k·(-D·a)
		//   I_αα  = Σ η_k²·W_k + 1/σ²   I_bb  = Σ (D·a)²·W_k
		//   I_αb  = -Σ η_k·(D·a)·W_k
		// 先验 α~N(μ,σ²) 贡献 g_α += -(α-μ)/σ² , I_αα += 1/σ²，把区分度拉向
		// 1.0 附近，稳定 MML 中区分度不可识别导致的恢复退化（等价 girth 对 a
		// 的 log-normal 先验）. Fisher 信息在 α 参数化下正定.
		//
		// 每步 Fisher scoring 后做回溯线搜索（保证 Q 单调增）：初值远离最优点时
		// 完整 Newton 步会越过峰值导致 EM 振荡不收敛；步长折半直至 Q 上升.
		priorPrec := 1.0 / (irtPriorAlphaStd * irtPriorAlphaStd)
		for j := 0; j < Q; j++ {
			for ni := 0; ni < irtNewtonIter; ni++ {
				alpha := math.Log(a[j])
				gAlpha, gb := 0.0, 0.0
				IalphaAlpha, Ibb, IalphaB := 0.0, 0.0, 0.0
				for k := 0; k < irtQuadPoints; k++ {
					p := irtP(a[j], b[j], irtQuadNodes[k])
					w := n[k] * p * (1.0 - p)
					s := rjk[j][k] - n[k]*p
					th := irtQuadNodes[k] - b[j]
					eta := irtD * a[j] * th
					gAlpha += s * eta
					gb += s * (-irtD * a[j])
					IalphaAlpha += eta * eta * w
					Ibb += irtD * irtD * a[j] * a[j] * w
					IalphaB += -eta * irtD * a[j] * w
				}
				// 先验贡献.
				gAlpha += -(alpha - irtPriorAlphaMean) * priorPrec
				IalphaAlpha += priorPrec
				det := IalphaAlpha*Ibb - IalphaB*IalphaB
				if det <= 0 || math.IsNaN(det) || math.IsInf(det, 0) {
					break
				}
				// Fisher scoring 完整步：I·Δ = g.
				dAlphaFull := (Ibb*gAlpha - IalphaB*gb) / det
				dbFull := (IalphaAlpha*gb - IalphaB*gAlpha) / det
				qCur := irtQitem(j, a[j], b[j], n, rjk, priorPrec)
				// 回溯线搜索：步长 λ = 1, 1/2, 1/4, ... 直至 Q 上升.
				step := 1.0
				accepted := false
				for trial := 0; trial < 8; trial++ {
					newAlpha := alpha + step*dAlphaFull
					newB := b[j] + step*dbFull
					newA := math.Exp(newAlpha)
					newA = math.Min(math.Max(newA, irtAMin), irtAMax)
					newB = math.Min(math.Max(newB, irtBMin), irtBMax)
					if irtQitem(j, newA, newB, n, rjk, priorPrec) > qCur {
						a[j] = newA
						b[j] = newB
						accepted = true
						break
					}
					step *= 0.5
				}
				if !accepted {
					break // 完整步与折半步均不提升 Q，已近该题极值.
				}
			}
		}

		// —— 收敛判定 ——
		maxDelta := 0.0
		for j := 0; j < Q; j++ {
			d := math.Abs(a[j]-aOld[j]) + math.Abs(b[j]-bOld[j])
			if d > maxDelta {
				maxDelta = d
			}
		}
		if maxDelta < irtTol {
			break
		}
	}

	// —— 4. 组装产出（按 item_version_id 升序，确定性） ——
	result := make([]ItemIrtStats, 0, Q)
	for j := 0; j < Q; j++ {
		result = append(result, ItemIrtStats{
			ItemVersionID:  itemIDs[j],
			SampleSize:     itemN[j],
			Difficulty:     b[j],
			Discrimination: a[j],
			Rating:         IrtDifficultyToRating(a[j], b[j]),
		})
	}
	sort.Slice(result, func(i, j int) bool {
		return result[i].ItemVersionID < result[j].ItemVersionID
	})
	return result
}
