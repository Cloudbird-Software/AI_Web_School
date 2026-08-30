// linter.go 承载母题 spec 的静态校验（Python 冻结基准
// src/core/instantiation/dsl/linter.py 的 Go 移植；T-W2-001）。
//
// 四类必检（验收 §2，不短路、逐项收集）：
//  1. 必填块缺失（六大块）
//  2. slot 类型不在 ALLOWED_SLOT_TYPES
//  3. variation_axis 引用不存在的 slot
//  4. difficulty_relevant 不是 boolean
//
// 并叠加全量结构校验（extra='forbid'、字段类型、必填项、枚举——对齐
// Pydantic model_validate），与阶段 1 按 (code, path) 去重后合并，
// Linter 单次调用即收集全部问题。
package dsl

import "fmt"

// requiredBlocks 六大必填块（架构 v2 §4.1）。
var requiredBlocks = []string{
	"objective", "slots", "variation_axes",
	"presentation", "answer_program", "distractor_rules",
}

// Lint 对母题 spec 做静态校验，返回结构化结果（valid 当且仅当无任何错误）。
// spec 为六大块 map（通常来自 item_template_version.spec 的 YAML/JSON 解码
// 形态）；非 map 输入由阶段 2 结构解析兜底报错。
func Lint(spec any) *LintResult {
	var errs []LintError

	// ── 阶段 1：四类必检（手动收集，不短路） ──
	if m, ok := spec.(map[string]any); ok {
		errs = append(errs, checkRequiredBlocks(m)...)
		errs = append(errs, checkSlotTypes(m)...)
		errs = append(errs, checkVariationAxisSlots(m)...)
		errs = append(errs, checkDifficultyRelevant(m)...)
	}

	// ── 阶段 2：全量结构校验（extra/enum/类型等），(code, path) 去重合并 ──
	_, parseErrs := parseSpec(spec)
	seen := map[string]bool{}
	for _, e := range errs {
		seen[e.key()] = true
	}
	for _, pe := range parseErrs {
		if !seen[pe.key()] {
			errs = append(errs, pe)
		}
	}

	return &LintResult{Valid: len(errs) == 0, Errors: errs}
}

// ParseSpec 把 spec map 解析为强类型 ItemTemplateSpec；存在任何结构错误时
// 返回聚合错误（供引擎 fail-closed 拒绝）。等价 Pydantic model_validate。
func ParseSpec(spec any) (*ItemTemplateSpec, error) {
	parsed, errs := parseSpec(spec)
	if len(errs) > 0 {
		agg := errs[0]
		for _, e := range errs[1:] {
			agg.Message += "; " + e.Code + "@" + e.Path + ": " + e.Message
		}
		return nil, &SpecError{First: agg, Count: len(errs)}
	}
	return parsed, nil
}

// SpecError spec 结构不合规（fail-closed 拒绝实例化）。
type SpecError struct {
	First LintError
	Count int
}

func (e *SpecError) Error() string {
	return "dsl: spec 结构不合规（" + itoa(e.Count) + " 处）: " +
		e.First.Code + "@" + e.First.Path + ": " + e.First.Message
}

// ────────────────────────────────────────────────────────────────────
// 阶段 1 四类必检
// ────────────────────────────────────────────────────────────────────

// checkRequiredBlocks 检查六大块是否齐全（在顶层明确指出缺失块名，便于教研定位）。
func checkRequiredBlocks(spec map[string]any) []LintError {
	var missing []string
	for _, b := range requiredBlocks {
		if _, ok := spec[b]; !ok {
			missing = append(missing, b)
		}
	}
	if len(missing) == 0 {
		return nil
	}
	msg := "缺少必填块："
	for i, m := range missing {
		if i > 0 {
			msg += ", "
		}
		msg += m
	}
	return []LintError{{Code: "missing_block", Path: ".", Message: msg}}
}

// checkSlotTypes 检查 slots 中每个槽的 type 是否在允许列表内。
func checkSlotTypes(spec map[string]any) []LintError {
	slots, ok := spec["slots"].(map[string]any)
	if !ok {
		return nil
	}
	var errs []LintError
	for name, def := range slots {
		dm, ok := def.(map[string]any)
		if !ok {
			continue
		}
		stype, ok := dm["type"]
		if !ok || stype == nil {
			continue // 缺 type 由阶段 2 兜底
		}
		s, ok := stype.(string)
		if !ok {
			continue
		}
		if !AllowedSlotTypes[s] {
			allowed := ""
			for i, a := range AllowedSlotTypesSorted() {
				if i > 0 {
					allowed += ", "
				}
				allowed += a
			}
			errs = append(errs, LintError{
				Code:    "invalid_slot_type",
				Path:    "slots." + name + ".type",
				Message: "槽类型 '" + s + "' 不在允许列表 (" + allowed + ")",
			})
		}
	}
	return errs
}

// checkVariationAxisSlots 检查变式轴引用的槽名是否都存在于 slots 块。
func checkVariationAxisSlots(spec map[string]any) []LintError {
	slots, ok := spec["slots"].(map[string]any)
	if !ok {
		return nil
	}
	va, ok := spec["variation_axes"].(map[string]any)
	if !ok {
		return nil
	}
	axes, ok := va["axes"].([]any)
	if !ok {
		return nil
	}
	var errs []LintError
	for i, ax := range axes {
		am, ok := ax.(map[string]any)
		if !ok {
			continue
		}
		axisID, _ := am["axis_id"].(string)
		if axisID == "" {
			axisID = "[" + itoa(i) + "]"
		}
		refSlots, ok := am["slots"].([]any)
		if !ok {
			continue
		}
		for _, ref := range refSlots {
			rs, ok := ref.(string)
			if !ok {
				continue
			}
			if _, exists := slots[rs]; !exists {
				errs = append(errs, LintError{
					Code:    "dangling_variation_slot",
					Path:    "variation_axes.axes[" + itoa(i) + "].slots",
					Message: "变式轴 '" + axisID + "' 引用了不存在的槽 '" + rs + "'",
				})
			}
		}
	}
	return errs
}

// checkDifficultyRelevant 检查每个槽的 difficulty_relevant 是否为布尔
// （严格布尔：明确指向哪个槽并给出可读 message，且不阻断其他检查）。
func checkDifficultyRelevant(spec map[string]any) []LintError {
	slots, ok := spec["slots"].(map[string]any)
	if !ok {
		return nil
	}
	var errs []LintError
	for name, def := range slots {
		dm, ok := def.(map[string]any)
		if !ok {
			continue
		}
		val, present := dm["difficulty_relevant"]
		if !present || val == nil {
			continue // 缺字段由阶段 2 兜底
		}
		if _, isBool := val.(bool); !isBool {
			errs = append(errs, LintError{
				Code: "invalid_difficulty_relevant_type",
				Path: "slots." + name + ".difficulty_relevant",
				Message: "difficulty_relevant 必须为 boolean，实际为 " +
					typeName(val),
			})
		}
	}
	return errs
}

// typeName 返回值的 Go 类型可读名（错误信息用）。
func typeName(v any) string {
	switch v.(type) {
	case string:
		return "str"
	case float64, int, int64:
		return "number"
	case bool:
		return "bool"
	case []any:
		return "list"
	case map[string]any:
		return "dict"
	case nil:
		return "None"
	default:
		return fmt.Sprintf("%T", v)
	}
}
