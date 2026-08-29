// profile.go 承载约束集四维编译（Python 冻结基准
// src/core/assembly/profile.py 的 Go 移植，逐函数对齐）。
//
// 架构 v2 §4.4：AssemblyProfile = base ∪ subject_overlay ∪ purpose_overlay
// ∪ gradeband_overlay，四维均为版本化配置，编译时做冲突检测。
//   - ConstraintSet：编译后的机器可执行约束集（题量/知识点配比/目标正确率
//     区间/序列梯度单调/曝光互斥/题组≤6，R-Z-02）；
//   - CompileProfile：四维合并 + 冲突检测 + 按预置优先级裁决（Adjudication 留档）；
//   - DiagnosisProfile：诊断用途 Profile 工厂（孤立题强制、每知识点≥3、
//     多点关系声明核验，R-Z-03）。
//
// 已知冲突的预置裁决（架构评审报告 §344 路径①）：「约 20 题」×「每知识点≥3」
// 在知识点多的单元不可同时满足 → 每知识点最低题量是硬约束（R-Z-03），题量
// 上限软目标化并记录理由。裁决发生在编译期而非求解期——求解器看到的
// ConstraintSet 已无冲突，禁止求解期静默放松（§4.4）。
package assembly

import (
	"fmt"
)

// 用途与学段值域（Python Literal["practice","diagnosis","measurement"] /
// Literal["L","M","H"]；与 core/session 场景与学段常量同值域，本地常量化
// 避免核心域间为两个字符串建立编译耦合）。
const (
	PurposePractice    = "practice"
	PurposeDiagnosis   = "diagnosis"
	PurposeMeasurement = "measurement"

	GradebandL = "L"
	GradebandM = "M"
	GradebandH = "H"

	// MaxItemsPerGroup 题组 ≤6（R-Z-06，DB 层 ck_ig_max_six_items 兜底）.
	MaxItemsPerGroup = 6

	// DefaultPCorrectMargin 冷启动降级的保守宽度默认值（§4.4：无学生/cohort
	// 数据时以纯先验区间+保守宽度代入约束求解，数据回流后按周收紧）.
	DefaultPCorrectMargin = 0.10
)

// ErrProfileConflict 是 ProfileConflictError 的类型载体（Python
// ProfileConflictError(ValueError)：编译期不可裁决冲突，携带 conflict_id）。
type ProfileConflictError struct {
	ConflictID string
	Detail     string
}

func (e *ProfileConflictError) Error() string {
	return fmt.Sprintf("[%s] %s", e.ConflictID, e.Detail)
}

// ItemCountRule 题量约束（Python ItemCountRule）。
//
// Soft=true 表示该上限已被编译期裁决为软目标（如「约 20 题」）：求解器可
// 超出，但必须在结果的 SoftTargetAchievement 中记录超出量。Min 永远是硬
// 约束（题量不足=卷不成立）。
type ItemCountRule struct {
	Min  int
	Max  int
	Soft bool
}

// KpQuota 知识点配比约束（Python KpQuota）：某知识点在卷中的最低题量。
//
// IsolatedOnly=true（诊断）：只统计孤立题（单知识点、kp_set_mode='single'），
// 多点题只佐证不定位，不计入该配额（§4.5：定位必须由孤立题完成）。
type KpQuota struct {
	KpCode       string
	MinCount     int
	IsolatedOnly bool
}

// ContentMixRule 内容配比软目标（Python ContentMixRule；新学/复习/易混淆
// 交错，R-Z-02）。v1 为软目标：候选池标签不足时不判不可行，在结果中记录
// 达成率；硬约束化需等业务明确配比违例的处置策略（留档开放项）。
// Ratios：tag（new/review/confusable）→ 目标占比区间 [lo, hi]。
type ContentMixRule struct {
	Ratios map[string][2]float64
}

// ConstraintSet 编译后的机器可执行约束集（Python ConstraintSet；
// R-Z-02 全量 + R-Z-03 诊断扩展）。
type ConstraintSet struct {
	ItemCount ItemCountRule
	KpQuotas  []KpQuota
	// TargetPCorrectRange 目标正确率区间（nil=不约束）；配合 Margin 做冷启动保守加宽.
	TargetPCorrectRange       *[2]float64
	PCorrectUncertaintyMargin float64
	// GradientMonotone 序列梯度单调：输出序列按预测正确率降序（由易到难）.
	GradientMonotone bool
	// ExposureMutexSameTemplate / ExposureMutexCrossPeriod 曝光互斥（R-Z-02）：
	// 同母题不同卷 / 跨期不重复.
	ExposureMutexSameTemplate bool
	ExposureMutexCrossPeriod  bool
	// MaxItemsPerGroup 题组 ≤6（R-Z-06）.
	MaxItemsPerGroup int
	// ContentMix 内容配比软目标（nil=无）.
	ContentMix *ContentMixRule
	// RequireIsolatedItems / MultiPointRelationCheck 诊断硬约束（R-Z-03）.
	RequireIsolatedItems    bool
	MultiPointRelationCheck bool
}

// Adjudication 编译期冲突裁决记录（§4.4：按预置优先级裁决并记录理由）.
type Adjudication struct {
	ConflictID  string
	ConstraintA string
	ConstraintB string
	Decision    string
	Reason      string
}

// AssemblyProfile 版本化组卷 Profile（确定性三要素之一：快照+Profile版本+种子）.
type AssemblyProfile struct {
	ProfileID      string
	ProfileVersion string
	Purpose        string
	Gradeband      string
	Constraints    ConstraintSet
	Adjudications  []Adjudication
	// OverlayRefs 四维来源留档（审计用）：subject/purpose/gradeband overlay 的 id@version.
	OverlayRefs map[string]string
}

// Digest 计算 Profile 内容指纹（Python AssemblyProfile.digest：确定性——
// 同内容必同指纹）。规范化规则与 Python json.dumps(model_dump(mode="json"),
// sort_keys=True, ensure_ascii=False) 逐字节对齐（canon.go），跨实现可互验。
func (p *AssemblyProfile) Digest() string {
	dump := map[string]any{
		"profile_id":      p.ProfileID,
		"profile_version": p.ProfileVersion,
		"purpose":         p.Purpose,
		"gradeband":       p.Gradeband,
		"adjudications":   adjudicationsDump(p.Adjudications),
		"overlay_refs":    strMapDump(p.OverlayRefs),
		"constraints":     p.Constraints.dump(),
	}
	return sha256Hex(canonJSONForDigest(dump))
}

// adjudicationsDump 把裁决列表转为 Python model_dump 同形值树.
func adjudicationsDump(adjs []Adjudication) []any {
	out := make([]any, 0, len(adjs))
	for _, a := range adjs {
		out = append(out, map[string]any{
			"conflict_id":  a.ConflictID,
			"constraint_a": a.ConstraintA,
			"constraint_b": a.ConstraintB,
			"decision":     a.Decision,
			"reason":       a.Reason,
		})
	}
	return out
}

// strMapDump 空映射出 {}（Python dict 默认序列化同形；nil 与空同义）.
func strMapDump(m map[string]string) any {
	out := map[string]any{}
	for k, v := range m {
		out[k] = v
	}
	return out
}

// dump 把约束集转为 Python ConstraintSet.model_dump(mode="json") 同形值树.
func (c *ConstraintSet) dump() map[string]any {
	quotas := make([]any, 0, len(c.KpQuotas))
	for _, q := range c.KpQuotas {
		quotas = append(quotas, map[string]any{
			"kp_code":       q.KpCode,
			"min_count":     q.MinCount,
			"isolated_only": q.IsolatedOnly,
		})
	}
	var mix any
	if c.ContentMix != nil {
		ratios := map[string]any{}
		for tag, r := range c.ContentMix.Ratios {
			ratios[tag] = []any{r[0], r[1]}
		}
		mix = map[string]any{"ratios": ratios}
	}
	var pRange any
	if c.TargetPCorrectRange != nil {
		pRange = []any{c.TargetPCorrectRange[0], c.TargetPCorrectRange[1]}
	}
	return map[string]any{
		"item_count": map[string]any{
			"min":  c.ItemCount.Min,
			"max":  c.ItemCount.Max,
			"soft": c.ItemCount.Soft,
		},
		"kp_quotas":                    quotas,
		"target_p_correct_range":       pRange,
		"p_correct_uncertainty_margin": c.PCorrectUncertaintyMargin,
		"gradient_monotone":            c.GradientMonotone,
		"exposure_mutex_same_template": c.ExposureMutexSameTemplate,
		"exposure_mutex_cross_period":  c.ExposureMutexCrossPeriod,
		"max_items_per_group":          c.MaxItemsPerGroup,
		"content_mix":                  mix,
		"require_isolated_items":       c.RequireIsolatedItems,
		"multi_point_relation_check":   c.MultiPointRelationCheck,
	}
}

// canonJSONForDigest 是 canonicalJSON 的包内别名（digest 专用入口，便于
// 测试直接比对规范化串）.
func canonJSONForDigest(v any) string { return canonicalJSON(v) }

// overlayGet 从 overlay dict 按路径取值（Python _overlay_get：不存在返回 nil）.
func overlayGet(overlay map[string]any, path ...string) any {
	var node any = overlay
	for _, key := range path {
		m, ok := node.(map[string]any)
		if !ok {
			return nil
		}
		node, ok = m[key]
		if !ok {
			return nil
		}
	}
	return node
}

// CompileInput 是 CompileProfile 的请求参集（Python compile_profile 关键字形）.
type CompileInput struct {
	ProfileID      string
	ProfileVersion string
	Purpose        string
	Gradeband      string
	// KpCodes 本次组卷的知识点范围（快照内容；快照 id 由调用方传给求解器）.
	KpCodes []string
	// Base / SubjectOverlay / PurposeOverlay / GradebandOverlay 四维版本化配置
	//（dict 形式；SubjectOverlay 即学科包 assembly-overlays yaml 的内容）.
	Base             map[string]any
	SubjectOverlay   map[string]any
	PurposeOverlay   map[string]any
	GradebandOverlay map[string]any
	// MinItemsPerKp 每知识点最低题量覆盖（nil=按用途默认：诊断 3，其他 1）.
	MinItemsPerKp *int
	// AllowItemCountSoft 冲突时是否允许把题量上限裁决为软目标
	//（false 时冲突返回 ProfileConflictError——测量等场景的严格模式）；
	// nil 视同 true（Python 默认值）.
	AllowItemCountSoft *bool
}

// CompileProfile 四维编译：合并 base/subject/purpose/gradeband overlay 为
// 约束集并裁决冲突（Python compile_profile）。
//
// 合并优先级（高覆盖低）：gradeband_overlay > purpose_overlay >
// subject_overlay > base。这是架构 §4.4「四维编译」的默认顺序：学段参数最
// 贴近学生安全（低段时长/题量保护），优先级最高。
func CompileProfile(in CompileInput) (*AssemblyProfile, error) {
	switch in.Purpose {
	case PurposePractice, PurposeDiagnosis, PurposeMeasurement:
	default:
		return nil, fmt.Errorf("assembly: purpose %q 越域；合法域 [practice, diagnosis, measurement]", in.Purpose)
	}
	switch in.Gradeband {
	case GradebandL, GradebandM, GradebandH:
	default:
		return nil, fmt.Errorf("assembly: gradeband %q 越域；合法域 [L, M, H]", in.Gradeband)
	}

	overlays := []map[string]any{in.Base, in.SubjectOverlay, in.PurposeOverlay, in.GradebandOverlay}

	// ── 题量（取最高优先级定义了 item_count_range 的维度）──
	// 正向遍历 overlays（低→高优先级），高优先级后定义者覆盖低优先级。
	// Python `x or y` 的 falsy 语义：空列表/None 不覆盖前一维。
	countRange := []int(nil)
	for _, ov := range overlays {
		if r := overlayIntList(ov, "item_count_range"); len(r) > 0 {
			countRange = r
		}
	}
	if countRange == nil {
		// 平台默认：练习卷 10–20 题（无 overlay 时的保守默认）
		countRange = []int{10, 20}
	}
	if countRange[0] < 1 || countRange[1] < 1 {
		return nil, fmt.Errorf("assembly: item_count_range %v 越域（min/max 必须 ≥ 1）", countRange)
	}
	itemCount := ItemCountRule{Min: countRange[0], Max: countRange[1]}

	// ── 知识点配比 ──
	minPerKp := 1
	if in.Purpose == PurposeDiagnosis {
		minPerKp = 3
	}
	if in.MinItemsPerKp != nil {
		minPerKp = *in.MinItemsPerKp
	}
	if minPerKp < 1 {
		return nil, fmt.Errorf("assembly: min_items_per_kp = %d 越域（必须 ≥ 1）", minPerKp)
	}
	isolatedOnly := in.Purpose == PurposeDiagnosis
	quotas := make([]KpQuota, 0, len(in.KpCodes))
	for _, code := range in.KpCodes {
		quotas = append(quotas, KpQuota{KpCode: code, MinCount: minPerKp, IsolatedOnly: isolatedOnly})
	}

	// ── 目标正确率区间 ──
	var pRange *[2]float64
	margin := DefaultPCorrectMargin
	for _, ov := range overlays {
		if rng, ok := overlayGet(ov, "difficulty_target", "target_p_correct_range").([]any); ok && len(rng) == 2 {
			lo, err1 := asFloat(rng[0])
			hi, err2 := asFloat(rng[1])
			if err1 != nil || err2 != nil {
				return nil, fmt.Errorf("assembly: target_p_correct_range %v 非法（须为两数值）", rng)
			}
			pRange = &[2]float64{lo, hi}
		}
		if m, ok := overlayGet(ov, "difficulty_target", "uncertainty_margin").(any); ok && m != nil {
			f, err := asFloat(m)
			if err != nil {
				return nil, fmt.Errorf("assembly: uncertainty_margin %v 非法（须为数值）", m)
			}
			margin = f
		}
	}

	// ── 通用开关（subject_overlay 的 assembly_constraints 维度）──
	gradient := true
	sameTemplate := true
	crossPeriod := true
	var contentMix *ContentMixRule
	for _, ov := range overlays {
		if g, ok := overlayGet(ov, "assembly_constraints", "require_gradient_monotone").(bool); ok {
			gradient = g
		}
		if st, ok := overlayGet(ov, "assembly_constraints", "exposure_mutex", "same_template_different_paper").(bool); ok {
			sameTemplate = st
		}
		if cp, ok := overlayGet(ov, "assembly_constraints", "exposure_mutex", "cross_period_repeat").(bool); ok {
			// yaml 语义：cross_period_repeat=False 表示「不允许跨期重复」=互斥开
			crossPeriod = !cp
		}
		if mix, ok := overlayGet(ov, "assembly_constraints", "content_mix").(map[string]any); ok {
			ratios := map[string][2]float64{}
			for key, tag := range map[string]string{
				"new_learning_ratio": "new",
				"review_ratio":       "review",
				"confusable_ratio":   "confusable",
			} {
				if pair, ok := mix[key].([]any); ok && len(pair) == 2 {
					lo, err1 := asFloat(pair[0])
					hi, err2 := asFloat(pair[1])
					if err1 != nil || err2 != nil {
						return nil, fmt.Errorf("assembly: content_mix.%s 非法（须为两数值）", key)
					}
					ratios[tag] = [2]float64{lo, hi}
				}
			}
			if len(ratios) > 0 {
				contentMix = &ContentMixRule{Ratios: ratios}
			}
		}
	}

	// ── 诊断硬约束（R-Z-03；purpose_overlay 可显式覆盖，默认随用途开启）──
	requireIsolated := in.Purpose == PurposeDiagnosis
	relationCheck := in.Purpose == PurposeDiagnosis
	if iso, ok := overlayGet(in.PurposeOverlay, "isolation_rules", "require_isolated_items").(bool); ok {
		requireIsolated = iso
	}
	if rel, ok := overlayGet(in.PurposeOverlay, "isolation_rules", "multi_point_relation_check").(bool); ok {
		relationCheck = rel
	}

	constraints := ConstraintSet{
		ItemCount:                 itemCount,
		KpQuotas:                  quotas,
		TargetPCorrectRange:       pRange,
		PCorrectUncertaintyMargin: margin,
		GradientMonotone:          gradient,
		ExposureMutexSameTemplate: sameTemplate,
		ExposureMutexCrossPeriod:  crossPeriod,
		MaxItemsPerGroup:          MaxItemsPerGroup,
		ContentMix:                contentMix,
		RequireIsolatedItems:      requireIsolated,
		MultiPointRelationCheck:   relationCheck,
	}

	// ── 冲突检测与裁决（§4.4）──
	adjudications := []Adjudication{}
	minRequired := 0
	for _, q := range quotas {
		minRequired += q.MinCount
	}
	allowSoft := true
	if in.AllowItemCountSoft != nil {
		allowSoft = *in.AllowItemCountSoft
	}
	if minRequired > constraints.ItemCount.Max {
		conflictID := "item_count_vs_kp_quota"
		detail := fmt.Sprintf(
			"知识点最低题量合计 %d（%d 点 × 每点≥%d）超出题量上限 %d",
			minRequired, len(quotas), minPerKp, constraints.ItemCount.Max,
		)
		if !allowSoft {
			return nil, &ProfileConflictError{ConflictID: conflictID, Detail: detail}
		}
		// 预置优先级：每知识点最低题量（R-Z-03，诊断归因统计基础）> 题量上限
		//（评审报告 §344 路径①：「约 20 题」改软目标）
		constraints.ItemCount.Soft = true
		adjudications = append(adjudications, Adjudication{
			ConflictID:  conflictID,
			ConstraintA: "item_count.max",
			ConstraintB: "kp_quotas.min_count",
			Decision:    "soft_target",
			Reason: fmt.Sprintf(
				"%s；按预置优先级裁决：每知识点最低题量为硬约束（R-Z-03），题量上限软目标化（架构评审报告路径①），超出量将在组卷结果 soft_target_achievement 中记录",
				detail,
			),
		})
	}
	// min 也不得低于知识点配额合计（否则配额永远不可行且无解说不清）
	if constraints.ItemCount.Min < minRequired {
		constraints.ItemCount.Min = minRequired
		adjudications = append(adjudications, Adjudication{
			ConflictID:  "item_count_min_raised",
			ConstraintA: "item_count.min",
			ConstraintB: "kp_quotas.min_count",
			Decision:    "raise_min",
			Reason: fmt.Sprintf(
				"题量下限上调至知识点最低题量合计 %d，保证配额约束在数学上可达（非放松，是消除自相矛盾）",
				minRequired,
			),
		})
	}

	overlayRefs := map[string]string{}
	for name, ov := range map[string]map[string]any{
		"subject":   in.SubjectOverlay,
		"purpose":   in.PurposeOverlay,
		"gradeband": in.GradebandOverlay,
	} {
		if ov == nil {
			continue
		}
		if id, ok := ov["overlay_id"].(string); ok && id != "" {
			ver := "?"
			if v, ok := ov["overlay_version"].(string); ok && v != "" {
				ver = v
			}
			overlayRefs[name] = fmt.Sprintf("%s@%s", id, ver)
		}
	}

	return &AssemblyProfile{
		ProfileID:      in.ProfileID,
		ProfileVersion: in.ProfileVersion,
		Purpose:        in.Purpose,
		Gradeband:      in.Gradeband,
		Constraints:    constraints,
		Adjudications:  adjudications,
		OverlayRefs:    overlayRefs,
	}, nil
}

// overlayIntList 取 overlay 顶层的整型列表值（item_count_range；元素兼容
// int/float64——YAML 解出的整型在 map[string]any 里是 int）.
func overlayIntList(ov map[string]any, key string) []int {
	raw, ok := overlayGet(ov, key).([]any)
	if !ok {
		return nil
	}
	out := make([]int, 0, len(raw))
	for _, e := range raw {
		n, err := asInt(e)
		if err != nil {
			return nil
		}
		out = append(out, n)
	}
	return out
}

// asFloat 把 map 携带的标量（int/int64/float64）转为 float64.
func asFloat(v any) (float64, error) {
	switch t := v.(type) {
	case int:
		return float64(t), nil
	case int64:
		return float64(t), nil
	case float64:
		return t, nil
	default:
		return 0, fmt.Errorf("assembly: %T 不是数值", v)
	}
}

// asInt 把 map 携带的标量转为 int（float64 仅在整数值时接受）.
func asInt(v any) (int, error) {
	switch t := v.(type) {
	case int:
		return t, nil
	case int64:
		return int(t), nil
	case float64:
		if t == float64(int(t)) {
			return int(t), nil
		}
		return 0, fmt.Errorf("assembly: %v 不是整型", v)
	default:
		return 0, fmt.Errorf("assembly: %T 不是整型", v)
	}
}

// DiagnosisInput 是 DiagnosisProfile 的请求参集（Python diagnosis_profile
// 关键字形；默认值：题量 (20,20)、每点≥3、目标正确率 (0.30,0.85)）.
type DiagnosisInput struct {
	ProfileID      string
	ProfileVersion string
	Gradeband      string
	KpCodes        []string
	ItemCountRange *[2]int
	// MinItemsPerIsolatedKp 每知识点最低孤立题量（nil=3）.
	MinItemsPerIsolatedKp *int
	// TargetPCorrectRange nil 表示不注入难度目标（Python Optional 默认
	// (0.30,0.85)；显式 None 才是不注入——Go 以 nil 指针表达「显式 None」，
	// 以零长切片 [2]float64{} 不可行，故用 *「不传」与「传 None」合并语义：
	// nil = 用默认 (0.30,0.85)。需要显式不注入时用 TargetPCorrectRangeNone）.
	TargetPCorrectRange *[2]float64
	TargetPCorrectNone  bool
	SubjectOverlay      map[string]any
	GradebandOverlay    map[string]any
}

// DiagnosisProfile 诊断 Profile 工厂（R-Z-03 三硬约束 + 已知冲突软目标化裁决）.
//
//   - 孤立题强制存在：require_isolated_items=true，kp 配额 isolated_only=true
//   - 每知识点最低题量 ≥3（min_items_per_isolated_kp）
//   - 多点关系声明核验：multi_point_relation_check=true
//   - 「约 20 题」×「每点≥3」已知冲突：编译期按预置优先级把题量上限
//     软目标化并记录理由（架构评审报告路径①）
func DiagnosisProfile(in DiagnosisInput) (*AssemblyProfile, error) {
	countRange := [2]int{20, 20}
	if in.ItemCountRange != nil {
		countRange = *in.ItemCountRange
	}
	purposeOverlay := map[string]any{
		"overlay_id":      fmt.Sprintf("%s-purpose", in.ProfileID),
		"overlay_version": in.ProfileVersion,
		"item_count_range": []any{
			intToAny(countRange[0]),
			intToAny(countRange[1]),
		},
		"isolation_rules": map[string]any{
			"require_isolated_items":     true,
			"multi_point_relation_check": true,
		},
	}
	if !in.TargetPCorrectNone {
		pRange := [2]float64{0.30, 0.85}
		if in.TargetPCorrectRange != nil {
			pRange = *in.TargetPCorrectRange
		}
		purposeOverlay["difficulty_target"] = map[string]any{
			"target_p_correct_range": []any{pRange[0], pRange[1]},
		}
	}
	minPerKp := 3
	if in.MinItemsPerIsolatedKp != nil {
		minPerKp = *in.MinItemsPerIsolatedKp
	}
	return CompileProfile(CompileInput{
		ProfileID:          in.ProfileID,
		ProfileVersion:     in.ProfileVersion,
		Purpose:            PurposeDiagnosis,
		Gradeband:          in.Gradeband,
		KpCodes:            in.KpCodes,
		SubjectOverlay:     in.SubjectOverlay,
		PurposeOverlay:     purposeOverlay,
		GradebandOverlay:   in.GradebandOverlay,
		MinItemsPerKp:      &minPerKp,
		AllowItemCountSoft: boolPtr(true),
	})
}

func intToAny(n int) any { return n }

func boolPtr(b bool) *bool { return &b }
