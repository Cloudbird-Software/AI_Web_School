// solver.go 承载离线测量卷求解（Python 冻结基准
// src/core/assembly/solver/cpsat_solver.py + constraints.py 的 Go 移植）。
//
// 架构 v2 §4.4「求解」段：离线出口跑完整求解（硬约束可行解）。Python 侧用
// OR-Tools CP-SAT；Go 侧零依赖（X4：标准库可替代者不引依赖），以确定性多项式
// 装填实现同一约束语义——约束面逐条对齐：
//   - 单元格配额（硬）：每个 SpecCell 的入选题数 == target_count；
//   - 候选-cell 分配（硬）：每个入选候选恰好分配给一个匹配的 cell；
//   - 难度合规（硬）：候选只能分配给 p_correct ∈ [difficulty_min, difficulty_max]
//     的 cell；
//   - 曝光互斥（硬）：excluded 集合中的 item_version_id / template_version_id
//     禁止入选；
//   - 题组整体入选（硬）：同 group_id 的候选要么全入选要么全不入选（testlet 语义）。
//
// 为什么所有约束都硬约束化：测量卷的「双向细目表合规」是测量有效性的统计
// 基础（每格题数不足则维度估计不可靠），不可降级为软目标。
//
// 求解策略（确定性，R-Z-01）：同一输入必得同一输出。
//  1. 孤立候选先装填：按 cell 顺序做二分增广（Kuhn），候选按池内下标序
//     遍历——增广路径法保证孤立候选可达的最大匹配；
//  2. 题组后装填：按 group_id 序尝试整体放入剩余缺口（组员按池内下标序，
//     不重路由已有分配；任一组员放不下则整组回滚）；
//  3. 仍有缺口 → 不可行分析（与冻结实现 _analyze_infeasibility 同口径）。
//
// CP-SAT 在多解时的具体选取属求解器内部细节，两实现满足同一硬约束集；
// Go 侧以纯函数保证同输入同解（selection_digest 公式与冻结实现一致）。
package assembly

import (
	"fmt"
	"sort"
	"strings"
)

// MeasurementCandidate 求解器消费的测量卷候选题（Python
// MeasurementCandidate；与 CandidateItem 同口径但面向离线测量）。
type MeasurementCandidate struct {
	// ItemVersionID 题 version id（与 CandidateItem 同口径）.
	ItemVersionID string
	// KpCodes 知识点编码列表（可多元素；与 cell.content_code 任一匹配即可填入）.
	KpCodes []string
	// CognitiveLevel 认知层级（Bloom 六级，与 SpecCell.cognitive_level 同集）.
	CognitiveLevel string
	// PCorrect 难度指数（p_correct 口径，[0.0, 1.0]，越大越易）.
	PCorrect float64
	// GroupID 题组 id（同组题作为整体入选/排除；nil=孤立题）.
	GroupID *string
	// TemplateVersionID 母题版本 id（曝光互斥依据；nil 表示无母题）.
	TemplateVersionID *string
}

// MatchesCell 该候选能否填入指定 cell（Python matches_cell）。
// 匹配条件：content_code 任一匹配；cognitive_level 严格相等；
// p_correct ∈ [difficulty_min, difficulty_max]（闭区间，与 SpecCell 校验一致）。
func (c *MeasurementCandidate) MatchesCell(contentCode, cognitiveLevel string, difficultyMin, difficultyMax float64) bool {
	if !containsStr(c.KpCodes, contentCode) {
		return false
	}
	if c.CognitiveLevel != cognitiveLevel {
		return false
	}
	return difficultyMin <= c.PCorrect && c.PCorrect <= difficultyMax
}

// MeasurementCandidateFromServingRow 从 v_serving_item_version 行构建测量卷
// 候选（Python measurement_candidate_from_serving_row；与
// CandidateFromServingRow 同源但额外抽取 objective.cognitive_level 和
// lineage.params.p_correct_prior——缺任一即报错）.
func MeasurementCandidateFromServingRow(row ServingRow) (MeasurementCandidate, error) {
	objective := row.Objective
	if objective == nil {
		objective = map[string]any{}
	}
	kpSet, _ := objective["kp_set"].([]any)
	kpCodes := []string{}
	for _, k := range kpSet {
		entry, ok := k.(map[string]any)
		if !ok {
			continue
		}
		if code, ok := entry["code"].(string); ok && code != "" {
			kpCodes = append(kpCodes, code)
		}
	}
	if len(kpCodes) == 0 {
		return MeasurementCandidate{}, fmt.Errorf(
			"assembly: item_version %s 的 objective.kp_set 为空，无法组卷", row.ItemVersionID)
	}

	cognitiveLevel, _ := objective["cognitive_level"].(string)
	if cognitiveLevel == "" {
		return MeasurementCandidate{}, fmt.Errorf(
			"assembly: item_version %s 缺 objective.cognitive_level", row.ItemVersionID)
	}
	if !IsValidCognitiveLevel(cognitiveLevel) {
		return MeasurementCandidate{}, fmt.Errorf(
			"assembly: item_version %s cognitive_level %q 越域；合法域 %v",
			row.ItemVersionID, cognitiveLevel, cognitiveLevels)
	}

	lineage := row.Lineage
	if lineage == nil {
		lineage = map[string]any{}
	}
	params, _ := lineage["params"].(map[string]any)
	if params == nil {
		params = map[string]any{}
	}
	rawPrior, present := params["p_correct_prior"]
	if !present || rawPrior == nil {
		return MeasurementCandidate{}, fmt.Errorf(
			"assembly: item_version %s 缺 lineage.params.p_correct_prior", row.ItemVersionID)
	}
	pPrior, err := asFloat(rawPrior)
	if err != nil {
		return MeasurementCandidate{}, fmt.Errorf("assembly: item_version %s p_correct_prior 非法: %v", row.ItemVersionID, err)
	}

	return MeasurementCandidate{
		ItemVersionID:     row.ItemVersionID,
		KpCodes:           kpCodes,
		CognitiveLevel:    cognitiveLevel,
		PCorrect:          pPrior,
		GroupID:           strPtrIfSet(anyToString(params["group_id"])),
		TemplateVersionID: strPtrIfSet(row.TemplateVersionID),
	}, nil
}

func anyToString(v any) string {
	if s, ok := v.(string); ok {
		return s
	}
	return ""
}

// CpSatConflict 单条求解不可行冲突原因（架构 §4.4 铁律；Python CpSatConflict）.
type CpSatConflict struct {
	ConstraintID       string
	Detail             string
	CellContentCode    *string
	CellCognitiveLevel *string
	Required           *int
	Available          *int
}

// CpSatInfeasible 求解不可行报告：含全部冲突约束（Python CpSatInfeasible）。
// 调用方据此决定升级给人类或调整 spec_table/题池。
type CpSatInfeasible struct {
	Conflicts           []CpSatConflict
	CandidatePoolSize   int
	SpecTableTotalCount int
	Seed                int64
}

// Summary 人类可读摘要（错误信息/日志用；Python summary）.
func (r *CpSatInfeasible) Summary() string {
	lines := []string{fmt.Sprintf("CP-SAT 不可行（%d 条冲突）：", len(r.Conflicts))}
	for _, c := range r.Conflicts {
		lines = append(lines, fmt.Sprintf("  - [%s] %s", c.ConstraintID, c.Detail))
	}
	return strings.Join(lines, "\n")
}

// CpSatSolution 求解可行解：入选候选列表 + 留档元数据（Python CpSatSolution）.
type CpSatSolution struct {
	Selected         []MeasurementCandidate
	SpecTableID      string
	SpecTableVersion string
	Seed             int64
	// CellAssignment cell_key 'content_code/cognitive_level' → 入选
	// item_version_id 列表.
	CellAssignment map[string][]string
	// SelectionDigest 选题结果指纹（sha256，确定性留档）.
	SelectionDigest string
}

// IsFeasible 可行解标记（与 CpSatInfeasible 区分）.
func (s *CpSatSolution) IsFeasible() bool { return true }

// SolveResult 是 Solve 的返回联合（Python Union[CpSatSolution, CpSatInfeasible]）.
type SolveResult interface{ solveResult() }

func (s *CpSatSolution) solveResult()   {}
func (r *CpSatInfeasible) solveResult() {}

// SolveOptions 是 Solve 的附加参集（Python solve 关键字形）.
type SolveOptions struct {
	// Seed 确定性种子（同输入同种子同输出）.
	Seed int64
	// ExcludedItemVersionIDs / ExcludedTemplateVersionIDs 曝光互斥集合
	//（同母题不同卷，§4.4 R-Z-02）.
	ExcludedItemVersionIDs     IDSet
	ExcludedTemplateVersionIDs IDSet
	// TimeLimitSeconds 求解时间上限（签名面保留；Go 侧为多项式装填，
	// 无搜索截断语义——字段不参与求解）.
	TimeLimitSeconds *float64
}

// eligibleCount 统计可填入 cell 的合格候选数（曝光互斥已过滤；Python
// _eligible_count）.
func eligibleCount(
	cell *SpecCell,
	pool []MeasurementCandidate,
	excludedItemVersionIDs IDSet,
	excludedTemplateVersionIDs IDSet,
) int {
	n := 0
	for i := range pool {
		c := &pool[i]
		if excludedItemVersionIDs.Has(c.ItemVersionID) {
			continue
		}
		if c.TemplateVersionID != nil && excludedTemplateVersionIDs.Has(*c.TemplateVersionID) {
			continue
		}
		if c.MatchesCell(cell.ContentCode, cell.CognitiveLevel, cell.DifficultyMin, cell.DifficultyMax) {
			n++
		}
	}
	return n
}

// analyzeInfeasibility 分析不可行原因：遍历每个 cell 统计合格候选数与
// target_count 对比（Python _analyze_infeasibility）。为什么独立分析而非依赖
// 求解器内部冲突报告：自研回归分析可控且与 SpecTable schema 紧耦合。无配额
// 缺口但仍不可行 → 记 generic conflict（题组约束或曝光互斥）。
func analyzeInfeasibility(
	specTable *SpecTable,
	candidatePool []MeasurementCandidate,
	seed int64,
	excludedItemVersionIDs IDSet,
	excludedTemplateVersionIDs IDSet,
) *CpSatInfeasible {
	conflicts := []CpSatConflict{}
	for k := range specTable.Cells {
		cell := &specTable.Cells[k]
		el := eligibleCount(cell, candidatePool, excludedItemVersionIDs, excludedTemplateVersionIDs)
		if el < cell.TargetCount {
			content, cognitive := cell.ContentCode, cell.CognitiveLevel
			required, available := cell.TargetCount, el
			conflicts = append(conflicts, CpSatConflict{
				ConstraintID: "cell_quota",
				Detail: fmt.Sprintf("单元格 %s/%s 需 %d 题，但合格候选仅 %d 题（p_correct∈[%v, %v]，曝光互斥已过滤）",
					cell.ContentCode, cell.CognitiveLevel, cell.TargetCount, el, cell.DifficultyMin, cell.DifficultyMax),
				CellContentCode:    &content,
				CellCognitiveLevel: &cognitive,
				Required:           &required,
				Available:          &available,
			})
		}
	}
	if len(conflicts) == 0 {
		// 配额都满足但仍不可行 → 题组整体入选或曝光互斥交叉冲突
		conflicts = append(conflicts, CpSatConflict{
			ConstraintID: "constraint_conflict",
			Detail:       "CP-SAT 不可行但无明显 cell 配额缺口；可能为题组整体入选约束（同 group_id 候选不足同时入选）或曝光互斥与配额交叉冲突",
		})
	}
	return &CpSatInfeasible{
		Conflicts:           conflicts,
		CandidatePoolSize:   len(candidatePool),
		SpecTableTotalCount: specTable.TotalCount(),
		Seed:                seed,
	}
}

// Solve 测量卷求解（Python solve 的 Go 语义等价实现——同一硬约束集、同一
// digest 公式；多解时的具体选取为 Go 侧确定性策略，见包内 solver.go 注释）。
func Solve(specTable *SpecTable, candidatePool []MeasurementCandidate, opts SolveOptions) SolveResult {
	cells := specTable.Cells
	n := len(candidatePool)

	// ── 匹配预计算（Python _build_model 的 y 变量域）──
	matches := make([][]bool, n)
	hasMatch := make([]bool, n)
	for i := range candidatePool {
		matches[i] = make([]bool, len(cells))
		for k := range cells {
			if matchesCellExcluded(&candidatePool[i], &cells[k], opts) {
				matches[i][k] = true
				hasMatch[i] = true
			}
		}
	}

	// ── 装填 ──
	remaining := make([]int, len(cells))
	for k := range cells {
		remaining[k] = cells[k].TargetCount
	}
	matchOf := make([]int, n) // 候选 → cell；-1 = 未分配
	for i := range matchOf {
		matchOf[i] = -1
	}

	// assign 直配（不重路由；题组装填用）
	assign := func(i, k int) {
		matchOf[i] = k
		remaining[k]--
	}
	unassign := func(i int) {
		remaining[matchOf[i]]++
		matchOf[i] = -1
	}
	// augment 单元格 k 经增广路径找一个候选（Kuhn；候选按池内下标序——确定性）。
	// eligible 过滤器限制参与本阶段的候选（阶段 1 只允许孤立候选）.
	visited := make([]bool, n)
	var augment func(k int, eligible func(i int) bool) bool
	augment = func(k int, eligible func(i int) bool) bool {
		for i := 0; i < n; i++ {
			if visited[i] || !matches[i][k] || !eligible(i) {
				continue
			}
			visited[i] = true
			if matchOf[i] == -1 || augment(matchOf[i], func(int) bool { return true }) {
				if matchOf[i] >= 0 {
					remaining[matchOf[i]]++
				}
				matchOf[i] = k
				remaining[k]--
				return true
			}
		}
		return false
	}
	augmentSingle := func(k int, isSingle []bool) bool {
		return augment(k, func(i int) bool { return isSingle[i] })
	}

	// 阶段 1：孤立候选（无 group_id）按 cell 顺序、增广路径装填配额。
	// 题组成员不参与本阶段（整体入选语义禁止拆组装填）.
	isSingle := make([]bool, n)
	for i := range candidatePool {
		isSingle[i] = candidatePool[i].GroupID == nil
	}
	for k := range cells {
		for remaining[k] > 0 {
			for i := range visited {
				visited[i] = false
			}
			if !augmentSingle(k, isSingle) {
				break
			}
		}
	}

	// 阶段 2：题组整体入选（testlet 语义；同 group_id 全有或全无）。
	// 组序 = group_id 字典序（确定性）；组员按池内下标序直配剩余缺口，
	// 不重路由已有分配——任一组员放不下（含被曝光互斥排除或无匹配 cell 的
	// 组员，Python 中 x[i]==0 经组等式传染全组）则整组回滚。
	groupMembers := map[string][]int{}
	var groupIDs []string
	for i := range candidatePool {
		gid := candidatePool[i].GroupID
		if gid == nil {
			continue
		}
		if _, seen := groupMembers[*gid]; !seen {
			groupIDs = append(groupIDs, *gid)
		}
		groupMembers[*gid] = append(groupMembers[*gid], i)
	}
	sort.Strings(groupIDs)
	for _, gid := range groupIDs {
		members := groupMembers[gid]
		placeable := true
		for _, i := range members {
			if !hasMatch[i] {
				placeable = false
				break
			}
		}
		if !placeable {
			continue
		}
		placed := []int{}
		ok := true
		for _, i := range members {
			done := false
			for k := range cells {
				if remaining[k] > 0 && matches[i][k] {
					assign(i, k)
					placed = append(placed, i)
					done = true
					break
				}
			}
			if !done {
				ok = false
				break
			}
		}
		if !ok {
			for _, i := range placed {
				unassign(i)
			}
		}
	}

	// ── 缺口校验：仍有 cell 未填满 → 不可行分析（冻结实现同口径）──
	for k := range cells {
		if remaining[k] > 0 {
			return analyzeInfeasibility(specTable, candidatePool, opts.Seed,
				opts.ExcludedItemVersionIDs, opts.ExcludedTemplateVersionIDs)
		}
	}

	// ── 提取：cell 序为主序、cell 内按池内下标序（Python 提取循环同形）──
	selected := []MeasurementCandidate{}
	cellAssignment := map[string][]string{}
	for k := range cells {
		cellKey := cellKeyOf(cells[k].ContentCode, cells[k].CognitiveLevel)
		cellAssignment[cellKey] = []string{}
		for i := 0; i < n; i++ {
			if matchOf[i] == k {
				selected = append(selected, candidatePool[i])
				cellAssignment[cellKey] = append(cellAssignment[cellKey], candidatePool[i].ItemVersionID)
			}
		}
	}

	ids := make([]string, 0, len(selected))
	for i := range selected {
		ids = append(ids, selected[i].ItemVersionID)
	}
	sort.Strings(ids)
	digest := sha256Hex(strings.Join(ids, "|"))

	return &CpSatSolution{
		Selected:         selected,
		SpecTableID:      specTable.SpecTableID,
		SpecTableVersion: specTable.SpecTableVersion,
		Seed:             opts.Seed,
		CellAssignment:   cellAssignment,
		SelectionDigest:  digest,
	}
}

// matchesCellExcluded 候选-cell 匹配 + 曝光互斥预过滤（_build_model 的
// y 变量域 + Add(x[i]==0) 语义合流）.
func matchesCellExcluded(cand *MeasurementCandidate, cell *SpecCell, opts SolveOptions) bool {
	if opts.ExcludedItemVersionIDs.Has(cand.ItemVersionID) {
		return false
	}
	if cand.TemplateVersionID != nil && opts.ExcludedTemplateVersionIDs.Has(*cand.TemplateVersionID) {
		return false
	}
	return cand.MatchesCell(cell.ContentCode, cell.CognitiveLevel, cell.DifficultyMin, cell.DifficultyMax)
}
