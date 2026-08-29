// heuristic.go 承载组卷求解器 v1：候选预算装填启发式（Python 冻结基准
// src/core/assembly/solver/heuristic.py 的 Go 移植，逐段对齐）。
//
// 架构 v2 §4.4：在线出口毫秒级「候选预算装填+加权启发式修补」；
// 离线测量卷出口的完整求解见同包 solver.go。同一引擎同一题库，仅 Profile
// 与时间预算不同。
//
// 确定性（R-Z-01）：给定（快照 id, Profile 版本, 种子）结果唯一——
//   - 候选顺序 = sha256(seed:item_version_id) 稳定哈希排序（非随机洗牌，
//     不依赖运行时哈希种子，跨进程可复现）；
//   - 贪心装填无时间依赖、无外部状态；
//   - SelectionDigest 固化选题结果，供审计比对重放。
//
// 不可行处理（§4.4 铁律：禁止静默放松）：任何硬约束不满足 → 返回
// *InfeasibleError，携带结构化 ConflictReport（每条冲突含约束 id/知识点/
// 需求量/可用量），缺口报告直接馈送覆盖缺口盘点。
//
// 序列梯度单调：输出按预测正确率降序（由易到难）；无先验的题排末尾，
// 同值按稳定哈希决胜——确定性优先于梯度语义。
package assembly

import (
	"fmt"
	"sort"
	"strings"
)

// ConflictReason 单条冲突原因（禁止静默放松的载体；Python ConflictReason）.
type ConflictReason struct {
	ConstraintID string
	Detail       string
	KpCode       *string
	Required     *int
	Available    *int
}

// ConflictReport 不可行报告：组卷输入三要素 + 全部冲突 + 池规模（馈送缺口
// 盘点；Python ConflictReport）.
type ConflictReport struct {
	SnapshotRef    string
	ProfileID      string
	ProfileVersion string
	Purpose        string
	PoolSize       int
	EligibleSize   int
	DropReasons    map[string]int
	Conflicts      []ConflictReason
}

// InfeasibleError 硬约束不可行（Python InfeasibleError）。Report 为结构化
// 冲突原因（§4.4：必须返回冲突原因）。
type InfeasibleError struct {
	Report ConflictReport
}

func (e *InfeasibleError) Error() string {
	details := make([]string, 0, len(e.Report.Conflicts))
	for _, c := range e.Report.Conflicts {
		details = append(details, c.Detail)
	}
	return fmt.Sprintf("组卷不可行（%d 条冲突）：%s", len(e.Report.Conflicts), strings.Join(details, "; "))
}

// AssemblyResult 组卷结果：已排序选题 + 确定性留档 + 软目标达成情况
// （Python AssemblyResult）.
type AssemblyResult struct {
	Items                 []CandidateItem
	SnapshotRef           string
	ProfileID             string
	ProfileVersion        string
	Purpose               string
	Seed                  int64
	Adjudications         []Adjudication
	SoftTargetAchievement map[string]any
	SelectionDigest       string
}

// AssembleOptions 是 Assemble 的确定性三要素与曝光集入参（Python assemble
// 关键字形）.
type AssembleOptions struct {
	// Seed 确定性种子.
	Seed int64
	// SnapshotRef 内容快照引用（确定性三要素之一，留档）.
	SnapshotRef string
	// ExcludedItemVersionIDs / ExcludedTemplateVersionIDs 曝光集（曝光账本
	// 查询结果；学生轨或周队列轨）.
	ExcludedItemVersionIDs     IDSet
	ExcludedTemplateVersionIDs IDSet
}

// unit 选择单元：单题或整个题组（题组≤6，R-Z-06；Python _Unit）.
type unit struct {
	members            []CandidateItem
	sortKey            string
	kpCodes            []string
	templateVersionIDs map[string]struct{}
	isIsolated         bool
	selected           bool // Python `unit in selected` 的身份判定同义
}

func (u *unit) size() int { return len(u.members) }

// stableKey 确定性排序键：sha256(seed:id)。跨进程可复现（Python _stable_key）.
func stableKey(seed int64, itemVersionID string) string {
	return sha256Hex(fmt.Sprintf("%d:%s", seed, itemVersionID))
}

// filterEligible 候选筛选（学段×用途许可×曝光历史×目标正确率×诊断关系核验），
// 返回（合格候选, 淘汰原因计数）（Python _filter_eligible）。淘汰原因计数进
// ConflictReport.DropReasons——缺口盘点的原料。
func filterEligible(
	profile *AssemblyProfile,
	candidates []CandidateItem,
	kpScope map[string]struct{},
	excludedItemVersionIDs IDSet,
	excludedTemplateVersionIDs IDSet,
) ([]CandidateItem, map[string]int) {
	c := &profile.Constraints
	eligible := []CandidateItem{}
	drops := map[string]int{}
	drop := func(reason string) { drops[reason]++ }

	var widened *[2]float64
	if c.TargetPCorrectRange != nil {
		lo, hi := c.TargetPCorrectRange[0], c.TargetPCorrectRange[1]
		m := c.PCorrectUncertaintyMargin
		// 冷启动降级（§4.4）：纯先验区间 + 保守宽度
		w := [2]float64{maxF(0.0, lo-m), minF(1.0, hi+m)}
		widened = &w
	}

	for _, item := range candidates {
		if item.Gradeband != profile.Gradeband {
			drop("gradeband_mismatch")
			continue
		}
		inScope := false
		for _, k := range item.KpCodes {
			if _, ok := kpScope[k]; ok {
				inScope = true
				break
			}
		}
		if !inScope {
			drop("kp_out_of_scope")
			continue
		}
		licensed := false
		for _, p := range item.AllowedPurposes {
			if p == profile.Purpose {
				licensed = true
				break
			}
		}
		if !licensed {
			drop("purpose_not_licensed")
			continue
		}
		if c.ExposureMutexCrossPeriod && excludedItemVersionIDs.Has(item.ItemVersionID) {
			drop("exposed_item")
			continue
		}
		if c.ExposureMutexSameTemplate && item.TemplateVersionID != nil &&
			excludedTemplateVersionIDs.Has(*item.TemplateVersionID) {
			drop("exposed_template")
			continue
		}
		if widened != nil {
			if item.PCorrectPrior == nil {
				// 无先验且 Profile 要求正确率区间：无法代入约束，淘汰并记录
				drop("missing_p_correct_prior")
				continue
			}
			// Python: not (widened[0] <= prior <= widened[1])
			if *item.PCorrectPrior < (*widened)[0] || *item.PCorrectPrior > (*widened)[1] {
				drop("p_correct_out_of_range")
				continue
			}
		}
		if c.MultiPointRelationCheck && !item.IsIsolated() {
			// R-Z-03 多点关系声明核验：多点题必须显式声明
			// all_required / compensatory；声明 single 而挂多点是自相矛盾
			if item.KpSetMode != KpSetModeAllRequired && item.KpSetMode != KpSetModeCompensatory {
				drop("relation_declaration_invalid")
				continue
			}
		}
		eligible = append(eligible, item)
	}
	return eligible, drops
}

// Assemble 确定性预算装填组卷（Python assemble）。
//
// profile 为编译后的版本化 Profile（CompileProfile / DiagnosisProfile 产物）；
// candidates 候选池须为同一次快照内容。返回 AssemblyResult（Items 已按梯度
// 单调排序）；硬约束不可行时返回 *InfeasibleError（禁止静默放松）。
func Assemble(profile *AssemblyProfile, candidates []CandidateItem, opts AssembleOptions) (*AssemblyResult, error) {
	c := &profile.Constraints
	kpScope := map[string]struct{}{}
	for _, q := range c.KpQuotas {
		kpScope[q.KpCode] = struct{}{}
	}

	eligible, drops := filterEligible(
		profile, candidates, kpScope,
		opts.ExcludedItemVersionIDs, opts.ExcludedTemplateVersionIDs,
	)

	// ── 组单元（题组整体；单题自成单元），确定性排序 ──
	groups := map[string][]CandidateItem{}
	var groupOrder []string
	singles := []CandidateItem{}
	for _, item := range eligible {
		if item.GroupID != nil {
			if _, seen := groups[*item.GroupID]; !seen {
				groupOrder = append(groupOrder, *item.GroupID)
			}
			groups[*item.GroupID] = append(groups[*item.GroupID], item)
		} else {
			singles = append(singles, item)
		}
	}

	conflicts := []ConflictReason{}
	units := []*unit{}
	for _, item := range singles {
		units = append(units, newUnit([]CandidateItem{item}, stableKey(opts.Seed, item.ItemVersionID)))
	}
	// Python `groups.items()` 为插入序（合格池首现序）；groupOrder 保留同一
	// 插入序，不按键重排——与冻结实现的单元遍历序逐位一致.
	for _, gid := range groupOrder {
		members := groups[gid]
		sort.SliceStable(members, func(i, j int) bool {
			return stableKey(opts.Seed, members[i].ItemVersionID) < stableKey(opts.Seed, members[j].ItemVersionID)
		})
		if len(members) > c.MaxItemsPerGroup {
			conflicts = append(conflicts, ConflictReason{
				ConstraintID: "max_items_per_group",
				Detail: fmt.Sprintf("题组 %s 含 %d 题，超过题组上限 %d（R-Z-06）",
					gid, len(members), c.MaxItemsPerGroup),
				Required:  intPtr(c.MaxItemsPerGroup),
				Available: intPtr(len(members)),
			})
			continue
		}
		units = append(units, newUnit(members, stableKey(opts.Seed, gid)))
	}
	sort.SliceStable(units, func(i, j int) bool { return units[i].sortKey < units[j].sortKey })

	// ── 装填 ──
	selected := []*unit{}
	usedTemplates := map[string]struct{}{}

	templateOK := func(u *unit) bool {
		if !c.ExposureMutexSameTemplate {
			return true
		}
		// R-Z-02 同母题不同卷：同卷内同母题实例至多一个
		for t := range u.templateVersionIDs {
			if _, used := usedTemplates[t]; used {
				return false
			}
		}
		return true
	}
	take := func(u *unit) {
		u.selected = true
		selected = append(selected, u)
		for t := range u.templateVersionIDs {
			usedTemplates[t] = struct{}{}
		}
	}
	kpCount := func(kpCode string, isolatedOnly bool) int {
		n := 0
		for _, u := range selected {
			if isolatedOnly {
				if u.isIsolated && len(u.kpCodes) == 1 && u.kpCodes[0] == kpCode {
					n += u.size()
				}
			} else {
				for _, k := range u.kpCodes {
					if k == kpCode {
						n += u.size()
						break
					}
				}
			}
		}
		return n
	}
	totalItems := func() int {
		n := 0
		for _, u := range selected {
			n += u.size()
		}
		return n
	}

	// 阶段 A：知识点配额定题（诊断：孤立题配额）
	for _, quota := range c.KpQuotas {
		deficit := quota.MinCount - kpCount(quota.KpCode, quota.IsolatedOnly)
		for _, u := range units {
			if deficit <= 0 {
				break
			}
			if u.selected {
				continue
			}
			if quota.IsolatedOnly {
				if !(u.isIsolated && len(u.kpCodes) == 1 && u.kpCodes[0] == quota.KpCode) {
					continue
				}
			} else {
				found := false
				for _, k := range u.kpCodes {
					if k == quota.KpCode {
						found = true
						break
					}
				}
				if !found {
					continue
				}
			}
			if !templateOK(u) {
				continue
			}
			take(u)
			deficit = quota.MinCount - kpCount(quota.KpCode, quota.IsolatedOnly)
		}
		if deficit > 0 {
			kind := "kp_quota"
			if quota.IsolatedOnly {
				kind = "kp_quota_isolated"
			}
			noun := "题"
			if quota.IsolatedOnly {
				noun = "孤立题"
			}
			conflicts = append(conflicts, ConflictReason{
				ConstraintID: kind,
				Detail: fmt.Sprintf("知识点 %s 需要%s≥%d，合格池仅可提供 %d",
					quota.KpCode, noun, quota.MinCount, kpCount(quota.KpCode, quota.IsolatedOnly)),
				KpCode:    strPtr(quota.KpCode),
				Required:  intPtr(quota.MinCount),
				Available: intPtr(kpCount(quota.KpCode, quota.IsolatedOnly)),
			})
		}
	}

	// 阶段 B：题量下限装填（内容配比软目标加权：配比缺口大的标签优先）
	mixDeficit := func(tag *string) float64 {
		if c.ContentMix == nil || tag == nil {
			return 0.0
		}
		target, ok := c.ContentMix.Ratios[*tag]
		if !ok {
			return 0.0
		}
		total := totalItems()
		current := 0
		for _, u := range selected {
			for _, m := range u.members {
				if m.MixTag != nil && *m.MixTag == *tag {
					current += u.size()
					break
				}
			}
		}
		// 缺口 = 目标下限×(当前总数+1) − 已有；>0 表示该标签欠账
		return target[0]*float64(total+1) - float64(current)
	}

	for totalItems() < c.ItemCount.Min {
		var best *unit
		bestScore := 0.0
		for _, u := range units {
			if u.selected || !templateOK(u) {
				continue
			}
			if !c.ItemCount.Soft && totalItems()+u.size() > c.ItemCount.Max {
				continue
			}
			score := 0.0
			for _, m := range u.members {
				if d := mixDeficit(m.MixTag); d > score {
					score = d
				}
			}
			if best == nil || score > bestScore {
				best = u
				bestScore = score
			}
		}
		if best == nil {
			conflicts = append(conflicts, ConflictReason{
				ConstraintID: "item_count",
				Detail: fmt.Sprintf("题量下限 %d 不可达：合格池 %d 题，已装填 %d，剩余候选受题量上限/同母题互斥约束不可用",
					c.ItemCount.Min, len(eligible), totalItems()),
				Required:  intPtr(c.ItemCount.Min),
				Available: intPtr(totalItems()),
			})
			break
		}
		take(best)
	}

	// 阶段 C：硬上限校验（soft 上限超出不判不可行，记录达成情况）
	achievement := map[string]any{}
	if c.ItemCount.Soft && totalItems() > c.ItemCount.Max {
		achievement["item_count"] = map[string]any{
			"soft_max":    c.ItemCount.Max,
			"actual":      totalItems(),
			"exceeded_by": totalItems() - c.ItemCount.Max,
		}
	}

	if len(conflicts) > 0 {
		return nil, &InfeasibleError{
			Report: ConflictReport{
				SnapshotRef:    opts.SnapshotRef,
				ProfileID:      profile.ProfileID,
				ProfileVersion: profile.ProfileVersion,
				Purpose:        profile.Purpose,
				PoolSize:       len(candidates),
				EligibleSize:   len(eligible),
				DropReasons:    drops,
				Conflicts:      conflicts,
			},
		}
	}

	// ── 序列梯度单调：预测正确率降序（由易到难），无先验排末尾 ──
	orderedItems := []CandidateItem{}
	var flat []CandidateItem
	for _, u := range selected {
		flat = append(flat, u.members...)
	}
	if c.GradientMonotone {
		sort.SliceStable(flat, func(i, j int) bool {
			a, b := &flat[i], &flat[j]
			aNone, bNone := a.PCorrectPrior == nil, b.PCorrectPrior == nil
			if aNone != bNone {
				return !aNone // 有先验在前，None 排末尾
			}
			if !aNone {
				pa, pb := *a.PCorrectPrior, *b.PCorrectPrior
				if pa != pb {
					return pa > pb // 降序（由易到难）
				}
			}
			return stableKey(opts.Seed, a.ItemVersionID) < stableKey(opts.Seed, b.ItemVersionID)
		})
	}
	orderedItems = flat

	ids := make([]string, 0, len(orderedItems))
	for _, m := range orderedItems {
		ids = append(ids, m.ItemVersionID)
	}
	digest := sha256Hex(strings.Join(ids, "|"))

	adjs := append([]Adjudication(nil), profile.Adjudications...)
	return &AssemblyResult{
		Items:                 orderedItems,
		SnapshotRef:           opts.SnapshotRef,
		ProfileID:             profile.ProfileID,
		ProfileVersion:        profile.ProfileVersion,
		Purpose:               profile.Purpose,
		Seed:                  opts.Seed,
		Adjudications:         adjs,
		SoftTargetAchievement: achievement,
		SelectionDigest:       digest,
	}, nil
}

// newUnit 构造选择单元（Python _Unit.__init__：kp 去重保序、母题集、孤立判定）.
func newUnit(members []CandidateItem, sortKey string) *unit {
	kps := []string{}
	for _, m := range members {
		for _, code := range m.KpCodes {
			if !containsStr(kps, code) {
				kps = append(kps, code)
			}
		}
	}
	tplIDs := map[string]struct{}{}
	for _, m := range members {
		if m.TemplateVersionID != nil {
			tplIDs[*m.TemplateVersionID] = struct{}{}
		}
	}
	isolated := len(members) == 1 && members[0].IsIsolated()
	return &unit{
		members:            members,
		sortKey:            sortKey,
		kpCodes:            kps,
		templateVersionIDs: tplIDs,
		isIsolated:         isolated,
	}
}

func containsStr(list []string, s string) bool {
	for _, e := range list {
		if e == s {
			return true
		}
	}
	return false
}

func intPtr(n int) *int       { return &n }
func strPtr(s string) *string { return &s }
func maxF(a, b float64) float64 {
	if a > b {
		return a
	}
	return b
}
func minF(a, b float64) float64 {
	if a < b {
		return a
	}
	return b
}
