// Package registry 是双类型注册表的 Go 落点（宪法 D4：作答交互与评分器
// 只能来自平台注册表，学科包只能复用参数化；A5/X6：core 零学科特判）。
//
// W5-R 骨架只定义接口与版本化条目结构；加载器与 YAML 契约
// （specs/contracts/registries/*.yaml）的对齐在重锚定卡 T-W5-016 落地。
package registry

import "context"

// Entry 是注册表条目的公共元数据：一切条目都是版本化资产（可审计，§八）。
type Entry struct {
	ID      string // 注册表内唯一 id（契约文件键）
	Version string // 语义化版本；条目演进只增不改
}

// Interaction 是作答交互类型的注册表接口。
// 实现（渲染器 + 作答数据 schema + 归一化器）由学科包提供，经注册表装配。
type Interaction interface {
	Entry() Entry
	// Normalize 把作答原文归一化为评分可消费的标准形态。
	Normalize(raw string) (string, error)
}

// ScoreResult 是评分器输出的最小契约（model/model_version 供 D10 可回放；
// 双向强制见 ValidateResult——AI 评分必填，确定性评分器为空）。
type ScoreResult struct {
	Correct      bool
	Score        float64
	Confidence   float64
	Model        string // 模型标识（AI 评分必填，写入 scoring_trace）；确定性评分器为空
	ModelVersion string // 模型版本（AI 评分必填，写入 scoring_trace）；确定性评分器为空
	// EvidenceJSON 是评分证据的 canonical JSON 序列（scorer.yaml
	// unified_contract.output_schema 必备输出 evidence + error_inferences 的
	// 承载面；空串 = 本评分器无证据输出）。为什么是字符串而非 map：replay
	// 断言对 Result 做 == 结构比较，map 字段使结构不可比较——证据以确定性
	// JSON（encoding/json 对 map 键排序）随结果走，Runner 落 trace 时解码为
	// 加性键 evidence（契约 §3：scoring_trace 可扩展只增不改）.
	EvidenceJSON string
}

// Scorer 是评分器注册表接口。human_confirm 兜底的处置见 ADR-0005（申请中）。
type Scorer interface {
	Entry() Entry
	// Score 在给定事务外（事务边界由 core/session 管，D11）执行评分。
	Score(ctx context.Context, answer string, params map[string]any) (ScoreResult, error)
}

// Registry 是泛型注册表：条目只增不改（演进 = 新版本条目）。
type Registry[T any] struct {
	entries map[string]T
}

// New 构造空注册表。
func New[T any]() *Registry[T] {
	return &Registry[T]{entries: make(map[string]T)}
}

// Register 登记条目；id 重复即注册失败（禁止静默覆盖历史条目）。
func (r *Registry[T]) Register(id string, v T) error {
	if _, exists := r.entries[id]; exists {
		return ErrDuplicate
	}
	r.entries[id] = v
	return nil
}

// Get 按 id 取条目。
func (r *Registry[T]) Get(id string) (T, bool) {
	v, ok := r.entries[id]
	return v, ok
}

// Len 返回条目数（测试与可观测用）。
func (r *Registry[T]) Len() int { return len(r.entries) }
