package registry

import (
	"fmt"
	"math"
)

// 评分器条目的契约声明面（T-W5-016；specs/contracts/registries/scorer.yaml
// required_fields + params_schema.required 的 Go 投影）。
//
// 契约校验纪律（D4 fail-loud）：评分器条目加载时校验契约面，不合规条目注册即
// 失败——带病条目一旦入库，历史 scoring_trace 的审计口径即被污染，注册期是
// 最后一个零成本拦截点。校验三面（任务卡验收 #2）：
//  1. 身份面：登记键与声明 id 一致、version 非空（条目只增不改的版本锚）；
//  2. 输入面：必备入参键集合（scorer.yaml params_schema.required）或显式
//     「任意作答」声明，二选一——两者皆无/皆有都是残缺契约；
//  3. AI 面：非确定性（AI 参与）评分器必须声明 prompt 版本（D10 台账要素
//     「缺一不可」的注册期投影；结果侧模型身份在 ValidateResult 强制）。
//
// ADR-0005 对齐：人工兜底（human_confirm）已申请废弃为 model_arbiter（L3 模型
// 仲裁），判据正是「人工结论无 model_version/prompt 版本，破坏 D10 可回放统一
// 口径」。本契约面把该判据结构化：非确定性条目必须有模型身份（结果侧）与
// prompt 版本（声明侧）才可注册与出分——human_confirm 形态的条目从此过不了
// 装配门，model_arbiter 形态的条目天然满足，无需为仲裁单开注册通道.

// ParamKind 是入参键的期待形态（scorer.yaml params_schema 的 JSON Schema
// type 子集投影；作答载荷经 JSON 通道的常态形态）.
type ParamKind string

// 入参形态五值（与 params_schema type 词表同源；any 用于契约未声明 type 的
// 必备键——如 exact_match.answer 形态随交互类型，标量/数组/对象皆合法，
// 具体形态纪律由评分器按契约自校，T-W5-016 PyR 评分域补全）.
const (
	KindObject ParamKind = "object" // JSON object → map[string]any
	KindArray  ParamKind = "array"  // JSON array  → []any
	KindString ParamKind = "string" // JSON string → string
	KindNumber ParamKind = "number" // JSON number → float64（Go 字面量 int/int64 兼容）
	KindAny    ParamKind = "any"    // 任意 JSON 形态（含 null；必备键存在性仍由 Runner 强制）
)

// ScorerSpec 是评分器条目自述的契约面（Contracted 准入与 Runner 入参校验的
// 唯一事实源；声明一次，注册期与执行期双消费——两处口径不可能漂移）.
type ScorerSpec struct {
	// Entry 条目身份：ID 必须与登记键一致，Version 非空且落 scoring_trace
	// （D6/D10：历史报告永远引用当时版本）.
	Entry Entry
	// InputSchema 必备入参键 → 期待形态（params_schema.required + type）。
	// 与 AcceptsAnyInput 恰一声明：键表残缺与「任意作答」同写/同缺即注册失败.
	InputSchema map[string]ParamKind
	// AcceptsAnyInput 声明「任何交互类型的作答」（scorer.yaml input_contract
	// 的 model_arbiter 类条目；无必备入参键不等于无契约——须显式声明）.
	AcceptsAnyInput bool
	// Deterministic 确定性评分器（无 AI 参与）。true 时结果不得携带模型身份、
	// 本字段声明侧 PromptVersion 必须为空；false 时两者相反（D10 双向强制）.
	Deterministic bool
	// PromptVersion AI 评分的 prompt 版本（.baml 函数版本锚，落 scoring_trace；
	// 十年后回答「当时用什么提示词给的分」）.
	PromptVersion string
}

// validate 校验契约声明面本身（注册期消费；key 为登记键）.
func (s ScorerSpec) validate(key string) error {
	if key == "" {
		return fmt.Errorf("%w: 登记键为空", ErrInvalidContract)
	}
	if s.Entry.ID != key {
		return fmt.Errorf("%w: 声明 id %q 与登记键 %q 不一致（静默错位会让审计口径漂移）", ErrInvalidContract, s.Entry.ID, key)
	}
	if s.Entry.Version == "" {
		return fmt.Errorf("%w: %s 条目 version 为空（版本锚是可回放的最低要求，D6）", ErrInvalidContract, key)
	}
	if (len(s.InputSchema) == 0) != s.AcceptsAnyInput {
		return fmt.Errorf("%w: %s 条目输入面必须恰一声明（必备键表或 AcceptsAnyInput）", ErrInvalidContract, key)
	}
	for k := range s.InputSchema {
		if k == "" {
			return fmt.Errorf("%w: %s 条目入参键表含空键", ErrInvalidContract, key)
		}
	}
	if !s.Deterministic && s.PromptVersion == "" {
		return fmt.Errorf("%w: %s 条目为 AI 评分（非确定性），prompt 版本必填（D10）", ErrInvalidContract, key)
	}
	if s.Deterministic && s.PromptVersion != "" {
		return fmt.Errorf("%w: %s 条目为确定性评分器，不得声明 prompt 版本", ErrInvalidContract, key)
	}
	return nil
}

// Contracted 是评分器自述契约面的能力接口（ScorerTable.Register 的准入要求；
// 与 Entry() 同构的自描述模式——条目自带身份与契约，注册表只做守门）.
type Contracted interface {
	Scorer
	ScorerContract() ScorerSpec
}

// ScorerTable 是带契约校验的评分器注册表（Registry[Scorer] 的 D4 装配门面）。
// 条目只增不改继承自底层 Registry（重复注册 ErrDuplicate）；本类型在其上追加
// 注册期契约校验与声明面留存——core/scoring 执行期按同一声明面校验入参与结果，
// 「加载时契约」与「运行时契约」同源.
type ScorerTable struct {
	reg   *Registry[Scorer]
	specs map[string]ScorerSpec
}

// NewScorerTable 构造空评分器注册表.
func NewScorerTable() *ScorerTable {
	return &ScorerTable{reg: New[Scorer](), specs: make(map[string]ScorerSpec)}
}

// Register 登记评分器条目：无契约声明面、声明不合规、id 重复（ErrDuplicate）
// 一律失败——不合规条目注册即失败（fail-loud），无任何静默收编路径.
func (t *ScorerTable) Register(key string, s Scorer) error {
	c, ok := s.(Contracted)
	if !ok {
		return fmt.Errorf("%w: %s 未实现 ScorerContract（无契约面的评分器不予装配）", ErrInvalidContract, key)
	}
	spec := c.ScorerContract()
	if err := spec.validate(key); err != nil {
		return err
	}
	if err := t.reg.Register(key, s); err != nil {
		return err
	}
	t.specs[key] = spec
	return nil
}

// Get 按 id 取条目与其契约声明面（未注册第二返回值为 false）.
func (t *ScorerTable) Get(id string) (Scorer, ScorerSpec, bool) {
	s, ok := t.reg.Get(id)
	if !ok {
		return nil, ScorerSpec{}, false
	}
	return s, t.specs[id], true
}

// Len 返回条目数（测试与可观测用）.
func (t *ScorerTable) Len() int { return t.reg.Len() }

// ValidateResult 校验评分结果的 verdict 形态（执行期，装配 trace 前拦截）：
//   - confidence ∈ [0,1]（NaN 视为越界——JSON 序列化即失败的值不得出分）；
//   - score 非 NaN（残缺数值下游聚合即中毒）；
//   - 模型身份双向强制（D10）：AI 评分（非确定性）必须携带 model/model_version
//     ——十年后可定位「当时是哪个模型给的分」；确定性评分器不得携带（registry
//     ScoreResult 契约注释的字面语义，防确定性条目伪挂模型身份混淆台账口径）.
func ValidateResult(spec ScorerSpec, res ScoreResult) error {
	if !(res.Confidence >= 0 && res.Confidence <= 1) {
		return fmt.Errorf("%w: confidence=%v 越界 [0,1]", ErrInvalidResult, res.Confidence)
	}
	if math.IsNaN(res.Score) {
		return fmt.Errorf("%w: score=NaN（残缺判定不落账）", ErrInvalidResult)
	}
	if spec.Deterministic {
		if res.Model != "" || res.ModelVersion != "" {
			return fmt.Errorf("%w: 确定性评分器 %s 不得携带模型身份（model=%q model_version=%q）",
				ErrInvalidResult, spec.Entry.ID, res.Model, res.ModelVersion)
		}
		return nil
	}
	if res.Model == "" || res.ModelVersion == "" {
		return fmt.Errorf("%w: AI 评分器 %s 结果缺 model/model_version（D10：AI 参与的评分必须可定位模型版本）",
			ErrInvalidResult, spec.Entry.ID)
	}
	return nil
}
