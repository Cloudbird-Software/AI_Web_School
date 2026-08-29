// listening_overlay.go 承载听力组卷 overlay（Python 冻结基准
// src/core/assembly/listening_overlay.py 的 Go 移植；架构 v2 §4.4 / §4.6 / S5）。
//
// 英语试卷听力题约束 overlay：
//   - 占比硬约束：听力题占卷面总题量的 30–40%（ADR §4.6，S5 overlay 清单）；
//   - 位置硬约束：听力题置卷首（模拟考试听力先行语义）；
//   - testlet 标记：听力子题共享同一 testlet_id + 音频上下文（一材多题形态）。
//
// 为什么不修改 ConstraintSet：ConstraintSet 是波内冻结契约，听力约束是学科级
// overlay（英语听力线专属），不应侵入核心域约束模型。overlay 以独立模型返回，
// 由组卷编排层在装配后应用：
//  1. 求解器按 base Profile 选题（含听力候选）；
//  2. overlay 校验听力占比 + 标记 testlet + 重排卷首；
//  3. 不可行（听力素材不足）→ 返回冲突原因，不静默放松（§4.4 铁律）。
//
// 宪法 A5/X6：不 import 学科包/学段包；overlay 是核心域通用约束，不感知
// 「英语」语义（任何学科有听力需求均可复用）。
package assembly

import (
	"fmt"
	"math"
	"sort"
	"strings"
)

// 听力占比硬约束范围（ADR §4.6：30–40%）与位置（Python 常量）.
const (
	ListeningRatioMin = 0.30
	ListeningRatioMax = 0.40
	ListeningPosition = "first"
)

// ListeningOverlaySpec 听力 overlay 配置（Python ListeningOverlaySpec；可自定义
// 参数，默认 30–40% / 置卷首）。
//
//   - RatioRange：听力题占比范围 [min, max]（默认 0.30–0.40）；
//   - Position：听力题位置（"first"=卷首，当前唯一支持值）；
//   - AudioContextRef：共享音频上下文引用（如 audio_id 或 paper 级音频 bundle id）；
//   - MaxDurationMinutes：听力时长上限（分钟，学段配置；nil=不限制）。
type ListeningOverlaySpec struct {
	RatioRange         [2]float64
	Position           string
	AudioContextRef    string
	MaxDurationMinutes *int
}

// NewListeningOverlaySpec 构造并校验 overlay 配置（Python pydantic 构造期校验：
// audio_context_ref min_length=1；0 < min < max < 1；max_duration ge=1）.
func NewListeningOverlaySpec(audioContextRef string, ratioRange *[2]float64, maxDurationMinutes *int) (*ListeningOverlaySpec, error) {
	spec := &ListeningOverlaySpec{
		RatioRange:         [2]float64{ListeningRatioMin, ListeningRatioMax},
		Position:           ListeningPosition,
		AudioContextRef:    audioContextRef,
		MaxDurationMinutes: maxDurationMinutes,
	}
	if ratioRange != nil {
		spec.RatioRange = *ratioRange
	}
	if len(audioContextRef) < 1 {
		return nil, fmt.Errorf("assembly: ListeningOverlaySpec.audio_context_ref 不能为空（min_length=1）")
	}
	lo, hi := spec.RatioRange[0], spec.RatioRange[1]
	if !(0.0 < lo && lo < hi && hi < 1.0) {
		return nil, fmt.Errorf("assembly: ratio_range 非法：(%v, %v)，需满足 0 < min < max < 1", lo, hi)
	}
	if maxDurationMinutes != nil && *maxDurationMinutes < 1 {
		return nil, fmt.Errorf("assembly: max_duration_minutes = %d 越域（ge=1）", *maxDurationMinutes)
	}
	return spec, nil
}

// ListeningConflict 听力 overlay 冲突原因（不可行时返回，禁止静默放松）.
type ListeningConflict struct {
	ConstraintID string
	Detail       string
	Required     *int
	Available    *int
}

// ListeningOverlay 听力组卷 overlay（ApplyListeningOverlay 产物）：
//   - TestletID：听力题组 testlet 标识（子题共享同一音频上下文）；
//   - ListeningItemCountRange：听力题量范围 [min, max]（由总题量×占比计算）；
//   - Spec：原始 overlay 配置（ratio/position/audio_context_ref 等）。
type ListeningOverlay struct {
	TestletID               string
	ListeningItemCountRange [2]int
	Spec                    ListeningOverlaySpec
}

// ListeningOverlayResult 听力 overlay 应用结果：
//   - Feasible=true：profile 可追加听力约束，Overlay 含 testlet/占比范围；
//   - Feasible=false：Conflicts 非空，不可行原因结构化（不静默放松）。
type ListeningOverlayResult struct {
	Profile   *AssemblyProfile
	Overlay   *ListeningOverlay
	Conflicts []ListeningConflict
	Feasible  bool
}

// computeTestletID 生成确定性 testlet_id（基于音频上下文哈希；Python
// _compute_testlet_id）。为什么用哈希而非随机：确定性——同一音频上下文得同一
// testlet_id，便于审计回溯与重放（R-Z-01 确定性要求）。
func computeTestletID(audioContextRef string) string {
	digest := sha256Hex(fmt.Sprintf("listening:%s", audioContextRef))
	return fmt.Sprintf("testlet:listening:%s", digest[:16])
}

// computeListeningCountRange 根据总题量与占比范围计算听力题量 [min, max]
// （Python _compute_listening_count_range）。向上取整 min（保证听力占比下限），
// 向下取整 max（不超占比上限）。为什么 ceil min / floor max：保证 [min, max]
// 内的任何值都落在 [ratio_min, ratio_max] 区间内（保守不越界）。
func computeListeningCountRange(totalItems int, ratioRange [2]float64) (int, int) {
	lo := int(math.Ceil(float64(totalItems) * ratioRange[0]))
	hi := int(math.Floor(float64(totalItems) * ratioRange[1]))
	// 边界保护：至少 1 题（total_items > 0 时）
	if lo < 1 {
		lo = 1
	}
	if hi < lo {
		hi = lo
	}
	return lo, hi
}

// ApplyListeningOverlay 在约束集中注入听力占比与位置硬约束（Python
// apply_listening_overlay；验收 #1/#2/#3）。
//
// 流程：
//  1. 从 profile.Constraints.ItemCount 取总题量；
//  2. 按 ratio_range 计算听力题量 [min, max]；
//  3. 校验可行：availableListeningItems >= 听力题量 min（不可行 → conflicts）；
//  4. 生成 testlet_id（基于 audio_context_ref 哈希）；
//  5. 返回 ListeningOverlayResult（overlay + feasible=true）。
//
// 为什么不直接修改 ConstraintSet：听力约束是 overlay 层（不侵入核心约束模型），
// 由组卷编排层在装配后应用（标记 testlet + 重排卷首），保持求解器学科无关。
//
// spec 为 nil 时返回错误（Python ValueError「spec 不能为 None」——需提供
// audio_context_ref）。
func ApplyListeningOverlay(profile *AssemblyProfile, availableListeningItems int, spec *ListeningOverlaySpec) (*ListeningOverlayResult, error) {
	if spec == nil {
		return nil, fmt.Errorf("assembly: spec 不能为 None（需提供 audio_context_ref）")
	}

	totalItems := profile.Constraints.ItemCount.Max
	ratioMin, ratioMax := spec.RatioRange[0], spec.RatioRange[1]
	listenMin, listenMax := computeListeningCountRange(totalItems, spec.RatioRange)

	conflicts := []ListeningConflict{}

	// ── 可行性校验：听力素材是否充足 ──
	if availableListeningItems < listenMin {
		required := listenMin
		available := availableListeningItems
		conflicts = append(conflicts, ListeningConflict{
			ConstraintID: "listening_ratio_min",
			Detail: fmt.Sprintf("听力题占比下限 %.0f%% × 总题量 %d = 至少 %d 道听力题，但可用听力候选仅 %d 道",
				ratioMin*100, totalItems, listenMin, availableListeningItems),
			Required:  &required,
			Available: &available,
		})
	}

	// ── 可行性校验：听力题量不超过总题量 ──
	if listenMax > totalItems {
		required := totalItems
		available := listenMax
		conflicts = append(conflicts, ListeningConflict{
			ConstraintID: "listening_ratio_max_exceeds_total",
			Detail: fmt.Sprintf("听力题占比上限 %.0f%% × 总题量 %d = %d 道，超过总题量 %d",
				ratioMax*100, totalItems, listenMax, totalItems),
			Required:  &required,
			Available: &available,
		})
	}

	if len(conflicts) > 0 {
		return &ListeningOverlayResult{
			Profile:   profile,
			Overlay:   nil,
			Conflicts: conflicts,
			Feasible:  false,
		}, nil
	}

	// ── 生成 overlay ──
	overlay := &ListeningOverlay{
		TestletID:               computeTestletID(spec.AudioContextRef),
		ListeningItemCountRange: [2]int{listenMin, listenMax},
		Spec:                    *spec,
	}
	return &ListeningOverlayResult{
		Profile:   profile,
		Overlay:   overlay,
		Conflicts: []ListeningConflict{},
		Feasible:  true,
	}, nil
}

// MarkListeningTestlet 在组卷结果中标记听力题 testlet_id + 确保置卷首（Python
// mark_listening_testlet；验收 #2）。
//
// 流程：
//  1. 对 result.Items 中的听力题（item_version_id ∈ listeningSet）设置
//     GroupID = overlay.TestletID（标记 testlet）；
//  2. 重排序：听力题置卷首，非听力题保持原序；
//  3. 校验听力占比在 [min, max] 范围内（不满足 → 返回错误，不静默放松）；
//  4. 重新计算 selection_digest（排序变化后）。
//
// 为什么在后处理而非求解期：求解器是通用预算装填，不感知「听力」语义；
// overlay 在求解后应用，保持求解器学科无关（A5）。
func MarkListeningTestlet(result *AssemblyResult, overlay *ListeningOverlay, listeningItemVersionIDs IDSet) (*AssemblyResult, error) {
	listenMin, listenMax := overlay.ListeningItemCountRange[0], overlay.ListeningItemCountRange[1]

	// 标记 testlet
	listeningItems := []CandidateItem{}
	nonListeningItems := []CandidateItem{}
	for _, item := range result.Items {
		if listeningItemVersionIDs.Has(item.ItemVersionID) {
			marked := item
			gid := overlay.TestletID
			marked.GroupID = &gid
			listeningItems = append(listeningItems, marked)
		} else {
			nonListeningItems = append(nonListeningItems, item)
		}
	}

	// 校验占比
	listenCount := len(listeningItems)
	if listenCount < listenMin || listenCount > listenMax {
		return nil, fmt.Errorf(
			"assembly: 听力题数量 %d 不在 overlay 范围 [%d, %d]（禁止静默放松）",
			listenCount, listenMin, listenMax)
	}

	// 重排：听力置卷首，非听力保持原序
	reordered := append(append([]CandidateItem(nil), listeningItems...), nonListeningItems...)

	// 重新计算 digest（排序变化）
	ids := make([]string, 0, len(reordered))
	for _, m := range reordered {
		ids = append(ids, m.ItemVersionID)
	}
	newDigest := sha256Hex(strings.Join(ids, "|"))

	out := *result
	out.Items = reordered
	out.SelectionDigest = newDigest
	return &out, nil
}

// sortedConflictIDs 供测试与日志输出确定性（约束 id 排序视图）.
func sortedConflictIDs(conflicts []ListeningConflict) []string {
	out := make([]string, 0, len(conflicts))
	for _, c := range conflicts {
		out = append(out, c.ConstraintID)
	}
	sort.Strings(out)
	return out
}
