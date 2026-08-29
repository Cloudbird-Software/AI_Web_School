// gradeband_constraints.go 承载低段组卷 overlay 与会话约束（Python 冻结基准
// src/core/assembly/gradeband_constraints.py 的 Go 移植；架构 v2 §5.3 / §4.8）。
//
// 核心域学段约束政策：
//   - L 段：题量 ≤10、会话时长 ≤15 分钟、形态=闯关（架构 §5.3 / §4.8）
//   - M 段：题量 ≤20、时长 ≤60 分钟、形态=常规
//   - H 段：题量 ≤30、时长 ≤60 分钟、形态=常规
//
// ApplyGradebandOverlay 把学段约束注入 paper_spec 并检测不可行冲突
// （如请求 20 题低段卷 → 返回明确冲突原因）。
//
// 宪法 A5：学段约束政策是核心域常量（与 core/session 的 GradebandTimeLimitSec
// 同源：L=15min、M/H=60min）；学段包 config.yaml 可通过 overlay 参数注入覆盖
// （核心不感知包文件位置，仅按约定键 max_items / session_duration_max_min /
// session_form_game 消费）。
package assembly

import (
	"fmt"
	"sort"
	"strings"
)

// GradebandConstraints 核心域学段约束政策（Python GRADEBAND_CONSTRAINTS；
// 键 = 学段 L/M/H）. 返回副本防调用方改写共享底层数据.
func GradebandConstraints() map[string]map[string]any {
	return map[string]map[string]any{
		GradebandL: {"max_items": 10, "time_limit_min": 15, "session_form": "game"},
		GradebandM: {"max_items": 20, "time_limit_min": 60, "session_form": "standard"},
		GradebandH: {"max_items": 30, "time_limit_min": 60, "session_form": "standard"},
	}
}

// validGradebands 学段值域（Python VALID_GRADEBANDS）.
var validGradebands = []string{GradebandL, GradebandM, GradebandH}

// GradeBandOverlayResult 是 ApplyGradebandOverlay 返回结果（Python
// GradeBandOverlayResult dataclass）.
type GradeBandOverlayResult struct {
	// PaperSpec 注入学段约束后的 paper_spec（含 time_limit_min /
	// session_form / max_items / gradeband 字段）.
	PaperSpec map[string]any
	// Feasible 学段约束是否可行（题量/时长未超学段上限）.
	Feasible bool
	// Conflict 不可行时的明确冲突原因（可行时为空串）.
	Conflict string
	// OverlayApplied 实际生效的学段约束 dict（审计用）.
	OverlayApplied map[string]any
}

// GradeBandConflictError 学段约束不可行（Python GradeBandConflictError；
// 调用方显式要求 RaiseOnConflict=true 时返回）.
type GradeBandConflictError struct{ Detail string }

func (e *GradeBandConflictError) Error() string { return e.Detail }

// resolveGradebandConstraints 合并核心默认约束与 pack 注入 overlay（pack 覆盖
// 核心默认；Python _resolve_constraints）。
//
// pack config.yaml 的字段名（max_items / session_duration_max_min /
// session_form_game）映射到核心约束键（max_items / time_limit_min /
// session_form）——核心不 import 学段包，只按约定键消费 overlay dict。
func resolveGradebandConstraints(gradeBand string, overlay map[string]any) (map[string]any, error) {
	base, ok := GradebandConstraints()[gradeBand]
	if !ok {
		return nil, fmt.Errorf("assembly: grade_band 必须 ∈ %v，实际 %q", validGradebands, gradeBand)
	}
	merged := map[string]any{}
	for k, v := range base {
		merged[k] = v
	}
	if overlay != nil {
		if v, ok := overlay["max_items"]; ok {
			n, err := asInt(v)
			if err != nil {
				return nil, fmt.Errorf("assembly: overlay.max_items 非法: %v", err)
			}
			merged["max_items"] = n
		}
		if v, ok := overlay["session_duration_max_min"]; ok {
			n, err := asInt(v)
			if err != nil {
				return nil, fmt.Errorf("assembly: overlay.session_duration_max_min 非法: %v", err)
			}
			merged["time_limit_min"] = n
		}
		if v, ok := overlay["session_form_game"]; ok {
			b, ok := v.(bool)
			if !ok {
				return nil, fmt.Errorf("assembly: overlay.session_form_game 非法（须为布尔）")
			}
			if b {
				merged["session_form"] = "game"
			} else {
				merged["session_form"] = "standard"
			}
		}
	}
	return merged, nil
}

// extractItemCount 从 paper_spec 取题量（Python _extract_item_count；兼容多种
// 声明形态）：item_count（int）/ items（list，取长度）/ item_count_range
// （取上界）。不存在返回 (nil, false)。
func extractItemCount(paperSpec map[string]any) (*int, bool) {
	if v, ok := paperSpec["item_count"]; ok {
		if n, err := asInt(v); err == nil {
			return &n, true
		}
	}
	if items, ok := paperSpec["items"].([]any); ok {
		n := len(items)
		return &n, true
	}
	if rng, ok := paperSpec["item_count_range"].([]any); ok && len(rng) > 0 {
		// 上界作为题量上限校验依据（Python rng[-1]）
		if n, err := asInt(rng[len(rng)-1]); err == nil {
			return &n, true
		}
	}
	return nil, false
}

// BuildGradebandOverlay 生成 CompileProfile 用的 gradeband_overlay dict
// （Python build_gradeband_overlay）。
//
// 返回的 dict 符合 CompileProfile 的 GradebandOverlay 参数约定
// （item_count_range / time_limit_max_minutes / session_form），作为四维编译的
// 学段维度注入。
func BuildGradebandOverlay(gradeBand string, overlay map[string]any) (map[string]any, error) {
	c, err := resolveGradebandConstraints(gradeBand, overlay)
	if err != nil {
		return nil, err
	}
	maxItems, _ := asInt(c["max_items"])
	timeLimit, _ := asInt(c["time_limit_min"])
	return map[string]any{
		"overlay_id":      fmt.Sprintf("gradeband-%s", strings.ToLower(gradeBand)),
		"overlay_version": "1.0.0",
		"item_count_range": []any{
			intToAny(1),
			intToAny(maxItems),
		},
		"time_limit_max_minutes": timeLimit,
		"session_form":           c["session_form"],
	}, nil
}

// ApplyGradebandOverlay 注入学段约束到 paper_spec，并检测不可行冲突（Python
// apply_gradeband_overlay）。
//
// paperSpec 可含 item_count / items / item_count_range / time_limit_min /
// session_form（map 形态，与冻结实现的 dict 语义同形；本函数不改写入参）。
// RaiseOnConflict=true 时不可行返回 *GradeBandConflictError；false（默认）时
// 通过 result.Conflict 返回。
//
// Notes:
//   - L 段注入：max_items=10、time_limit_min=15、session_form="game"。
//   - 不可行示例：请求 20 题低段卷 → Conflict="L 段题量上限 10，请求 20 超出"。
func ApplyGradebandOverlay(paperSpec map[string]any, gradeBand string, overlay map[string]any, raiseOnConflict bool) (*GradeBandOverlayResult, error) {
	if !containsStr(validGradebands, gradeBand) {
		// 与 Python 一致：未知学段直接 ValueError（核心域零特判，学段取值受控）
		return nil, fmt.Errorf("assembly: grade_band 必须 ∈ %v，实际 %q", validGradebands, gradeBand)
	}

	constraints, err := resolveGradebandConstraints(gradeBand, overlay)
	if err != nil {
		return nil, err
	}
	overlaid := map[string]any{}
	for k, v := range paperSpec {
		overlaid[k] = v
	}
	conflicts := []string{}

	// 题量校验：请求题量超学段上限 → 不可行
	if itemCount, ok := extractItemCount(paperSpec); ok {
		maxItems, _ := asInt(constraints["max_items"])
		if *itemCount > maxItems {
			conflicts = append(conflicts, fmt.Sprintf("%s 段题量上限 %d，请求 %d 超出", gradeBand, maxItems, *itemCount))
		}
	}

	// 时长校验：paper_spec 显式声明时长且超学段上限 → 不可行
	if specTime, ok := paperSpec["time_limit_min"]; ok && specTime != nil {
		t, err := asInt(specTime)
		if err != nil {
			return nil, fmt.Errorf("assembly: paper_spec.time_limit_min 非法: %v", err)
		}
		limit, _ := asInt(constraints["time_limit_min"])
		if t > limit {
			conflicts = append(conflicts, fmt.Sprintf("%s 段时长上限 %d 分钟，请求 %d 超出", gradeBand, limit, t))
		}
	}

	// 注入学段约束（无论是否冲突都注入，便于调用方看到目标约束）
	overlaid["gradeband"] = gradeBand
	overlaid["max_items"] = constraints["max_items"]
	overlaid["time_limit_min"] = constraints["time_limit_min"]
	overlaid["session_form"] = constraints["session_form"]

	conflict := strings.Join(conflicts, "; ")
	if conflict != "" && raiseOnConflict {
		return nil, &GradeBandConflictError{Detail: conflict}
	}

	return &GradeBandOverlayResult{
		PaperSpec:      overlaid,
		Feasible:       conflict == "",
		Conflict:       conflict,
		OverlayApplied: constraints,
	}, nil
}

// ValidGradebands 返回学段值域（排序副本；Python VALID_GRADEBANDS 的 frozenset
// 语义——测试面用排序切片表达）.
func ValidGradebands() []string {
	out := append([]string(nil), validGradebands...)
	sort.Strings(out)
	return out
}
