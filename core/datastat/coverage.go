// coverage.go 承载覆盖缺口盘点五轴热力图数据（T-W4-005；Python 冻结实现
// src/core/data/coverage_gap.py 的 Go 重锚定）。
//
// 架构 v2 §4.7「飞轮闭环」：覆盖缺口盘点（知识点×认知层级×用途×学段×学科
// 五轴热力图数据）← 组装缺口报告+库存盘点 → 驱动四线生产排期。
//
// 五轴定义：
//  1. 知识点 kp_code（来自 item_kp 表）
//  2. 认知层级 cognitive_level（来自 item_version.objective）
//  3. 用途 purpose（practice/diagnosis/measurement；目标侧维度——items 不绑定
//     用途，同一题池服务全部用途，profile 指定各用途的需求量）
//  4. 学段 gradeband（L/M/H）
//  5. 学科 subject（参数注入，§5 不 import 学科包）
//
// 设计要点：实际题量按 4 轴聚合（kp × cognitive × grade × subject），用途为
// profile 目标维度；单次 SQL 全量实际计数是取数面职责（冻结实现
// _FETCH_ACTUAL_COUNTS_SQL），本核消费注入的 actualCounts。不做自动排期决策
// （任务卡 non_goals）；仅产出缺口数据供外部消费。
package datastat

import (
	"encoding/json"
	"errors"
	"fmt"
	"math"
	"sort"
	"strconv"
	"strings"
)

// Purposes 是场景三值域（对应冻结实现 coverage_gap.PURPOSES）.
var Purposes = []string{ScopePractice, ScopeDiagnosis, ScopeMeasurement}

// 覆盖缺口的入参错误.
var (
	// ErrInvalidPurpose 表示 purpose 不在 D5 三值域内.
	ErrInvalidPurpose = errors.New("datastat: 非法 purpose")
	// ErrNegativeTarget 表示目标题量为负.
	ErrNegativeTarget = errors.New("datastat: target 不能为负")
)

// CoverageTarget 是五轴目标配比单元格（profile 输入；对应冻结实现
// CoverageTarget）. Subject 为学科标识（如 'subject-math'；参数注入，§5）.
type CoverageTarget struct {
	KpCode         string
	CognitiveLevel string
	Purpose        string
	Gradeband      string
	Subject        string
	Target         int
}

// CoverageKey 是实际题量的 4 轴聚合键（kp × cognitive × grade × subject）。
// purpose 无关：items 不绑定用途，同一题池服务全部用途——practice 需 10 题、
// measurement 需 3 题，actual 是同一池（对应冻结实现取数 SQL 的 GROUP BY 轴）.
type CoverageKey struct {
	KpCode         string
	CognitiveLevel string
	Gradeband      string
	Subject        string
}

// CoverageCell 是五轴缺口矩阵单元格（对应冻结实现 CoverageCell；JSON 字段序
// 与冻结实现 asdict 一致，供 ToJSON 消费）.
type CoverageCell struct {
	KpCode         string  `json:"kp_code"`
	CognitiveLevel string  `json:"cognitive_level"`
	Purpose        string  `json:"purpose"`
	Gradeband      string  `json:"gradeband"`
	Subject        string  `json:"subject"`
	Target         int     `json:"target"`
	Actual         int     `json:"actual"`
	Gap            int     `json:"gap"`
	CoveragePct    float64 `json:"coverage_pct"`
}

// CoverageGapMatrix 是五轴覆盖缺口矩阵（含汇总统计；对应冻结实现
// CoverageGapMatrix 的 dataclass 字段）.
type CoverageGapMatrix struct {
	// Cells 所有五轴单元格（含 target=0 的也保留，便于排期工具全量消费）.
	Cells []CoverageCell
	// SnapshotID 内容快照标识（可选，用于追溯取数时点）.
	SnapshotID *string
	// TotalTarget / TotalActual / TotalGap 全局汇总；TotalActual 实际贡献
	// 不超过需求（min(actual, target)）.
	TotalTarget int
	TotalActual int
	TotalGap    int
	// OverallCoveragePct 全局覆盖率（原始值；ToJSON 时保留 2 位小数）.
	OverallCoveragePct float64
}

// pyFloat 按 Python str(float)（最短往返表示）格式化浮点：整值补 ".0"
// （Python str(40.0)="40.0" 而 Go strconv 'g' 为 "40"）。适用值域为本包统计
// 输出的常规量级（0~100 的覆盖率与普通参数值；超大数量级下 Python 在 1e16
// 处切指数记法、Go 'g' 在 1e21，超出常规统计值域不适用）.
func pyFloat(v float64) string {
	s := strconv.FormatFloat(v, 'g', -1, 64)
	if !strings.ContainsAny(s, ".eE") {
		s += ".0"
	}
	return s
}

// pyFloat64 是按 Python str(float) 字面格式序列化的浮点（JSON 视图用，
// 保证 ToJSON 与冻结实现 json.dumps 逐字节同形：整值浮点输出 "40.0" 而非
// Go 默认的 "40"）.
type pyFloat64 float64

// MarshalJSON 实现 json.Marshaler.
func (f pyFloat64) MarshalJSON() ([]byte, error) {
	return []byte(pyFloat(float64(f))), nil
}

// round2 两位小数舍入（对应冻结实现 round(x, 2)；本包统计值不触半点边界）.
func round2(v float64) float64 {
	return math.Round(v*100) / 100
}

// coverageCellJSON / coverageSummaryJSON 是 ToJSON 的输出视图（字段序与冻结
// 实现单元格 asdict 的键序一致；coverage_pct 以 Python str(float) 字面格式
// 序列化——整值浮点输出 "40.0" 而非 Go 默认的 "40"）.
type coverageCellJSON struct {
	KpCode         string    `json:"kp_code"`
	CognitiveLevel string    `json:"cognitive_level"`
	Purpose        string    `json:"purpose"`
	Gradeband      string    `json:"gradeband"`
	Subject        string    `json:"subject"`
	Target         int       `json:"target"`
	Actual         int       `json:"actual"`
	Gap            int       `json:"gap"`
	CoveragePct    pyFloat64 `json:"coverage_pct"`
}

type coverageSummaryJSON struct {
	TotalTarget        int       `json:"total_target"`
	TotalActual        int       `json:"total_actual"`
	TotalGap           int       `json:"total_gap"`
	OverallCoveragePct pyFloat64 `json:"overall_coverage_pct"`
	CellCount          int       `json:"cell_count"`
}

type coverageMatrixJSON struct {
	SnapshotID *string             `json:"snapshot_id"`
	Summary    coverageSummaryJSON `json:"summary"`
	Cells      []coverageCellJSON  `json:"cells"`
}

// ToJSON 转 JSON 字节（对应冻结实现 CoverageGapMatrix.to_json，验收 §3：
// 可被外部排期工具消费）。summary.overall_coverage_pct 保留 2 位小数；单元格
// coverage_pct 为原始值。字符串转义用 Go encoding/json（HTML 转义差异仅影响
// 含 <>& 的键值，本域标识符不含）.
func (m CoverageGapMatrix) ToJSON() ([]byte, error) {
	cells := make([]coverageCellJSON, 0, len(m.Cells))
	for _, c := range m.Cells {
		cells = append(cells, coverageCellJSON{
			KpCode: c.KpCode, CognitiveLevel: c.CognitiveLevel, Purpose: c.Purpose,
			Gradeband: c.Gradeband, Subject: c.Subject,
			Target: c.Target, Actual: c.Actual, Gap: c.Gap,
			CoveragePct: pyFloat64(c.CoveragePct),
		})
	}
	view := coverageMatrixJSON{
		SnapshotID: m.SnapshotID,
		Summary: coverageSummaryJSON{
			TotalTarget:        m.TotalTarget,
			TotalActual:        m.TotalActual,
			TotalGap:           m.TotalGap,
			OverallCoveragePct: pyFloat64(round2(m.OverallCoveragePct)),
			CellCount:          len(m.Cells),
		},
		Cells: cells,
	}
	return json.MarshalIndent(view, "", "  ")
}

// csvField 按 Python csv.writer 的 QUOTE_MINIMAL 规则转义字段.
func csvField(s string) string {
	if strings.ContainsAny(s, ",\"\r\n") {
		return "\"" + strings.ReplaceAll(s, "\"", "\"\"") + "\""
	}
	return s
}

// ToCSV 转 CSV 字符串（对应冻结实现 CoverageGapMatrix.to_csv，验收 §3）。
// 列：kp_code,cognitive_level,purpose,gradeband,subject,target,actual,gap,
// coverage_pct；行终止符 \r\n（Python csv.writer 默认 lineterminator，含末行）；
// coverage_pct 保留 2 位小数.
func (m CoverageGapMatrix) ToCSV() string {
	var b strings.Builder
	b.WriteString("kp_code,cognitive_level,purpose,gradeband,subject,target,actual,gap,coverage_pct\r\n")
	for _, c := range m.Cells {
		row := []string{
			csvField(c.KpCode), csvField(c.CognitiveLevel), csvField(c.Purpose),
			csvField(c.Gradeband), csvField(c.Subject),
			strconv.Itoa(c.Target), strconv.Itoa(c.Actual), strconv.Itoa(c.Gap),
			pyFloat(round2(c.CoveragePct)),
		}
		b.WriteString(strings.Join(row, ","))
		b.WriteString("\r\n")
	}
	return b.String()
}

// GapCells 仅返回有缺口的单元格（gap > 0），按 gap 降序（对应冻结实现
// gap_cells；稳定排序——同 gap 保持 profile 原序）.
func (m CoverageGapMatrix) GapCells() []CoverageCell {
	gaps := make([]CoverageCell, 0)
	for _, c := range m.Cells {
		if c.Gap > 0 {
			gaps = append(gaps, c)
		}
	}
	sort.SliceStable(gaps, func(i, j int) bool { return gaps[i].Gap > gaps[j].Gap })
	return gaps
}

// validateProfile 校验 profile（对应冻结实现 _validate_profile）：purpose 必须
// 在三值域内（D5 分场景禁混估的边界保护）且 target 非负；返回首个违规错误.
func validateProfile(profile []CoverageTarget) error {
	for _, t := range profile {
		if !ValidPurposeScope(t.Purpose) {
			return fmt.Errorf("%w: 非法 purpose=%q；合法值 %v", ErrInvalidPurpose, t.Purpose, Purposes)
		}
		if t.Target < 0 {
			return fmt.Errorf("%w: %d", ErrNegativeTarget, t.Target)
		}
	}
	return nil
}

// ComputeCoverageGap 计算五轴覆盖缺口矩阵（纯函数；对应冻结实现
// coverage_gap.compute_coverage_gap 的聚合面）。
//
// actualCounts 是 4 轴实际已发布题量（取数 SQL 按 status='published' 聚合，
// IO 面本波留白由调用方注入）；缺失键视为 0。
// 覆盖率：target=0 视为已满足（100.0，避免除零）；actual > target 时封顶
// 100%（不算超额覆盖，只算满足）。空 profile：汇总全 0、覆盖率 100.0.
func ComputeCoverageGap(profile []CoverageTarget, actualCounts map[CoverageKey]int, snapshotID *string) (CoverageGapMatrix, error) {
	if err := validateProfile(profile); err != nil {
		return CoverageGapMatrix{}, err
	}

	cells := make([]CoverageCell, 0, len(profile))
	totalTarget := 0
	totalActual := 0
	totalGap := 0

	for _, target := range profile {
		// 实际题量：4 轴聚合（kp × cognitive × grade × subject），purpose 无关
		actual := actualCounts[CoverageKey{
			KpCode:         target.KpCode,
			CognitiveLevel: target.CognitiveLevel,
			Gradeband:      target.Gradeband,
			Subject:        target.Subject,
		}]
		gap := max(0, target.Target-actual)
		// 覆盖率：target=0 视为已满足（无需求），避免除零；上限 100%
		coveragePct := 100.0
		if target.Target > 0 {
			coveragePct = math.Min(float64(actual)/float64(target.Target)*100.0, 100.0)
		}

		cells = append(cells, CoverageCell{
			KpCode:         target.KpCode,
			CognitiveLevel: target.CognitiveLevel,
			Purpose:        target.Purpose,
			Gradeband:      target.Gradeband,
			Subject:        target.Subject,
			Target:         target.Target,
			Actual:         actual,
			Gap:            gap,
			CoveragePct:    coveragePct,
		})
		totalTarget += target.Target
		totalActual += min(actual, target.Target) // 实际贡献不超过需求
		totalGap += gap
	}

	overall := 100.0
	if totalTarget > 0 {
		overall = float64(totalActual) / float64(totalTarget) * 100.0
	}

	return CoverageGapMatrix{
		Cells:              cells,
		SnapshotID:         snapshotID,
		TotalTarget:        totalTarget,
		TotalActual:        totalActual,
		TotalGap:           totalGap,
		OverallCoveragePct: overall,
	}, nil
}

// BuildProfileFromGrid 从网格批量构造 profile（笛卡尔积 × 3 用途；对应冻结
// 实现 build_profile_from_grid）。便于按「数学 3-4 年级首批图谱维度」批量生成
// ~400 节点级目标配比（验收 §2）。
// 遍历顺序：kp × cognitive × grade × purpose——purpose 按 Purposes 规范序
// （冻结实现依赖调用方 dict 插入序；Go 固定规范序保证确定性，D6 可重放）；
// targetPerPurpose 未声明的用途跳过.
func BuildProfileFromGrid(kpCodes, cognitiveLevels, gradebands []string, subject string, targetPerPurpose map[string]int) []CoverageTarget {
	targets := make([]CoverageTarget, 0)
	for _, kp := range kpCodes {
		for _, cog := range cognitiveLevels {
			for _, grade := range gradebands {
				for _, purpose := range Purposes {
					count, ok := targetPerPurpose[purpose]
					if !ok {
						continue
					}
					targets = append(targets, CoverageTarget{
						KpCode:         kp,
						CognitiveLevel: cog,
						Purpose:        purpose,
						Gradeband:      grade,
						Subject:        subject,
						Target:         count,
					})
				}
			}
		}
	}
	return targets
}
