// measurement_paper.go 承载测量卷产出（Python 冻结基准
// src/core/assembly/measurement_paper.py 的 Go 移植）。
//
// 将求解结果（CpSatSolution）组装为 MeasurementPaper——一份可渲染、可审计的
// 测量卷对象，承载细目表映射、题序、作答说明与合规校验。
//
// 为什么独立 MeasurementPaper 而非复用 W3 paper/paper_item ORM：
//   - W3 paper 面向在线练习/诊断的卷追溯（卷码/QR/曝光账本），字段集围绕
//     「已发布卷的持久化与扫码溯源」；
//   - 测量卷是离线产出物，核心是「双向细目表合规」——每单元格题数×难度区间
//     必须与 spec_table 一致（测量有效性的统计基础），合规偏差为 0 才可签发。
//
// 渲染适配（measurement_adapter）由 render 域消费，保持装配/渲染职责分离。
//
// 合规校验（验收 #2）：VerifyCompliance 对照 spec_table 逐单元格校验——
//   - 题数偏差：actual_count vs target_count（|差| 累加为 TotalCountDeviation）
//   - 难度合规：每题 p_correct ∈ [cell.difficulty_min, difficulty_max]
//
// 求解可行解保证偏差为 0；本函数独立校验以防产出链路下游篡改/漂移。
package assembly

import (
	"fmt"
	"strings"
)

// MeasurementCellMapping 细目表单元格映射：spec cell → 入选题列表 + 难度留档
// （Python MeasurementCellMapping）。一个 cell = (content_code, cognitive_level)
// 二元组。ItemPCorrects 与 ItemVersionIDs 同序，留档每题难度指数供合规校验
// 与审计。
type MeasurementCellMapping struct {
	ContentCode    string
	CognitiveLevel string
	TargetCount    int
	ActualCount    int
	DifficultyMin  float64
	DifficultyMax  float64
	ItemVersionIDs []string
	ItemPCorrects  []float64
}

// ComplianceViolation 单条合规偏差（VerifyCompliance 产出）.
type ComplianceViolation struct {
	CellKey  string
	Kind     string // count_mismatch | difficulty_out_of_range | cell_missing
	Detail   string
	Expected string
	Actual   string
}

// 合规偏差种类（Python Literal 域）.
const (
	ViolationCountMismatch        = "count_mismatch"
	ViolationDifficultyOutOfRange = "difficulty_out_of_range"
	ViolationCellMissing          = "cell_missing"
)

// ComplianceReport 细目表合规校验报告（Python ComplianceReport）：
// IsCompliant=true 当且仅当 Violations 为空（题数与难度全合规）；
// TotalCountDeviation 为各 cell |actual-target| 之和（0 = 题数完全一致）.
type ComplianceReport struct {
	IsCompliant         bool
	TotalCountDeviation int
	Violations          []ComplianceViolation
}

// MeasurementPaper 测量卷：求解解 + 细目表映射 + 题序 + 作答说明（Python
// MeasurementPaper）。
type MeasurementPaper struct {
	// SpecTableID / SpecTableVersion 溯源到 SpecTable（D1 版本化）.
	SpecTableID      string
	SpecTableVersion string
	// Seed 求解确定性种子（R-Z-01 留档）.
	Seed int64
	// CellMappings 细目表每 cell 的映射（按 spec_table.cells 顺序）.
	CellMappings []MeasurementCellMapping
	// OrderedItemVersionIDs 卷内题序（按 cell 顺序、cell 内按求解器返回序），
	// 渲染时按此序分配题号 1..N（题号对齐）.
	OrderedItemVersionIDs []string
	// AnswerInstructions 作答说明（卷首页印刷）.
	AnswerInstructions string
	// SelectionDigest 来自 CpSatSolution.SelectionDigest，固化选题结果供审计.
	SelectionDigest string
	// ItemPCorrect item_version_id → p_correct，合规校验与审计用.
	ItemPCorrect map[string]float64
}

// defaultAnswerInstructions 测量卷作答说明默认文案（Python
// _default_answer_instructions）。为什么提供默认：测量卷作答说明高度模板化
// （题量/作答卡/翻页限制），调用方通常无需自定义；文案含「翻页无效」提示，
// 与渲染层 ProhibitionMarker 呼应（验收 #3 禁止标记）。
func defaultAnswerInstructions(totalCount int) string {
	return fmt.Sprintf(
		"本测量卷共 %d 题。请仔细阅读每题要求后作答：选择题答案填涂在作答卡对应题号处，填空题答案写在题内空位；每题作答完毕请检查，考试期间翻页无效，禁止交头接耳。",
		totalCount)
}

// BuildMeasurementPaper 将求解可行解组装为测量卷（Python build_measurement_paper）。
//
// solution 为 Solve 产物；传入不可行解（*CpSatInfeasible）返回错误——不可行解
// 不能组卷（§4.4 铁律：禁止静默放松）。answerInstructions 传 nil 用默认文案。
func BuildMeasurementPaper(solution SolveResult, specTable *SpecTable, answerInstructions *string) (*MeasurementPaper, error) {
	sol, ok := solution.(*CpSatSolution)
	if !ok || sol == nil {
		return nil, fmt.Errorf(
			"assembly: 不可行解不能组卷：求解返回 CpSatInfeasible，请先调整 spec_table/候选池后重试求解（§4.4 铁律：禁止静默放松）")
	}

	// item_version_id → p_correct（从 selected 候选建索引，供合规校验留档）
	pCorrectMap := map[string]float64{}
	for i := range sol.Selected {
		pCorrectMap[sol.Selected[i].ItemVersionID] = sol.Selected[i].PCorrect
	}

	cellMappings := []MeasurementCellMapping{}
	orderedItemVersionIDs := []string{}
	for i := range specTable.Cells {
		cell := &specTable.Cells[i]
		cellKey := cellKeyOf(cell.ContentCode, cell.CognitiveLevel)
		vids := append([]string(nil), sol.CellAssignment[cellKey]...)
		pCorrects := make([]float64, 0, len(vids))
		for _, v := range vids {
			pCorrects = append(pCorrects, pCorrectMap[v])
		}
		cellMappings = append(cellMappings, MeasurementCellMapping{
			ContentCode:    cell.ContentCode,
			CognitiveLevel: cell.CognitiveLevel,
			TargetCount:    cell.TargetCount,
			ActualCount:    len(vids),
			DifficultyMin:  cell.DifficultyMin,
			DifficultyMax:  cell.DifficultyMax,
			ItemVersionIDs: vids,
			ItemPCorrects:  pCorrects,
		})
		orderedItemVersionIDs = append(orderedItemVersionIDs, vids...)
	}

	instructions := defaultAnswerInstructions(specTable.TotalCount())
	if answerInstructions != nil {
		instructions = *answerInstructions
	}

	return &MeasurementPaper{
		SpecTableID:           specTable.SpecTableID,
		SpecTableVersion:      specTable.SpecTableVersion,
		Seed:                  sol.Seed,
		CellMappings:          cellMappings,
		OrderedItemVersionIDs: orderedItemVersionIDs,
		AnswerInstructions:    instructions,
		SelectionDigest:       sol.SelectionDigest,
		ItemPCorrect:          pCorrectMap,
	}, nil
}

// VerifyCompliance 细目表合规校验：逐 cell 校验题数与难度（Python
// verify_compliance；验收 #2）。
//
// 校验项：
//  1. 题数：每 cell actual_count == target_count（偏差累加为 TotalCountDeviation）
//  2. 难度：每题 p_correct ∈ [cell.difficulty_min, difficulty_max]
//  3. 覆盖：spec_table 每 cell 在产出卷中均有映射（缺失记 cell_missing）
//
// 求解可行解经 BuildMeasurementPaper 产出后，本函数应返回 IsCompliant=true、
// TotalCountDeviation=0；独立校验以防产出链路下游篡改/漂移。
func VerifyCompliance(paper *MeasurementPaper, specTable *SpecTable) *ComplianceReport {
	mappingByKey := map[string]*MeasurementCellMapping{}
	for i := range paper.CellMappings {
		m := &paper.CellMappings[i]
		mappingByKey[cellKeyOf(m.ContentCode, m.CognitiveLevel)] = m
	}

	violations := []ComplianceViolation{}
	totalCountDeviation := 0

	for i := range specTable.Cells {
		cell := &specTable.Cells[i]
		key := cellKeyOf(cell.ContentCode, cell.CognitiveLevel)
		mapping, ok := mappingByKey[key]
		if !ok {
			totalCountDeviation += cell.TargetCount
			violations = append(violations, ComplianceViolation{
				CellKey:  key,
				Kind:     ViolationCellMissing,
				Detail:   fmt.Sprintf("细目表单元格 %s 在产出卷中缺失", key),
				Expected: fmt.Sprintf("target_count=%d", cell.TargetCount),
				Actual:   "缺失",
			})
			continue
		}

		// 题数校验
		if mapping.ActualCount != cell.TargetCount {
			diff := mapping.ActualCount - cell.TargetCount
			if diff < 0 {
				diff = -diff
			}
			totalCountDeviation += diff
			violations = append(violations, ComplianceViolation{
				CellKey: key,
				Kind:    ViolationCountMismatch,
				Detail: fmt.Sprintf("单元格 %s 题数偏差：目标 %d，实际 %d（偏差 %d）",
					key, cell.TargetCount, mapping.ActualCount, diff),
				Expected: fmt.Sprintf("%d", cell.TargetCount),
				Actual:   fmt.Sprintf("%d", mapping.ActualCount),
			})
		}

		// 难度校验
		for j, vid := range mapping.ItemVersionIDs {
			p := mapping.ItemPCorrects[j]
			if !(cell.DifficultyMin <= p && p <= cell.DifficultyMax) {
				violations = append(violations, ComplianceViolation{
					CellKey: key,
					Kind:    ViolationDifficultyOutOfRange,
					Detail: fmt.Sprintf("题 %s 的 p_correct=%v 不在单元格 %s 难度区间 [%v, %v]",
						vid, p, key, cell.DifficultyMin, cell.DifficultyMax),
					Expected: fmt.Sprintf("[%v, %v]", cell.DifficultyMin, cell.DifficultyMax),
					Actual:   fmt.Sprintf("%v", p),
				})
			}
		}
	}

	return &ComplianceReport{
		IsCompliant:         len(violations) == 0,
		TotalCountDeviation: totalCountDeviation,
		Violations:          violations,
	}
}

// cellKeyOf 细目表单元格键（'content_code/cognitive_level'；与求解器/合规
// 校验共用，防三处键名漂移）.
func cellKeyOf(contentCode, cognitiveLevel string) string {
	return strings.Join([]string{contentCode, cognitiveLevel}, "/")
}
