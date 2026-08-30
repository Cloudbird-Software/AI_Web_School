// ctt.go 承载 CTT 经典测量理论统计核（W3 S8 / T-W4-047 / T-W4-030；Python
// 冻结实现 src/core/data/ctt.py + ctt_report.py 的 Go 重锚定）。
//
// 架构 v2 §4.7「参数标定」首年形态：CTT（正确率/点二列）产出实测参数行。
// 产出对齐 item_param 列（迁移 0010）：
//   - params.difficulty      = 正确率 p（CTT 难度指数，越大越易）
//   - params.discrimination  = 修正点二列相关系数；不可计算时为 nil（不伪造 0）
//   - sample_size            = 参与估计的作答事件数 n
//
// 分场景禁混估（宪法 D5）：取数按 scene 精确过滤是调用方（取数面）职责，
// 本核消费已过滤的记录列表，结构上不存在跨场景聚合路径。
//
// DB 取数/落库面（run_ctt_calibration 的 AsyncSession 面）本波留白，见 doc.go。
package datastat

import (
	"errors"
	"fmt"
	"math"
	"sort"
	"time"
)

// 估计方法版本（D6：方法迭代时递增，历史行引用当时版本；对应冻结实现
// ctt.CTT_METHOD_VERSION）与实测来源标识（item_param.source 域 measured_*）.
const (
	CTTMethodVersion    = "ctt-v1"
	CTTSource           = "measured_ctt"
	CTTMinSampleDefault = 30 // T-W4-047：区分度最小样本门槛；n<30 点二列无统计意义
)

// 场景三值域（与 response_event_scene_enum / D5 对齐；ctt / shrinkage / replay /
// coverage_gap / health 的 VALID_PURPOSE_SCOPES 同域，包级统一声明一次）.
const (
	ScopePractice    = "practice"
	ScopeDiagnosis   = "diagnosis"
	ScopeMeasurement = "measurement"
)

// purposeScopes 固定展示顺序的三值域（遍历确定性）.
var purposeScopes = []string{ScopePractice, ScopeDiagnosis, ScopeMeasurement}

// ErrInvalidPurposeScope 表示 purpose_scope 不在 D5 三值域内.
var ErrInvalidPurposeScope = errors.New(
	"datastat: purpose_scope 越域（合法域 practice/diagnosis/measurement，D5）")

// ValidPurposeScope 报告 scope 是否在 D5 三值域内.
func ValidPurposeScope(s string) bool {
	for _, v := range purposeScopes {
		if s == v {
			return true
		}
	}
	return false
}

// validatePurposeScope 校验场景入参（报错文案与冻结实现 ValueError 对齐）.
func validatePurposeScope(scope string) error {
	if !ValidPurposeScope(scope) {
		return fmt.Errorf("%w: %q（合法域 [diagnosis measurement practice]；D5 禁止跨场景混估）",
			ErrInvalidPurposeScope, scope)
	}
	return nil
}

// ResponseRecord 是一条参与估计的作答记录（已按场景过滤；对应冻结实现
// ctt.ResponseRecord）.
type ResponseRecord struct {
	ItemVersionID  string
	StudentAliasID string
	Correct        float64 // 0.0/1.0（客观题）；部分分给分题可取 [0,1]
}

// ItemCttStats 是单题 CTT 统计量（对应冻结实现 ctt.ItemCttStats；冻结实现的
// 报告内嵌 ItemStat 与本类型字段一致，Go 侧复用）.
//
// Difficulty：正确率 p（越大越易）。
// Discrimination：修正点二列（本题得分 × 学生总分减本题 的 Pearson 相关）；
// nil = n<2 或任一变量零方差（信息不足，不伪造 0）。
type ItemCttStats struct {
	ItemVersionID  string
	SampleSize     int
	Difficulty     float64
	Discrimination *float64
}

// pearson 是 Pearson 相关系数（对应冻结实现 ctt._pearson）：n<2 或任一变量
// 零方差时返回 nil——信息不足不伪造 0。运算次序与冻结实现逐行一致.
func pearson(xs, ys []float64) *float64 {
	n := len(xs)
	if n < 2 {
		return nil
	}
	mx := sum(xs) / float64(n)
	my := sum(ys) / float64(n)
	var sxx, syy float64
	for i := range xs {
		sxx += (xs[i] - mx) * (xs[i] - mx)
		syy += (ys[i] - my) * (ys[i] - my)
	}
	if sxx == 0 || syy == 0 {
		return nil
	}
	var sxy float64
	for i := range xs {
		sxy += (xs[i] - mx) * (ys[i] - my)
	}
	r := sxy / math.Sqrt(sxx*syy)
	return &r
}

// sum 求和（逐元素顺序累加，与 Python sum() 同序）.
func sum(vs []float64) float64 {
	s := 0.0
	for _, v := range vs {
		s += v
	}
	return s
}

// ComputeCtt 对一批作答记录计算逐题 CTT 统计量（纯函数，无副作用；对应冻结
// 实现 ctt.compute_ctt）。
//
// 算法：
//  1. 学生总分 = 该学生批内全部记录 correct 之和（场景内，D5）。
//  2. 逐题：difficulty = 本题记录 correct 均值。
//  3. 逐题区分度 = Pearson(本题 correct, 学生总分 - 本题 correct)
//     （修正点二列：总分剔除本题，避免自相关高估）。
//
// 返回按 item_version_id 升序排列（确定性）；空输入返回空切片。
func ComputeCtt(records []ResponseRecord) []ItemCttStats {
	// 学生总分（该学生在批内全部记录）
	studentTotal := make(map[string]float64)
	for _, r := range records {
		studentTotal[r.StudentAliasID] += r.Correct
	}

	// 按题分组（保持首次出现序，与冻结实现 dict 一致）
	byItem := make(map[string][]ResponseRecord)
	for _, r := range records {
		byItem[r.ItemVersionID] = append(byItem[r.ItemVersionID], r)
	}

	ids := make([]string, 0, len(byItem))
	for id := range byItem {
		ids = append(ids, id)
	}
	sort.Strings(ids) // 冻结实现 sorted(by_item)

	stats := make([]ItemCttStats, 0, len(ids))
	for _, itemVersionID := range ids {
		itemRecords := byItem[itemVersionID]
		n := len(itemRecords)
		xs := make([]float64, n)
		for i, r := range itemRecords {
			xs[i] = r.Correct
		}
		difficulty := sum(xs) / float64(n)
		// 修正总分：学生总分减本题得分
		ys := make([]float64, n)
		for i, r := range itemRecords {
			ys[i] = studentTotal[r.StudentAliasID] - r.Correct
		}
		stats = append(stats, ItemCttStats{
			ItemVersionID:  itemVersionID,
			SampleSize:     n,
			Difficulty:     difficulty,
			Discrimination: pearson(xs, ys),
		})
	}
	return stats
}

// ComputeDiscrimination 计算单题区分度（修正点二列 Pearson），带 min_sample
// 门槛（对应冻结实现 ctt.compute_discrimination，T-W4-047）。
//
// n < minSample 时返回 nil——小样本点二列方差大、统计无意义，不伪造 0
// （与「信息不足不伪造」原则一致）；n ≥ minSample 时计算行为与 ComputeCtt
// 的区分度完全一致（同修正点二列、同 pearson）。小样本警示记录由调用方
// 承担（纯函数核无日志面）。学生总分与 ComputeCtt 一致取批内全部记录之和。
func ComputeDiscrimination(responses []ResponseRecord, key string, minSample int) *float64 {
	studentTotal := make(map[string]float64)
	for _, r := range responses {
		studentTotal[r.StudentAliasID] += r.Correct
	}

	var itemRecords []ResponseRecord
	for _, r := range responses {
		if r.ItemVersionID == key {
			itemRecords = append(itemRecords, r)
		}
	}
	n := len(itemRecords)
	if n < minSample {
		return nil
	}
	xs := make([]float64, n)
	ys := make([]float64, n)
	for i, r := range itemRecords {
		xs[i] = r.Correct
		ys[i] = studentTotal[r.StudentAliasID] - r.Correct
	}
	return pearson(xs, ys)
}

// ────────────────────────────────────────────────────────────────────
// 测量卷 CTT 报告（冻结实现 ctt_report.py）
// ────────────────────────────────────────────────────────────────────

// difficultyBands 难度分布分桶边界（p_correct 口径，越大越易；对应冻结实现
// _DIFFICULTY_BANDS）：五档难(0-0.3)/较难(0.3-0.5)/中(0.5-0.7)/较易(0.7-0.9)/
// 易(0.9-1.0)；半开区间 [lo, hi)，最后一档含 1.0.
var difficultyBands = []struct {
	band         string
	lower, upper float64
}{
	{"hard", 0.0, 0.3},
	{"somewhat_hard", 0.3, 0.5},
	{"medium", 0.5, 0.7},
	{"somewhat_easy", 0.7, 0.9},
	{"easy", 0.9, 1.0 + 1e-9}, // 含 1.0
}

// DifficultyBand 是难度分布单桶统计（对应冻结实现 ctt_report.DifficultyBand）.
type DifficultyBand struct {
	Band  string
	Lower float64 // 区间下界（含）
	Upper float64 // 区间上界（不含，最后一桶含 1.0）
	Count int
}

// CttReport 是 CTT 信度/区分度报告（纯函数产物，无 DB 依赖；对应冻结实现
// ctt_report.CttReport）.
type CttReport struct {
	PaperID                string         // 关联测量卷 id（仅标签，本函数不校验存在性）
	SampleSize             int            // 学生数 n（去重 student_alias_id 计数）
	ItemCount              int            // 题数 k（去重 item_version_id 计数）
	CronbachAlpha          *float64       // 内部一致性；k<2 / n<2 / 总分零方差时 nil
	Sem                    *float64       // 测量标准误 SD·√(1-α)；α 不可计算时 nil
	ItemStats              []ItemCttStats // 每题统计（按 item_version_id 升序，确定性）
	DifficultyDistribution []DifficultyBand
	SmallSampleWarning     bool // n<minSample 时 true（验收 #2）
	Notes                  []string
	GeneratedAt            time.Time
}

// sampleVariance 是样本方差（n-1 分母；对应冻结实现 ctt_report._sample_variance）：
// n<2 或零方差返回 nil——零方差下 Cronbach's α 定义失效（分母 0），统一用
// nil 表达「不可计算」，notes 解释原因.
func sampleVariance(values []float64) *float64 {
	n := len(values)
	if n < 2 {
		return nil
	}
	mean := sum(values) / float64(n)
	ss := 0.0
	for _, v := range values {
		ss += (v - mean) * (v - mean)
	}
	if ss == 0.0 {
		return nil
	}
	v := ss / float64(n-1)
	return &v
}

// cronbachAlpha 是 Cronbach's α 内部一致性系数（对应冻结实现
// ctt_report._cronbach_alpha）：
//
//	α = (k/(k-1)) · (1 - Σσ²ᵢ / σ²_total)
//
// k=题数；σ²ᵢ=题 i 的样本方差（n-1 分母）；σ²_total=学生总分（各题得分之和）
// 的样本方差。为什么用样本方差：报告面对的是样本学生的测量数据，总体方差
// 未知，Cronbach 原始公式（Kuder-Richardson 同口径）用样本方差。
// matrix 每行是一个学生在 k 题上的得分；n<2 / k<2 / 行不等长 / 总分零方差
// 返回 nil。负值表示题间反向相关，如实返回.
func cronbachAlpha(matrix [][]float64) *float64 {
	n := len(matrix)
	if n < 2 {
		return nil
	}
	k := len(matrix[0])
	if k < 2 {
		return nil
	}
	// 校验所有行等长（防御性，调用方应保证）
	for _, row := range matrix {
		if len(row) != k {
			return nil
		}
	}

	// 逐题方差：某题零方差时 σ²ᵢ 贡献为 0，仍可计算 α（该题不区分学生，但公式有效）
	itemVariances := make([]float64, k)
	for j := 0; j < k; j++ {
		col := make([]float64, n)
		for i := 0; i < n; i++ {
			col[i] = matrix[i][j]
		}
		if v := sampleVariance(col); v != nil {
			itemVariances[j] = *v
		}
	}

	// 学生总分方差
	totalScores := make([]float64, n)
	for i := 0; i < n; i++ {
		totalScores[i] = sum(matrix[i])
	}
	totalVar := sampleVariance(totalScores)
	if totalVar == nil || *totalVar == 0.0 {
		return nil
	}

	alpha := (float64(k) / float64(k-1)) * (1.0 - sum(itemVariances) / *totalVar)
	return &alpha
}

// binDifficulty 将难度 p 分到五档之一（对应冻结实现 ctt_report._bin_difficulty）；
// 越界返回 ok=false（不应发生，p∈[0,1]）.
func binDifficulty(p float64) (string, bool) {
	for _, b := range difficultyBands {
		if b.lower <= p && p < b.upper {
			return b.band, true
		}
	}
	return "", false
}

// difficultyDistribution 按五档汇总难度分布（对应冻结实现
// ctt_report._difficulty_distribution）：每桶含 count，按 band 定义序输出.
func difficultyDistribution(itemStats []ItemCttStats) []DifficultyBand {
	counter := make(map[string]int)
	for _, s := range itemStats {
		if band, ok := binDifficulty(s.Difficulty); ok {
			counter[band]++
		}
	}
	bands := make([]DifficultyBand, 0, len(difficultyBands))
	for _, b := range difficultyBands {
		bands = append(bands, DifficultyBand{Band: b.band, Lower: b.lower, Upper: b.upper, Count: counter[b.band]})
	}
	return bands
}

// GenerateCttReport 生成测量卷 CTT 信度/区分度报告（纯函数，验收 #1；对应
// 冻结实现 ctt_report.generate_ctt_report）。
//
// 参数：
//   - responseEvents：单场景作答记录（调用方保证已按 scene='measurement' 过滤，
//     D5 禁混估；本函数不复检 scene 字段，因 ResponseRecord 不携带 scene）。
//   - minSample：小样本门槛，默认 CTTMinSampleDefault(30)；仅控制报告头警示，
//     不影响单题区分度 nil 判定（与 ComputeDiscrimination 的门槛职责分离）。
//   - now：报告生成时刻（冻结实现缺省 datetime.now(UTC)；纯函数核不做时钟
//     读取，由调用方显式传入，可传固定值用于确定性测试）。
//
// 边界：空事件列表返回 α=nil、sem=nil、n=0、k=0、警示=true、notes 含
// 「无作答数据」；α 计算矩阵缺位用 0 填（学生未答该题记 0 分——CTT 假设
// 全题集，未答=不得分是教育测量惯例；同一学生多条同题记录取最后一条，防御性去重）.
func GenerateCttReport(responseEvents []ResponseRecord, paperID string, minSample int, now time.Time) CttReport {
	notes := []string{}

	// 学生数 n / 题数 k（去重）
	studentIDs := make(map[string]bool)
	itemIDs := make(map[string]bool)
	for _, r := range responseEvents {
		studentIDs[r.StudentAliasID] = true
		itemIDs[r.ItemVersionID] = true
	}
	n := len(studentIDs)
	k := len(itemIDs)

	// 复用 ComputeCtt 计算每题统计（验收 #3：区分度与既有 CTT 一致）
	cttStats := ComputeCtt(responseEvents)
	itemStats := make([]ItemCttStats, len(cttStats))
	copy(itemStats, cttStats)

	// 小样本警示（验收 #2）
	smallSampleWarning := n < minSample
	if smallSampleWarning {
		notes = append(notes, fmt.Sprintf(
			"样本不足，结果仅供参考（n=%d < min_sample=%d；Cronbach's α 与区分度在小样本下方差大，不可作为定论）",
			n, minSample))
	}

	// 边界情形：无作答数据
	if n == 0 || k == 0 {
		notes = append(notes, "无作答数据：无法计算 α / SEM（n=0 或 k=0）。")
		return CttReport{
			PaperID:                paperID,
			SampleSize:             n,
			ItemCount:              k,
			CronbachAlpha:          nil,
			Sem:                    nil,
			ItemStats:              itemStats,
			DifficultyDistribution: difficultyDistribution(itemStats),
			SmallSampleWarning:     smallSampleWarning,
			Notes:                  notes,
			GeneratedAt:            now,
		}
	}

	// 构造 α 计算矩阵：行=学生（首次出现序），列=题（item_version_id 升序），
	// 缺位用 0.0 填；同一学生多条同题记录取最后一条（防御性去重，与冻结
	// 实现的覆盖写一致）。
	sortedItemIDs := make([]string, 0, k)
	for id := range itemIDs {
		sortedItemIDs = append(sortedItemIDs, id)
	}
	sort.Strings(sortedItemIDs)
	itemIdx := make(map[string]int, k)
	for j, vid := range sortedItemIDs {
		itemIdx[vid] = j
	}
	studentOrder := make([]string, 0, n) // 首次出现序（冻结实现 dict 插入序）
	studentRows := make(map[string][]float64, n)
	for _, r := range responseEvents {
		sid := r.StudentAliasID
		if _, ok := studentRows[sid]; !ok {
			studentRows[sid] = make([]float64, k)
			studentOrder = append(studentOrder, sid)
		}
		studentRows[sid][itemIdx[r.ItemVersionID]] = r.Correct
	}
	matrix := make([][]float64, 0, n)
	for _, sid := range studentOrder {
		matrix = append(matrix, studentRows[sid])
	}

	alpha := cronbachAlpha(matrix)
	var sem *float64
	if alpha == nil {
		notes = append(notes,
			"Cronbach's α 不可计算：k<2 / n<2 / 学生总分零方差（全员同分）。")
	} else {
		// SEM = SD_total · √(1-α)，SD_total 用样本标准差（n-1 分母）
		totalScores := make([]float64, len(matrix))
		for i, row := range matrix {
			totalScores[i] = sum(row)
		}
		totalVar := sampleVariance(totalScores)
		if totalVar == nil {
			notes = append(notes, "SEM 不可计算：学生总分零方差。")
		} else {
			sd := math.Sqrt(*totalVar)
			v := sd * math.Sqrt(1.0-*alpha)
			sem = &v
		}
	}

	// 区分度全 nil 时补一条备注（区分度不可计算原因：n<2 / 零方差）
	allDiscNil := len(itemStats) > 0
	for _, s := range itemStats {
		if s.Discrimination != nil {
			allDiscNil = false
			break
		}
	}
	if allDiscNil {
		notes = append(notes, "所有题目区分度均不可计算（n<2 或题分零方差）；不伪造 0。")
	}

	return CttReport{
		PaperID:                paperID,
		SampleSize:             n,
		ItemCount:              k,
		CronbachAlpha:          alpha,
		Sem:                    sem,
		ItemStats:              itemStats,
		DifficultyDistribution: difficultyDistribution(itemStats),
		SmallSampleWarning:     smallSampleWarning,
		Notes:                  notes,
		GeneratedAt:            now,
	}
}
