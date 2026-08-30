// d_pipeline.go 承载 D 线端到端流水线骨架（Python 冻结基准
// src/core/production/d_line_pipeline.py 的 Go 移植，T-W4-021 语义同构）。
//
// 落地架构 v2 §4.1 D 线「命题蓝图库」与 §4.5 量规评分的端到端串联：
//
//	选命题蓝图 → 按 A 线模板实例化开放式题目 → 校验（结构/许可/量规完整性）
//	→ 签发入库 → 量规评分器就绪
//
// 多阶段骨架（阶段间显式传递，fail-loud）：
//   - generate 生成：蓝图 + 学段 spec + 参数 → 实例化参数 → Generator 端口
//     产出 draft ItemVersion（A 线实例化引擎 Go 面未随本波次移植，经端口
//     注入——X6 等价：命题蓝图经 Registry 由调用方（学科包装载器/教研后台）
//     注入模板版本 dict、RubricTemplate 与 pack_digest，本模块只做编排）；
//   - validate 校验：内建量规完整性验证器（验收③）+ 注入验证器，验证结果
//     显式写入 Artifact.Verdicts 传给装配阶段；
//   - assemble 装配：门通过 → ItemSink 端口入库（入库唯一路径的门强制，
//     T-W1-007 语义）；未通过 → 不入库，返回 verdict 便于诊断。
//
// 与冻结实现的接线差异（如实记录）：Python run_d_pipeline 直接持有
// AsyncSession 跑 run_gate + publish_item_version；Go 面校验门与 content
// writer 的接线属后续波次，本骨架以端口注入同一语义（验证不过不发布），
// 蓝图注册表为 Memory 面。
//
// 为什么 D 线流水线不直接感知学科模板：核心域零学科特判（宪法 A5/X6）。
package production

import (
	"errors"
	"fmt"
	"math"
	"strings"
	"sync"

	"github.com/Cloudbird-Software/AI_Web_School/core/scoring"
)

// D 线流水线常量（Python run_d_pipeline 传给 instantiate 的字面量）.
const (
	// DLineInteractionID 开放式作答交互 id.
	DLineInteractionID = "writing"
	// DLineScorerID 量规评分器 id（scorer.yaml ai_rubric 契约）.
	DLineScorerID = "ai_rubric"
	// DLineSignedBy 流水线签发人（lineage.signed_by）.
	DLineSignedBy = "d-line-pipeline"
	// 默认学段（grade_band 缺省 M，Python params.get("grade_band", "M")）.
	DefaultGradeBand = GradebandM
)

// 流水线阶段名（StageTrace.Stage 值域）.
const (
	StageGenerate = "generate"
	StageValidate = "validate"
	StageAssemble = "assemble"
)

// 门判定值域（与冻结 gate final_verdict 同域）.
const (
	VerdictPass   = "pass"
	VerdictFail   = "fail"
	VerdictReview = "review"
)

// ────────────────────────────────────────────────────────────────────
// 哨兵错误
// ────────────────────────────────────────────────────────────────────

var (
	// ErrBlueprintNotRegistered 表示蓝图未在 D 线注册表注册.
	ErrBlueprintNotRegistered = errors.New("production: 蓝图未在 D 线注册表注册")

	// ErrBlueprintIDMismatch 表示注册时 blueprint_id 参数与
	// Blueprint.BlueprintID 不一致（fail-loud，防止键错位）.
	ErrBlueprintIDMismatch = errors.New("production: blueprint_id 参数与 Blueprint.blueprint_id 不一致")

	// ErrInvalidPipelineParams 表示流水线参数缺失（topic/picture_ref/prompt
	// 等；Python ValueError 的哨兵化）.
	ErrInvalidPipelineParams = errors.New("production: D 线流水线参数缺失")

	// ErrNilPipelinePort 表示流水线构造/执行面收到 nil 端口或产物
	//（阶段契约违例，fail-loud）.
	ErrNilPipelinePort = errors.New("production: D 线流水线端口/产物为 nil")
)

// ────────────────────────────────────────────────────────────────────
// 实例化参数构建（Python _build_instantiate_params 移植）
// ────────────────────────────────────────────────────────────────────

// BuildInstantiateParams 按写作类型与学段 spec 构建实例化参数.
//
//	composition: {topic, word_count_min, word_count_max, time_limit_minutes}
//	picture_writing: {picture_ref, prompt, word_count_min, word_count_max, time_limit_minutes}
func BuildInstantiateParams(blueprint *Blueprint, params map[string]any, spec GradeBandSpec) (map[string]any, error) {
	if blueprint == nil {
		return nil, fmt.Errorf("%w: blueprint 为 nil", ErrNilPipelinePort)
	}
	base := map[string]any{
		"word_count_min":     spec.WordCountMin,
		"word_count_max":     spec.WordCountMax,
		"time_limit_minutes": spec.TimeLimitMinutes,
	}
	switch blueprint.WritingType {
	case WritingComposition:
		topic := strParam(params, "topic")
		if topic == "" {
			return nil, fmt.Errorf("%w: composition 蓝图 params 缺 topic", ErrInvalidPipelineParams)
		}
		base["topic"] = topic
	case WritingPicture:
		pictureRef := strParam(params, "picture_ref")
		prompt := strParam(params, "prompt")
		if pictureRef == "" {
			return nil, fmt.Errorf("%w: picture_writing 蓝图 params 缺 picture_ref", ErrInvalidPipelineParams)
		}
		if prompt == "" {
			return nil, fmt.Errorf("%w: picture_writing 蓝图 params 缺 prompt", ErrInvalidPipelineParams)
		}
		base["picture_ref"] = pictureRef
		base["prompt"] = prompt
	default:
		return nil, fmt.Errorf("%w: 未知 writing_type: %q", ErrInvalidPipelineParams, blueprint.WritingType)
	}
	return base, nil
}

// strParam 取非空字符串参数（Python params.get(...) 的 falsy 语义同构：
// 缺失/非串/空串一律视为缺）.
func strParam(params map[string]any, key string) string {
	s, _ := params[key].(string)
	return s
}

// ────────────────────────────────────────────────────────────────────
// 蓝图注册表（核心域不 import 学科包；由调用方注入——Memory 面）
// ────────────────────────────────────────────────────────────────────

// BlueprintEntry 是注册表条目：蓝图 + 量规 + 模板版本 + 学科包摘要.
type BlueprintEntry struct {
	Blueprint *Blueprint
	Rubric    *RubricTemplate
	// TemplateVersion A 线母题模板版本 dict（composition/picture_writing yaml
	// 内容；经 Generator 端口传给实例化引擎）.
	TemplateVersion map[string]any
	// PackDigest 学科包摘要（sha256:...；公式一 item_version_id 输入）.
	PackDigest string
}

// BlueprintRegistry 是 D 线蓝图注册表（Memory 面）：blueprint_id → entry。
// 为什么注册表而非 DB：T-W4-021 只验「流水线可执行」，蓝图 DB 持久化
// （迁移 0018 的 blueprint/rubric_template 表）由教研后台后续波次接入；
// 本注册表供测试与早期集成注入。并发安全（-race 纪律）.
type BlueprintRegistry struct {
	mu      sync.RWMutex
	entries map[string]BlueprintEntry
}

// NewBlueprintRegistry 构造空注册表.
func NewBlueprintRegistry() *BlueprintRegistry {
	return &BlueprintRegistry{entries: make(map[string]BlueprintEntry)}
}

// Register 注册一条 D 线命题蓝图（供流水线按 id 查找）。blueprint_id 参数
// 须与 Blueprint.BlueprintID 一致（不一致 fail-loud）.
func (r *BlueprintRegistry) Register(blueprintID string, entry BlueprintEntry) error {
	if entry.Blueprint == nil {
		return fmt.Errorf("%w: 注册条目缺 Blueprint", ErrNilPipelinePort)
	}
	if blueprintID != entry.Blueprint.BlueprintID {
		return fmt.Errorf("%w: 参数 %q vs Blueprint.blueprint_id %q",
			ErrBlueprintIDMismatch, blueprintID, entry.Blueprint.BlueprintID)
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	r.entries[blueprintID] = entry
	return nil
}

// Get 取已注册的 D 线蓝图条目；未注册返回 ErrBlueprintNotRegistered wrap.
func (r *BlueprintRegistry) Get(blueprintID string) (BlueprintEntry, error) {
	r.mu.RLock()
	defer r.mu.RUnlock()
	entry, ok := r.entries[blueprintID]
	if !ok {
		return BlueprintEntry{}, fmt.Errorf("%w: 蓝图 %q", ErrBlueprintNotRegistered, blueprintID)
	}
	return entry, nil
}

// Reset 清空注册表（测试隔离用）.
func (r *BlueprintRegistry) Reset() {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.entries = make(map[string]BlueprintEntry)
}

// ────────────────────────────────────────────────────────────────────
// 端口：生成（A 线实例化引擎）与入库（content writer）
// ────────────────────────────────────────────────────────────────────

// GenerateRequest 是生成阶段传给 Generator 端口的显式输入（验收②：评分器
// 指向 ai_rubric、量规嵌入 scorer_params 由 Generator 实现落进产物 scoring_ref）.
type GenerateRequest struct {
	Blueprint         *Blueprint
	GradeBand         string
	Spec              GradeBandSpec
	InstantiateParams map[string]any
	Rubric            *RubricTemplate
	TemplateVersion   map[string]any
	PackDigest        string
	InteractionID     string // DLineInteractionID
	ScorerID          string // DLineScorerID
	Locale            string
	SignedBy          string // DLineSignedBy
}

// Generator 是生成阶段端口（A 线实例化引擎的注入面）。实现必须纯函数化：
// 同一请求必得同一 ItemVersion（D3）.
type Generator interface {
	Generate(req GenerateRequest) (*ItemVersion, error)
}

// GeneratorFunc 把函数适配为 Generator.
type GeneratorFunc func(req GenerateRequest) (*ItemVersion, error)

// Generate 实现 Generator.
func (f GeneratorFunc) Generate(req GenerateRequest) (*ItemVersion, error) { return f(req) }

// Publication 是装配阶段入库回执.
type Publication struct {
	ItemID        string
	ItemVersionID string
	// CertificateID 门证书 id（入库唯一路径的门强制，D2；端口实现回填）.
	CertificateID string
}

// ItemSink 是装配阶段入库端口（content writer 的注入面；published 必持证）.
type ItemSink interface {
	Publish(iv *ItemVersion) (*Publication, error)
}

// ItemSinkFunc 把函数适配为 ItemSink.
type ItemSinkFunc func(iv *ItemVersion) (*Publication, error)

// Publish 实现 ItemSink.
func (f ItemSinkFunc) Publish(iv *ItemVersion) (*Publication, error) { return f(iv) }

// ────────────────────────────────────────────────────────────────────
// 校验器
// ────────────────────────────────────────────────────────────────────

// ValidatorResult 是单验证器判定（冻结 ValidatorResult 的骨架投影）.
type ValidatorResult struct {
	ValidatorID string
	Verdict     string // VerdictPass / VerdictFail
	// Blocking true 表示 fail 时阻断发布（Python blocking classvar）.
	Blocking bool
	Evidence map[string]any
}

// ArtifactValidator 是校验阶段验证器端口.
type ArtifactValidator interface {
	ValidatorID() string
	Validate(artifact *PipelineArtifact) ValidatorResult
}

// PipelineArtifact 是阶段间显式传递的载体：每个阶段的输出字段就是下一阶段
// 的显式输入，流水线不做任何隐式重建（阶段显式性由测试锚定）.
type PipelineArtifact struct {
	BlueprintID string
	GradeBand   string
	// InstantiateParams 生成阶段的参数构建输出 → 校验/装配阶段可追溯.
	InstantiateParams map[string]any
	// ItemVersion 生成阶段产物（draft）→ 校验/装配阶段的唯一对象.
	ItemVersion *ItemVersion
	// Verdicts 校验阶段输出（按验证器执行序）→ 装配阶段的门依据.
	Verdicts []ValidatorResult
}

// FinalVerdict 汇总判定：任一 blocking fail → fail；否则任一非 blocking
// fail → review；全 pass → pass（冻结门语义同构）.
func (a *PipelineArtifact) FinalVerdict() string {
	verdict := VerdictPass
	for _, v := range a.Verdicts {
		if v.Verdict != VerdictPass {
			if v.Blocking {
				return VerdictFail
			}
			verdict = VerdictReview
		}
	}
	return verdict
}

// RubricCompleteness 是量规完整性验证器（Python RubricCompletenessValidator
// 移植，验收③：维度齐全/分值合计正确/等级描述非空；量规不完整 → 阻断发布）。
//
// 从 artifact.ItemVersion.ScoringRef 的 scorer_params.rubric 取量规，复用
// core/scoring.ParseRubric（T-W4-019）做结构校验，并额外校验分值合计一致性
// 与锚点非空——为什么复用 ParseRubric 而非自写校验：量规结构契约单一来源，
// ParseRubric 已覆盖结构校验；本验证器只补「分值合计」与「描述非空」两项
// ParseRubric 未强制的语义校验.
type RubricCompleteness struct{}

// ValidatorID 实现 ArtifactValidator（冻结 validator_id "rubric_completeness"）.
func (RubricCompleteness) ValidatorID() string { return "rubric_completeness" }

// Validate 实现 ArtifactValidator。校验项（任一失败即 fail）：
//  1. scoring_ref.scorer_params.rubric 存在；
//  2. ParseRubric 成功（dimensions 非空 / 每维度 id·name·anchors·score_bands 齐全）；
//  3. 等级描述（anchors）逐条非空字符串；
//  4. total_max_score == sum(dimensions.max_score)（分值合计正确）.
func (RubricCompleteness) Validate(artifact *PipelineArtifact) ValidatorResult {
	fail := func(reason string, extra map[string]any) ValidatorResult {
		evidence := map[string]any{"reason": reason, "validator": "rubric_completeness"}
		for k, v := range extra {
			evidence[k] = v
		}
		return ValidatorResult{
			ValidatorID: "rubric_completeness",
			Verdict:     VerdictFail,
			Blocking:    true,
			Evidence:    evidence,
		}
	}
	if artifact == nil || artifact.ItemVersion == nil {
		return fail("artifact 无 ItemVersion，无法读取 scoring_ref", nil)
	}

	scoringRef := artifact.ItemVersion.ScoringRef
	if scoringRef == nil {
		return fail("scoring_ref 非 dict", nil)
	}
	scorerParams, _ := scoringRef["scorer_params"].(map[string]any)
	raw, ok := scorerParams["rubric"]
	if !ok || raw == nil {
		return fail("scoring_ref.scorer_params 缺 rubric（量规未嵌入题目元数据）", nil)
	}
	rubric, ok := raw.(map[string]any)
	if !ok {
		return fail(fmt.Sprintf("量规结构非法：rubric 类型 %T 不是 object", raw), nil)
	}

	// §3.1 结构校验：ParseRubric 覆盖 dimensions/anchors/score_bands 齐全性.
	parsed, err := scoring.ParseRubric(rubric)
	if err != nil {
		return fail(fmt.Sprintf("量规结构非法：%v", err), nil)
	}

	// §3.2 等级描述（anchors）逐条非空字符串（读原始 JSON 通道锚点，
	// ParseRubric 已把锚点字符串化，非串/空白串在此显式拒绝）.
	var emptyAnchors []string
	for _, dim := range parsed.Dimensions {
		rawDim, _ := rubricDimensionAt(rubric, dim.ID)
		rawAnchors, _ := rawDim["anchors"].([]any)
		for i, anchor := range rawAnchors {
			s, isStr := anchor.(string)
			if !isStr || strings.TrimSpace(s) == "" {
				emptyAnchors = append(emptyAnchors, fmt.Sprintf("%s.anchor#%d", dim.ID, i))
			}
		}
	}
	if len(emptyAnchors) > 0 {
		return fail(fmt.Sprintf("等级描述为空：%v", emptyAnchors), nil)
	}

	// §3.3 分值合计：total_max_score == sum(dimensions.max_score).
	actualTotal := 0.0
	for _, d := range parsed.Dimensions {
		actualTotal += d.MaxScore
	}
	if math.Abs(actualTotal-parsed.TotalMaxScore) > 1e-9 {
		return fail(fmt.Sprintf("分值合计不正确：声明 %v，实际维度满分合计 %v",
			parsed.TotalMaxScore, actualTotal), nil)
	}

	dimIDs := make([]any, 0, len(parsed.Dimensions))
	for _, d := range parsed.Dimensions {
		dimIDs = append(dimIDs, d.ID)
	}
	return ValidatorResult{
		ValidatorID: "rubric_completeness",
		Verdict:     VerdictPass,
		Blocking:    true,
		Evidence: map[string]any{
			"dimensions":      dimIDs,
			"total_max_score": parsed.TotalMaxScore,
			"checked":         []any{"structure", "anchors_non_empty", "score_total"},
		},
	}
}

// rubricDimensionAt 按维度 id 从原始量规 dict 取维度对象（锚点原文读取用；
// ParseRubric 已保证 dimensions 数组结构与 id 存在）.
func rubricDimensionAt(rubric map[string]any, dimID string) (map[string]any, bool) {
	rawDims, _ := rubric["dimensions"].([]any)
	for _, raw := range rawDims {
		m, ok := raw.(map[string]any)
		if !ok {
			continue
		}
		if id, _ := m["id"].(string); id == dimID {
			return m, true
		}
	}
	return nil, false
}

// ────────────────────────────────────────────────────────────────────
// 流水线编排
// ────────────────────────────────────────────────────────────────────

// DPipelineRequest 是一次 D 线流水线运行请求.
type DPipelineRequest struct {
	// BlueprintID 命题蓝图 id（须已注册）.
	BlueprintID string
	// Params 实例化参数（composition: {topic}；picture_writing:
	// {picture_ref, prompt}；可选 grade_band 覆盖学段）.
	Params map[string]any
	// GradeBand 学段 L/M/H；空串时取 Params["grade_band"]，再缺省 M.
	GradeBand string
	// Locale 语言/地区；空串取 DefaultLocale.
	Locale string
}

// StageTrace 是阶段留痕（阶段顺序与显式传递的可审计面）.
type StageTrace struct {
	Stage  string
	Status string // ok / failed / skipped
	Detail string
}

// DPipelineResult 是 D 线流水线结果（验收①：item_id 与门证书 id）.
//
//   - ItemID：入库后的 item id（空=未入库，门未通过）。
//   - ItemVersionID：生成产物 id（无论门是否通过都有值，便于诊断）。
//   - CertificateID：门证书 id（仅门通过时非空）。
//   - FinalVerdict：门综合判定 pass/fail/review。
type DPipelineResult struct {
	ItemID        string
	ItemVersionID string
	CertificateID string
	FinalVerdict  string
	Published     bool
	StageTraces   []StageTrace
	FinalArtifact *PipelineArtifact
}

// DPipeline 是 D 线流水线：蓝图 → 实例化 → 校验 → 装配。端口不可为 nil
// （构造期 fail-loud）；阶段间经 PipelineArtifact 显式传递；生成/装配阶段
// 错误即时上抛（fail-loud），校验不过走 verdict=fail 不入库（与冻结门语义
// 一致：门失败不是异常，是判定）.
type DPipeline struct {
	registry   *BlueprintRegistry
	generator  Generator
	sink       ItemSink
	validators []ArtifactValidator
}

// NewDPipeline 构造流水线。extraValidators 追加在校验链尾（内建量规完整性
// 验证器总是先跑——量规完整性是 D 线题目的结构底线）.
func NewDPipeline(registry *BlueprintRegistry, generator Generator, sink ItemSink, extraValidators ...ArtifactValidator) (*DPipeline, error) {
	if registry == nil {
		return nil, fmt.Errorf("%w: registry", ErrNilPipelinePort)
	}
	if generator == nil {
		return nil, fmt.Errorf("%w: generator", ErrNilPipelinePort)
	}
	if sink == nil {
		return nil, fmt.Errorf("%w: sink", ErrNilPipelinePort)
	}
	validators := make([]ArtifactValidator, 0, 1+len(extraValidators))
	validators = append(validators, RubricCompleteness{})
	validators = append(validators, extraValidators...)
	return &DPipeline{registry: registry, generator: generator, sink: sink, validators: validators}, nil
}

// Run 执行 D 线端到端流水线：蓝图→实例化→校验→签发入库（验收①）.
func (p *DPipeline) Run(req DPipelineRequest) (*DPipelineResult, error) {
	// 1. 取蓝图（未注册 → fail-loud）.
	entry, err := p.registry.Get(req.BlueprintID)
	if err != nil {
		return nil, err
	}
	blueprint, rubric := entry.Blueprint, entry.Rubric
	if rubric == nil || entry.TemplateVersion == nil {
		return nil, fmt.Errorf("%w: 蓝图 %q 注册条目缺 rubric/template_version", ErrNilPipelinePort, req.BlueprintID)
	}

	locale := req.Locale
	if locale == "" {
		locale = DefaultLocale
	}

	// 2. 学段 + 实例化参数（Python grade_band or params["grade_band"] or "M"）.
	gradeBand := req.GradeBand
	if gradeBand == "" {
		gradeBand = strParam(req.Params, "grade_band")
	}
	if gradeBand == "" {
		gradeBand = DefaultGradeBand
	}
	spec, err := blueprint.SpecFor(gradeBand)
	if err != nil {
		return nil, err
	}
	instParams, err := BuildInstantiateParams(blueprint, req.Params, spec)
	if err != nil {
		return nil, err
	}

	result := &DPipelineResult{StageTraces: make([]StageTrace, 0, 3)}
	artifact := &PipelineArtifact{
		BlueprintID:       blueprint.BlueprintID,
		GradeBand:         gradeBand,
		InstantiateParams: instParams,
	}

	// 3. 生成阶段（fail-loud：实例化失败上抛，不产出半成品）.
	iv, err := p.generator.Generate(GenerateRequest{
		Blueprint:         blueprint,
		GradeBand:         gradeBand,
		Spec:              spec,
		InstantiateParams: instParams,
		Rubric:            rubric,
		TemplateVersion:   entry.TemplateVersion,
		PackDigest:        entry.PackDigest,
		InteractionID:     DLineInteractionID,
		ScorerID:          DLineScorerID,
		Locale:            locale,
		SignedBy:          DLineSignedBy,
	})
	if err != nil {
		result.StageTraces = append(result.StageTraces, StageTrace{Stage: StageGenerate, Status: "failed", Detail: err.Error()})
		return nil, fmt.Errorf("%s 阶段失败: %w", StageGenerate, err)
	}
	if iv == nil {
		result.StageTraces = append(result.StageTraces, StageTrace{Stage: StageGenerate, Status: "failed", Detail: "generator 返回 nil ItemVersion"})
		return nil, fmt.Errorf("%s 阶段失败: %w", StageGenerate, ErrNilPipelinePort)
	}
	artifact.ItemVersion = iv // 显式传递：校验/装配阶段只看这份产物
	result.ItemVersionID = iv.ItemVersionID
	result.StageTraces = append(result.StageTraces, StageTrace{Stage: StageGenerate, Status: "ok", Detail: iv.ItemVersionID})

	// 4. 校验阶段（验收③：量规完整性 + 注入验证器；判定写入 artifact）.
	for _, v := range p.validators {
		if v == nil {
			result.StageTraces = append(result.StageTraces, StageTrace{Stage: StageValidate, Status: "failed", Detail: "nil validator"})
			return nil, fmt.Errorf("%s 阶段失败: %w", StageValidate, ErrNilPipelinePort)
		}
		vr := v.Validate(artifact)
		vr.ValidatorID = v.ValidatorID()
		artifact.Verdicts = append(artifact.Verdicts, vr)
	}
	verdict := artifact.FinalVerdict()
	result.FinalVerdict = verdict
	result.StageTraces = append(result.StageTraces, StageTrace{Stage: StageValidate, Status: "ok", Detail: verdict})

	// 5. 门未通过 → 不入库（返回 item_version_id 便于诊断，ItemID/Cert 为空）.
	if verdict != VerdictPass {
		result.FinalArtifact = artifact
		result.StageTraces = append(result.StageTraces, StageTrace{Stage: StageAssemble, Status: "skipped", Detail: "verdict=" + verdict})
		return result, nil
	}

	// 6. 门通过 → 入库（验收①：返回 item_id 与 cert_id；fail-loud）.
	pub, err := p.sink.Publish(artifact.ItemVersion)
	if err != nil {
		result.StageTraces = append(result.StageTraces, StageTrace{Stage: StageAssemble, Status: "failed", Detail: err.Error()})
		return nil, fmt.Errorf("%s 阶段失败: %w", StageAssemble, err)
	}
	if pub == nil || pub.ItemID == "" {
		result.StageTraces = append(result.StageTraces, StageTrace{Stage: StageAssemble, Status: "failed", Detail: "sink 返回空回执"})
		return nil, fmt.Errorf("%s 阶段失败: %w", StageAssemble, ErrNilPipelinePort)
	}
	result.ItemID = pub.ItemID
	result.CertificateID = pub.CertificateID
	result.Published = true
	result.FinalArtifact = artifact
	result.StageTraces = append(result.StageTraces, StageTrace{Stage: StageAssemble, Status: "ok", Detail: pub.ItemID})
	return result, nil
}
