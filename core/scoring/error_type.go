package scoring

// error_type 注册中心（卡 #185）：收敛各学科包散落的 error_type_id 定义，
// 提供校验与从评分 trace 提取错误推断的服务。
//
// 为什么住在 core/scoring 而非独立包：error_type_id 的唯一消费面是评分桥
// （cmd/school/assembly.go）与 response_event.error_inferences 列，两者都
// 在评分链路内；注册中心随评分器注册表装配，不引入新的编译环。
//
// 种子来源：sim-student/error_type_registry.json（43 种，跨学科规范清单）+
// 学科包/评分器已用值（missing_step / low_confidence_needs_human_review）。
// 注册中心只增不改（波内契约冻结）：新 error_type_id 经学科包认领后登记，
// 绝不删除既有条目——历史 response_event.error_inferences 的审计口径依赖
// 已登记 id 的稳定性。

import (
	"fmt"
	"strings"
)

// ErrUnknownErrorType 表示 error_inferences 中的 error_type_id 未在注册中心
// 登记（防漂移：散落的学科包私造 id 在写入 response_event 前被拒）.
var ErrUnknownErrorType = fmt.Errorf("scoring: error_type_id 未在注册中心登记")

// ErrorTypeRegistry 维护合法 error_type_id 集合（写入 response_event 前的
// 校验面）。并发安全：构造后只读（登记在装配期完成），无写路径.
type ErrorTypeRegistry struct {
	ids map[string]struct{}
}

// NewErrorTypeRegistry 构造空注册中心.
func NewErrorTypeRegistry() *ErrorTypeRegistry {
	return &ErrorTypeRegistry{ids: make(map[string]struct{})}
}

// Register 登记一个合法 error_type_id（空串拒绝）。返回 error 便于装配期
// fail-loud：重复登记视为配置错误而非静默幂等——注册中心只增不改，重复即漂移.
func (r *ErrorTypeRegistry) Register(id string) error {
	if strings.TrimSpace(id) == "" {
		return fmt.Errorf("scoring: error_type_id 不能为空")
	}
	if _, exists := r.ids[id]; exists {
		return fmt.Errorf("scoring: error_type_id %q 重复登记（注册中心只增不改）", id)
	}
	r.ids[id] = struct{}{}
	return nil
}

// Valid 报告 id 是否在注册中心登记.
func (r *ErrorTypeRegistry) Valid(id string) bool {
	_, ok := r.ids[id]
	return ok
}

// Len 返回已登记条目数（测试与可观测用）.
func (r *ErrorTypeRegistry) Len() int { return len(r.ids) }

// extractEvidenceInferences 从 trace 的 evidence 面读取 error_inferences
// 原始数组（内部使用，调用方再做校验）.
func extractEvidenceInferences(trace map[string]any) []any {
	if trace == nil {
		return nil
	}
	ev, ok := trace["evidence"].(map[string]any)
	if !ok {
		return nil
	}
	raw, ok := ev["error_inferences"].([]any)
	if !ok {
		return nil
	}
	return raw
}

// ExtractErrorInferences 从评分 trace 的 evidence 面提取错误推断数组，逐条
// 校验 error_type_id 合法性（注册中心命中才保留）。返回 nil 表示空集（调用方
// 落账时按 nil/空数组同义处理，见 core/events/writer.go）。
//
// 校验纪律：error_type_id 缺失/未登记/形态不符的推断整条丢弃（不伪造归因），
// 合法推断保留 {error_type_id, confidence, rule_version} 三键（契约 §4 的
// 加性扩展面，只增不改）。
func ExtractErrorInferences(trace map[string]any, reg *ErrorTypeRegistry) []map[string]any {
	raw := extractEvidenceInferences(trace)
	if len(raw) == 0 {
		return nil
	}
	if reg == nil {
		return nil
	}
	out := make([]map[string]any, 0, len(raw))
	for _, item := range raw {
		inf, ok := item.(map[string]any)
		if !ok {
			continue
		}
		id, _ := inf["error_type_id"].(string)
		if !reg.Valid(id) {
			continue
		}
		clean := map[string]any{"error_type_id": id}
		if c, ok := inf["confidence"].(float64); ok {
			clean["confidence"] = c
		}
		if rv, ok := inf["rule_version"].(string); ok {
			clean["rule_version"] = rv
		}
		out = append(out, clean)
	}
	if len(out) == 0 {
		return nil
	}
	return out
}

// defaultErrorTypeIDs 是注册中心的规范种子集：sim-student/error_type_registry.json
// 的 43 种 + 评分器已用值（missing_step / low_confidence_needs_human_review）。
// 学科包私造但未入本清单的 id 在 ExtractErrorInferences 时被拒——倒逼认领登记.
var defaultErrorTypeIDs = []string{
	// ── calc.addsub ──
	"err.calc.addsub.mismatch",
	"err.calc.add.off-by-one",
	"err.calc.add.minus-one",
	"math.carry",
	"math.borrow",
	"math.add.carry",
	"math.add.random",
	// ── calc.mul / muldiv ──
	"err.calc.mul.off-by-one-operand",
	"err.calc.mul.digit-swap",
	"err.calc.muldiv.mismatch",
	"err.calc.mul.extra-factor",
	"err.calc.mul.less-factor",
	// ── frac.add / cmp ──
	"err.add.frac.denominator-added",
	"err.add.frac.off-by-one",
	"err.add.frac.sign-slip",
	"err.cmp.frac.mediant",
	"err.cmp.frac.denominator-confuse",
	"err.cmp.frac.numerator-only",
	"math.frac.swap",
	// ── dec.cmp ──
	"err.cmp.dec.digit-confuse",
	"err.cmp.dec.digit-slip",
	"math.decimal.digits_more_is_larger",
	// ── measurement / conversion ──
	"err.conv.unit.mismatch",
	"err.conv.time.mismatch",
	// ── geometry ──
	"err.geo.rect.mismatch",
	"err.pythagorean.sum",
	"err.pythagorean.diff",
	// ── rounding / sign ──
	"err.round.nearest.mismatch",
	"math.sign",
	// ── generic ──
	"off_by_one",
	"value_mismatch",
	"empty_response",
	"invalid_response",
	"missing_step",
	"err.miss",
	"err.missed.point",
	"math.compare.swap",
	// ── chinese ──
	"lang.chr.confusable",
	"lang.chr.wrong_radical",
	"lang.sem.wrong_relation",
	"lang.sent.confusable",
	"lang.pinyin.shape_confusion",
	"lang.pinyin.rhyme_confusion",
	"lang.pinyin.near_sound",
	// ── english ──
	"eng.gram.rule_violation",
	"eng.vocab.misspell",
	// ── scoring-internal ──
	"low_confidence_needs_human_review",
}

// DefaultErrorTypeRegistry 返回预填充规范种子的注册中心（装配面使用）.
func DefaultErrorTypeRegistry() *ErrorTypeRegistry {
	r := NewErrorTypeRegistry()
	for _, id := range defaultErrorTypeIDs {
		// 种子集由本文件硬编码，重复即源码 bug，panic 显式暴露.
		if err := r.Register(id); err != nil {
			panic(fmt.Sprintf("scoring: 种子集缺陷: %v", err))
		}
	}
	return r
}
