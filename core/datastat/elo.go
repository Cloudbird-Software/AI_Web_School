// elo.go 承载掌握度 Elo v1 在线轻量增量更新（W3 S8；Python 冻结实现
// src/core/data/elo.py 的 Go 重锚定）。
//
// 架构 v2 §4.7「掌握度」：Elo/带遗忘衰减加权正确率起步；在线轻量增量 +
// 夜间批权威双轨；可换 BKT/IRT（D6 估计器可替换）。本模块是 v1 的「在线
// 轻量增量」侧：纯函数，无 DB IO、无副作用——评级状态的存取（学生掌握度
// 账）由调用方负责，便于后续接入夜间批权威侧。
//
// 模型（双评级 Elo，学生掌握度 × 题目难度成对更新）：
//   - 期望得分 E = 1 / (1 + 10^(-(R_s - R_i) / 400))
//   - 增量更新 R_s' = R_s + K·(S - E)（学生掌握度）
//     R_i' = R_i - K·(S - E)（题目难度，方向相反：
//     学生答对比预期容易 → 题难度评级下调）
//   - S ∈ {0, 1}（客观题对错）；K 为步长（默认 32）
//
// 难度换算（CTT 正确率 p → Elo 题目评级）：平均学生 R_s = BASE 时期望得分
// = p ⟹ R_i = BASE + 400·log10((1-p)/p)。p 越小题越难，R_i 越高；p=0.5 时
// R_i = BASE。该换算让 CTT 实测难度（item_param.params.difficulty）可直接
// 初始化 Elo 题目侧评级，打通「批处理标定 → 在线掌握度」的数据飞轮（§4.7）。
//
// 宪法 A5/X6：本包是核心域，不 import 任何学科包/学段包。
package datastat

import (
	"errors"
	"fmt"
	"math"
)

// 基准评级（平均学生/中位难度锚点）、量表（Elo 经典 400 量表）与默认步长
// （对应冻结实现 BASE_RATING / SCALE / DEFAULT_K）.
const (
	BaseRating = 1500.0
	Scale      = 400.0
	DefaultK   = 32.0
)

// pEps：log10(0) 无定义；全对/全错样本的 p 截断到开区间（对应冻结实现 _P_EPS）.
const pEps = 1e-6

// Elo 增量更新的入参错误（fail-closed：非法输入不产生评级漂移）.
var (
	// ErrScoreOutOfRange 表示 score 不在 [0, 1].
	ErrScoreOutOfRange = errors.New("datastat: score 必须在 [0, 1]")
	// ErrNonPositiveK 表示步长 k 非正.
	ErrNonPositiveK = errors.New("datastat: k 必须为正")
)

// ExpectedScore 计算期望得分 E = P(学生答对) ∈ (0, 1)（对应冻结实现
// elo.expected_score）：两评级相等时为 0.5.
func ExpectedScore(studentRating, itemRating float64) float64 {
	return 1.0 / (1.0 + math.Pow(10.0, -(studentRating-itemRating)/Scale))
}

// EloUpdate 执行一次作答的增量更新，返回 (新学生评级, 新题目评级)（对应冻结
// 实现 elo.elo_update）。
//
// score 为实际得分 S ∈ [0, 1]（客观题 0|1；部分分给分题可取中间值）；k 为
// 步长（默认 DefaultK；低学段/小样本可调小，由策略配置）。
// score 不在 [0,1]（含 NaN）或 k ≤ 0 时返回错误——非法输入不产生评级漂移.
func EloUpdate(studentRating, itemRating, score, k float64) (float64, float64, error) {
	if !(0.0 <= score && score <= 1.0) {
		return 0, 0, fmt.Errorf("%w，得到 %v", ErrScoreOutOfRange, score)
	}
	if k <= 0 {
		return 0, 0, fmt.Errorf("%w，得到 %v", ErrNonPositiveK, k)
	}
	e := ExpectedScore(studentRating, itemRating)
	delta := k * (score - e)
	return studentRating + delta, itemRating - delta, nil
}

// DifficultyToRating 将 CTT 正确率 p 换算为 Elo 题目难度评级（对应冻结实现
// elo.difficulty_to_rating）。
//
// 推导：平均学生 R_s = base 时 expected_score(base, R_i) = p
// ⟹ R_i = base + scale·log10((1-p)/p)。
// p 截断到 (1e-6, 1-1e-6) 开区间避免 log10(0)；p=0.5 → BaseRating，
// p→0 → +∞ 方向（越难越高）.
func DifficultyToRating(p float64) float64 {
	clipped := math.Min(math.Max(p, pEps), 1.0-pEps)
	return BaseRating + Scale*math.Log10((1.0-clipped)/clipped)
}
