// b_assembler.go 承载 B 线语料装配线 v1（Python 冻结基准
// src/core/production/b_assembler.py 的 Go 移植，T-W2-017 语义同构）。
//
// 实现「框架模板 + 语料库填充 → ItemVersion draft」装配。地位：架构 v2
// §4.1 B 线 · 语料装配线（半模板级）。与 A 线对等：
//   - A 线：母题 DSL + 实例化引擎，产物走公式一（compute_instance_id）
//   - B 线：框架模板 + 语料库填充，产物走公式二
//     （ComputeCanonicalItemVersionID，canon.go）——避免跨域强依赖 A 线
//     引擎，且 B 线产物结构更接近 C/D 级「内容快照」语义。
//
// 产物特点（T-W2-017 验收 §3）：
//   - lineage.tier = "B"
//   - lineage.corpus_refs 非空，每条含 corpus_version_id + digest
//   - lineage.template_version_id + lineage.params 保留（B 线核心谱系）
//   - 同 (template, corpus_refs, params, locale) 多次装配必得同一
//     item_version_id（D3）
//
// 为什么 b_assembler 不写 DB：架构 v2 §4.1 四条线均统一汇入内容写入服务
// 入库（宪法 A7）；本模块只做纯计算产出 ItemVersion（status=draft），
// 入库由 writer 承载（与 A 线引擎一致——A 线 instantiate() 也不写 DB）。
//
// 宪法 A5/A7/X6：本文件不 import 任何学科包/学段包（仅依赖本包 canon.go
// 内容寻址）；学科函数库通过 corpus_refs 间接消费，不直接引用。
package production

import (
	"encoding/json"
	"errors"
	"fmt"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"time"
	"unicode/utf8"
)

// ────────────────────────────────────────────────────────────────────
// 错误层级（Python BAssemblerError(ValueError) 及子类的同构移植）
// ────────────────────────────────────────────────────────────────────

// BAssemblerError 是 B 线装配失败基类（errors.As 可自子类取到基类）.
type BAssemblerError struct{ Msg string }

func (e *BAssemblerError) Error() string { return "production: B 线装配失败: " + e.Msg }

// MissingCorpusError 表示 corpus_refs 为空（验收 §3：必须非空）.
// 架构 v2 §4.1 B 线：语料库是一等数据资产——产物必须携带 corpus_refs.
type MissingCorpusError struct{ BAssemblerError }

// Unwrap 暴露基类（Python 继承层级：MissingCorpusError → BAssemblerError）.
func (e *MissingCorpusError) Unwrap() error { return &e.BAssemblerError }

// SlotValidationError 表示 params 与 slots 声明不匹配
// （必填缺失/类型不符/未知槽/模板引用未知槽）.
type SlotValidationError struct{ BAssemblerError }

// Unwrap 暴露基类（Python 继承层级：SlotValidationError → BAssemblerError）.
func (e *SlotValidationError) Unwrap() error { return &e.BAssemblerError }

func missingCorpus(msg string) *MissingCorpusError {
	return &MissingCorpusError{BAssemblerError{Msg: msg}}
}

func slotValidation(msg string) *SlotValidationError {
	return &SlotValidationError{BAssemblerError{Msg: msg}}
}

// ErrInvalidTemplate 是框架模板 schema 非法的哨兵（Python Pydantic
// ValidationError 的 Go 对应面：errors.Is 分支处理，不字符串匹配）.
var ErrInvalidTemplate = errors.New("production: 框架模板 schema 非法")

// ────────────────────────────────────────────────────────────────────
// schema（Pydantic 模型的类型化移植）
// ────────────────────────────────────────────────────────────────────

// B 线简化槽类型域（Python TypeT Literal；与 A 线 DSL Slot.type 子集对齐，
// 不引入 array/object 是因为 B 线「半模板」性质：参数均为标量，复合结构
// 应升级为 A 线母题 DSL）.
const (
	SlotTypeInteger = "integer"
	SlotTypeNumber  = "number"
	SlotTypeString  = "string"
	SlotTypeBoolean = "boolean"
)

// DefaultLocale 是 assemble 的缺省 locale（Python locale="zh-CN"）.
const DefaultLocale = "zh-CN"

// SlotSpec 是 B 线框架模板的槽位声明。比 A 线 DSL Slot 简化：仅
// name/type/required/description，不含取值域/槽间约束（严格域校验由校验门
// 承载）。Required 为 nil 时取 Python 缺省 true.
type SlotSpec struct {
	Name        string
	Type        string
	Required    *bool
	Description string
}

// IsRequired 取槽必填性（nil 缺省 true，Python 同）.
func (s SlotSpec) IsRequired() bool { return s.Required == nil || *s.Required }

func (s SlotSpec) validate() error {
	if s.Name == "" {
		return fmt.Errorf("%w: slot.name 不能为空", ErrInvalidTemplate)
	}
	switch s.Type {
	case SlotTypeInteger, SlotTypeNumber, SlotTypeString, SlotTypeBoolean:
	default:
		return fmt.Errorf("%w: slot %q type %q 越域；合法域 [integer, number, string, boolean]",
			ErrInvalidTemplate, s.Name, s.Type)
	}
	return nil
}

// BlockSpec 是 B 线框架模板的题面块。用 {slot_name} 占位符引用 slots；
// 装配时替换为 params 实际值。Template 保留在产物中用于谱系追溯
// （让审计能看到原始模板字符串）。
//
// Python extra="allow"（交互特化字段如 precision/options）：冻结渲染器
// _interpolate_blocks 用 __dict__ 迭代透传 extras，但 Pydantic v2 把 extras
// 存于 model_extra、__dict__ 永远看不到——该透传分支是死代码，可观测行为
// 是 extras 不进产物。Go 按可观测行为移植：构造期容忍并丢弃 extra 键.
type BlockSpec struct {
	Type     string
	Template *string // nil = 纯静态块（Python None）
	Value    any     // nil 同 Python None（已渲染的静态内容，image/audio 等无插值时使用）
}

func blockFromMap(raw map[string]any) (BlockSpec, error) {
	typ, _ := raw["type"].(string)
	if typ == "" {
		return BlockSpec{}, fmt.Errorf("%w: block.type 缺失或非 string", ErrInvalidTemplate)
	}
	blk := BlockSpec{Type: typ}
	if v, ok := raw["template"]; ok && v != nil {
		s, ok := v.(string)
		if !ok {
			return BlockSpec{}, fmt.Errorf("%w: block.template 非 string", ErrInvalidTemplate)
		}
		blk.Template = &s
	}
	if v, ok := raw["value"]; ok {
		blk.Value = v
	}
	// extra 键（交互特化字段）按冻结可观测行为容忍并丢弃（见 BlockSpec 注释）.
	return blk, nil
}

// FrameworkTemplate 是 B 线框架模板（结构参数化）：B 线生产入口，含 slots
// 声明 + presentation 模板 + 评分器配置。不同于 A 线母题 DSL（六大块完整版
// 含 variation_axes/answer_program/distractor_rules），本模板仅含 B 线必需
// 子集：slots / presentation / objective / interaction_ref / scoring_ref /
// error_bindings。
//
// 为什么不含 answer_program：B 线「半模板」的正解由调用方在 params.answer
// 直接给出（或由更上游的装配器调用 A 线 expr_eval 算出后注入 params）；
// 本模块不做表达式求值，避免与 A 线 expr_eval 模块耦合。
type FrameworkTemplate struct {
	TemplateID      string
	TemplateVersion string
	PackID          string
	Slots           []SlotSpec
	Presentation    []BlockSpec
	Objective       map[string]any
	InteractionRef  map[string]any
	ScoringRef      map[string]any
	ErrorBindings   []any
	Description     string
}

// NewFrameworkTemplate 从 JSON 通道 dict 构造并校验框架模板（Python
// assemble 内部 `FrameworkTemplate(**template)` coerce 的同构面）。
// slots 必须是对象数组、presentation 必须是对象数组；未知顶层键报错
// （Python extra="forbid"）.
func NewFrameworkTemplate(from map[string]any) (*FrameworkTemplate, error) {
	str := func(key string) (string, error) {
		s, _ := from[key].(string)
		if s == "" {
			return "", fmt.Errorf("%w: %s 缺失或为空（min_length=1）", ErrInvalidTemplate, key)
		}
		return s, nil
	}
	templateID, err := str("template_id")
	if err != nil {
		return nil, err
	}
	templateVersion, err := str("template_version")
	if err != nil {
		return nil, err
	}
	packID, err := str("pack_id")
	if err != nil {
		return nil, err
	}

	rawSlots, ok := from["slots"].([]any)
	if !ok || len(rawSlots) == 0 {
		return nil, fmt.Errorf("%w: slots 缺失或非非空数组（min_length=1）", ErrInvalidTemplate)
	}
	slots := make([]SlotSpec, 0, len(rawSlots))
	for _, rs := range rawSlots {
		m, ok := rs.(map[string]any)
		if !ok {
			return nil, fmt.Errorf("%w: slots 元素非 object", ErrInvalidTemplate)
		}
		spec := SlotSpec{}
		spec.Name, _ = m["name"].(string)
		spec.Type, _ = m["type"].(string)
		if req, ok := m["required"].(bool); ok {
			spec.Required = &req
		}
		spec.Description, _ = m["description"].(string)
		if err := spec.validate(); err != nil {
			return nil, err
		}
		slots = append(slots, spec)
	}

	rawBlocks, ok := from["presentation"].([]any)
	if !ok || len(rawBlocks) == 0 {
		return nil, fmt.Errorf("%w: presentation 缺失或非非空数组（min_length=1）", ErrInvalidTemplate)
	}
	blocks := make([]BlockSpec, 0, len(rawBlocks))
	for _, rb := range rawBlocks {
		m, ok := rb.(map[string]any)
		if !ok {
			return nil, fmt.Errorf("%w: presentation 元素非 object", ErrInvalidTemplate)
		}
		blk, err := blockFromMap(m)
		if err != nil {
			return nil, err
		}
		blocks = append(blocks, blk)
	}

	dict := func(key string) (map[string]any, error) {
		m, ok := from[key].(map[string]any)
		if !ok {
			return nil, fmt.Errorf("%w: %s 缺失或非 object", ErrInvalidTemplate, key)
		}
		return m, nil
	}
	objective, err := dict("objective")
	if err != nil {
		return nil, err
	}
	interactionRef, err := dict("interaction_ref")
	if err != nil {
		return nil, err
	}
	scoringRef, err := dict("scoring_ref")
	if err != nil {
		return nil, err
	}
	var errorBindings []any
	if eb, ok := from["error_bindings"].([]any); ok {
		errorBindings = eb
	}
	description, _ := from["description"].(string)

	tpl := &FrameworkTemplate{
		TemplateID:      templateID,
		TemplateVersion: templateVersion,
		PackID:          packID,
		Slots:           slots,
		Presentation:    blocks,
		Objective:       objective,
		InteractionRef:  interactionRef,
		ScoringRef:      scoringRef,
		ErrorBindings:   errorBindings,
		Description:     description,
	}
	if err := tpl.Validate(); err != nil {
		return nil, err
	}
	return tpl, nil
}

// Validate 施加构造期 schema 校验（Python field_validator 等价面）：
// 标识非空 + template_version semver 形（点分段且各段全数字，≥2 段）.
func (t *FrameworkTemplate) Validate() error {
	if t.TemplateID == "" || t.TemplateVersion == "" || t.PackID == "" {
		return fmt.Errorf("%w: template_id/template_version/pack_id 均不能为空", ErrInvalidTemplate)
	}
	parts := strings.Split(t.TemplateVersion, ".")
	if len(parts) < 2 {
		return fmt.Errorf("%w: template_version 应为 semver，实际 %q", ErrInvalidTemplate, t.TemplateVersion)
	}
	for _, p := range parts {
		if p == "" || !allDigits(p) {
			return fmt.Errorf("%w: template_version 应为 semver，实际 %q", ErrInvalidTemplate, t.TemplateVersion)
		}
	}
	if len(t.Slots) == 0 {
		return fmt.Errorf("%w: slots 不能为空（min_length=1）", ErrInvalidTemplate)
	}
	if len(t.Presentation) == 0 {
		return fmt.Errorf("%w: presentation 不能为空（min_length=1）", ErrInvalidTemplate)
	}
	return nil
}

// allDigits 对齐 Python str.isdigit 的 ASCII 面（版本分段全数字）.
func allDigits(s string) bool {
	for _, r := range s {
		if r < '0' || r > '9' {
			return false
		}
	}
	return utf8.RuneCountInString(s) > 0
}

// CorpusRef 是语料库版本引用（lineage.corpus_refs 元素）。与冻结
// item_version.CorpusRef 字段一致：corpus_version_id + digest。B 线产物
// 必须非空（验收 §3）.
type CorpusRef struct {
	CorpusVersionID string
	Digest          string
}

// NewCorpusRef 从 JSON 通道 dict 构造语料引用（Python CorpusRef(**r) coerce
// 同构面）：两字段均必填非空.
func NewCorpusRef(from map[string]any) (CorpusRef, error) {
	id, _ := from["corpus_version_id"].(string)
	digest, _ := from["digest"].(string)
	if id == "" || digest == "" {
		return CorpusRef{}, fmt.Errorf("%w: CorpusRef 必须含非空 corpus_version_id 与 digest",
			ErrInvalidTemplate)
	}
	return CorpusRef{CorpusVersionID: id, Digest: digest}, nil
}

// ────────────────────────────────────────────────────────────────────
// 装配核心
// ────────────────────────────────────────────────────────────────────

// placeholderRE 占位符正则：{slot_name} 形式（与 Python str.format 区分：
// 不解析 !r/:fmt 后缀，防恶意格式串攻击）.
var placeholderRE = regexp.MustCompile(`\{([a-zA-Z_][a-zA-Z0-9_]*)\}`)

// AssembleOptions 是装配的可选项（Python keyword-only 参数面）.
type AssembleOptions struct {
	// Locale 语言/地区；空串取 DefaultLocale（zh-CN）.
	Locale string
	// SignedAt 签发时间 ISO 字符串；空串用当前 UTC（影响 lineage.signed_at，
	// 不影响 item_version_id——item_version_id 仅依赖六大块+locale）.
	SignedAt string
}

// ItemVersion 是四条生产线统一的产物面（宪法 A7：A/B/C/D 输出统一
// ItemVersion，共用同一入库服务/校验门/证据链）。字段与冻结 assemble()
// 返回 dict 同形；ToMap 输出 JSON 通道值树.
type ItemVersion struct {
	ItemVersionID  string
	ItemID         string
	Status         string
	Objective      map[string]any
	InteractionRef map[string]any
	Content        map[string]any
	ScoringRef     map[string]any
	ErrorBindings  []any
	Lineage        map[string]any
}

// ToMap 输出与冻结 assemble() 返回 dict 同形的值树（校验门 payload /
// 跨实现互验用；键序不影响规范化摘要）.
func (iv *ItemVersion) ToMap() map[string]any {
	return map[string]any{
		"item_version_id": iv.ItemVersionID,
		"item_id":         iv.ItemID,
		"status":          iv.Status,
		"objective":       iv.Objective,
		"interaction_ref": iv.InteractionRef,
		"content":         iv.Content,
		"scoring_ref":     iv.ScoringRef,
		"error_bindings":  iv.ErrorBindings,
		"lineage":         iv.Lineage,
	}
}

// Assemble 执行 B 线装配：框架模板 + 语料库填充 → ItemVersion（status=draft，
// 不入库——入库由 writer 承载）。
//
// 步骤（架构 v2 §4.1 B 线）：
//  1. 校验 corpus_refs 非空（验收 §3：语料库为一等数据资产）
//  2. 校验 params 与 template.slots 对齐
//  3. 插值 presentation.blocks：{slot_name} → params 值
//  4. 构造六大块（浅拷贝防外部污染）
//  5. ComputeCanonicalItemVersionID（公式二，D3 可复现）
//  6. 构造 lineage：tier="B"，corpus_refs 非空
//
// B 级产物 item_id = item_version_id（自引用，与 A 级 A/B 一致）。
// 装配是纯函数：同 (template, corpus_refs, params, locale, signed_at) 必得
// 同一输出（确定性测试面）.
func Assemble(
	template *FrameworkTemplate,
	corpusRefs []CorpusRef,
	params map[string]any,
	opts AssembleOptions,
) (*ItemVersion, error) {
	if template == nil {
		return nil, fmt.Errorf("%w: template 不能为 nil", ErrInvalidTemplate)
	}
	if err := template.Validate(); err != nil {
		return nil, err
	}

	// ── 验收 §3：corpus_refs 必须非空 ──
	if len(corpusRefs) == 0 {
		return nil, missingCorpus(
			"B 线装配必须携带非空 corpus_refs" +
				"（架构 v2 §4.1 B 线：语料库为一等数据资产；" +
				"任务卡 §验收 #3：lineage.corpus_refs 非空）")
	}
	for i, ref := range corpusRefs {
		if ref.CorpusVersionID == "" || ref.Digest == "" {
			return nil, fmt.Errorf("%w: corpus_refs[%d] 缺 corpus_version_id 或 digest",
				ErrInvalidTemplate, i)
		}
	}

	// ── 校验 params ──
	if err := validateParams(template.Slots, params); err != nil {
		return nil, err
	}

	// ── 插值 presentation ──
	renderedBlocks, err := interpolateBlocks(template.Presentation, params)
	if err != nil {
		return nil, err
	}
	blocksAny := make([]any, 0, len(renderedBlocks))
	for _, blk := range renderedBlocks {
		blocksAny = append(blocksAny, blk)
	}

	// ── 构造六大块（浅拷贝防外部污染）──
	locale := opts.Locale
	if locale == "" {
		locale = DefaultLocale
	}
	content := map[string]any{"blocks": blocksAny}

	// ── 构造 lineage（tier=B, corpus_refs 非空）──
	// signed_at 不影响 item_version_id：公式二仅哈希六大块+locale；调用方可
	// 固定 signed_at 保证可复现性（测试场景），生产场景由调用方注入确定
	// 时间戳（如批次时间），不依赖时钟.
	now := opts.SignedAt
	if now == "" {
		now = time.Now().UTC().Format("2006-01-02T15:04:05.000000+00:00")
	}
	refs := make([]any, 0, len(corpusRefs))
	for _, r := range corpusRefs {
		refs = append(refs, map[string]any{
			"corpus_version_id": r.CorpusVersionID,
			"digest":            r.Digest,
		})
	}
	lineage := map[string]any{
		"tier": "B",
		"pipeline": map[string]any{
			"id":      template.PackID + ".b_assembler",
			"version": template.TemplateVersion,
		},
		"template_version_id": template.TemplateID,
		"params":              copyStrAny(params), // 谱系保留参数（B 线核心信息）
		"corpus_refs":         refs,
		"signed_by":           "b_assembler",
		"signed_at":           now,
	}

	// ── 计算 item_version_id（公式二：canonical content addressing）──
	// 本实现采用公式二：不依赖 A 线实例化引擎（避免跨域强依赖），仅依赖
	// 六大块 + locale。
	itemVersionID, err := ComputeCanonicalItemVersionID(
		copyStrAny(template.Objective),
		copyStrAny(template.InteractionRef),
		content,
		copyStrAny(template.ScoringRef),
		copyAnySlice(template.ErrorBindings),
		locale,
	)
	if err != nil {
		return nil, err
	}

	// ── 构造 ItemVersion（B 级自引用：item_id = item_version_id）──
	return &ItemVersion{
		ItemVersionID:  itemVersionID,
		ItemID:         itemVersionID,
		Status:         "draft",
		Objective:      copyStrAny(template.Objective),
		InteractionRef: copyStrAny(template.InteractionRef),
		Content:        content,
		ScoringRef:     copyStrAny(template.ScoringRef),
		ErrorBindings:  copyAnySlice(template.ErrorBindings),
		Lineage:        lineage,
	}, nil
}

// validateParams 校验 params 与 slots 声明对齐（Python _validate_params 逐
// 规则移植）。规则：
//   - required slot 必须在 params 中（缺失 → *SlotValidationError）
//   - params 中的 key 必须在 slots 声明里（未知槽 → *SlotValidationError，
//     防止拼写错误悄悄通过；多个未知槽按 key 升序报首个——Go map 无序，
//     升序化保证错误确定性）
//   - 类型检查（int/float/str/bool 基础匹配；bool 不能充当 integer/number）
//
// 不做的事：取值域校验（min/max/枚举）与槽间约束 → 由校验门或 A 线 DSL 承载.
func validateParams(slots []SlotSpec, params map[string]any) error {
	slotByName := make(map[string]SlotSpec, len(slots))
	for _, s := range slots {
		slotByName[s.Name] = s
	}

	// 必填检查（按 slots 声明序，确定性）.
	for _, slot := range slots {
		if slot.IsRequired() {
			if _, ok := params[slot.Name]; !ok {
				return slotValidation(fmt.Sprintf("必填槽 %q 未在 params 中提供", slot.Name))
			}
		}
	}

	// 未知参数检查（key 升序，保证多违例时错误确定性）.
	keys := make([]string, 0, len(params))
	for k := range params {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	for _, k := range keys {
		if _, ok := slotByName[k]; !ok {
			return slotValidation(fmt.Sprintf("params 含未知槽 %q（未在 template.slots 声明）", k))
		}
	}

	// 类型检查（按 slots 声明序）.
	for _, slot := range slots {
		val, ok := params[slot.Name]
		if !ok {
			continue
		}
		if err := checkSlotType(slot, val); err != nil {
			return err
		}
	}
	return nil
}

// checkSlotType 基础类型匹配：bool 不能充当 integer/number（Python
// isinstance(True, int)==True 的防误用特判移植）.
func checkSlotType(slot SlotSpec, val any) error {
	switch slot.Type {
	case SlotTypeInteger:
		if isBool(val) {
			return slotValidation(fmt.Sprintf("槽 %q 期望 %s，实际 bool（bool 不能充当数值，防止 true→1 误用）",
				slot.Name, slot.Type))
		}
		if !isIntegral(val) {
			return slotValidation(fmt.Sprintf("槽 %q 期望 %s，实际 %s", slot.Name, slot.Type, pythonTypeName(val)))
		}
	case SlotTypeNumber:
		if isBool(val) {
			return slotValidation(fmt.Sprintf("槽 %q 期望 %s，实际 bool（bool 不能充当数值，防止 true→1 误用）",
				slot.Name, slot.Type))
		}
		if !isNumeric(val) {
			return slotValidation(fmt.Sprintf("槽 %q 期望 %s，实际 %s", slot.Name, slot.Type, pythonTypeName(val)))
		}
	case SlotTypeString:
		if _, ok := val.(string); !ok {
			return slotValidation(fmt.Sprintf("槽 %q 期望 %s，实际 %s", slot.Name, slot.Type, pythonTypeName(val)))
		}
	case SlotTypeBoolean:
		if !isBool(val) {
			return slotValidation(fmt.Sprintf("槽 %q 期望 %s，实际 %s", slot.Name, slot.Type, pythonTypeName(val)))
		}
	default:
		// 未知类型不校验（向前兼容，Python _TYPE_MAP.get 缺省同）
	}
	return nil
}

func isBool(v any) bool {
	_, ok := v.(bool)
	return ok
}

// isIntegral 判定整型：int/int64 直接判；json.Number 按数字原文是否含
// 小数/指数判（JSON 通道整数语义）.
func isIntegral(v any) bool {
	switch t := v.(type) {
	case int:
		return true
	case int64:
		return true
	case json.Number:
		return !strings.ContainsAny(string(t), ".eE")
	default:
		return false
	}
}

// isNumeric 判定数值：整型 + float64 + json.Number（bool 已在上游排除）.
func isNumeric(v any) bool {
	switch v.(type) {
	case int, int64, float64:
		return true
	case json.Number:
		return true
	default:
		return false
	}
}

// pythonTypeName 返回值的 Python 类型名（错误消息对齐 type(val).__name__）.
func pythonTypeName(v any) string {
	switch t := v.(type) {
	case nil:
		return "NoneType"
	case bool:
		return "bool"
	case string:
		return "str"
	case int, int64:
		return "int"
	case float64:
		return "float"
	case json.Number:
		if strings.ContainsAny(string(t), ".eE") {
			return "float"
		}
		return "int"
	case []any:
		return "list"
	case map[string]any:
		return "dict"
	default:
		return fmt.Sprintf("%T", v)
	}
}

// interpolateBlocks 插值 presentation.blocks：{slot_name} → params 值
// （Python _interpolate_blocks 逐规则移植）。规则：
//   - block.template 中的 {slot_name} 被替换为 params 中对应值的字符串形式
//   - 替换后的字符串写入 block.value（输出时调用方用 value 字段）
//   - block.template 保留在产物中用于谱系追溯
//   - block.template 引用未知槽 → *SlotValidationError
//
// 为什么不用 str.format：format 会解析 {:fmt} / {!r} 等后缀，B 线模板不需要
// 格式化能力，纯占位符替换更安全。extras 不透传（冻结可观测行为，见
// BlockSpec 注释）.
func interpolateBlocks(blocks []BlockSpec, params map[string]any) ([]map[string]any, error) {
	rendered := make([]map[string]any, 0, len(blocks))
	for _, blk := range blocks {
		out := map[string]any{"type": blk.Type}

		if blk.Template != nil {
			tpl := *blk.Template
			value, err := renderTemplate(tpl, params)
			if err != nil {
				return nil, err
			}
			out["value"] = value
			out["template"] = tpl // 保留模板用于谱系
		} else if blk.Value != nil {
			out["value"] = blk.Value
		}
		rendered = append(rendered, out)
	}
	return rendered, nil
}

// renderTemplate 执行单块占位符替换.
func renderTemplate(tpl string, params map[string]any) (string, error) {
	var renderErr error
	out := placeholderRE.ReplaceAllStringFunc(tpl, func(match string) string {
		name := match[1 : len(match)-1]
		val, ok := params[name]
		if !ok {
			renderErr = slotValidation(fmt.Sprintf("模板引用未知槽 %q（不在 params 中）", name))
			return match
		}
		return pythonStr(val)
	})
	if renderErr != nil {
		return "", renderErr
	}
	return out, nil
}

// pythonStr 对齐 Python str() 的标量渲染（占位符替换值）：
// str(1.5)="1.5"、str(2)="2"、str(True)="True"、str(None)="None"；
// json.Number 原文直出。B 线参数均为标量（SlotSpec 类型域承诺），
// 容器值不属于取值空间.
func pythonStr(v any) string {
	switch t := v.(type) {
	case nil:
		return "None"
	case bool:
		if t {
			return "True"
		}
		return "False"
	case string:
		return t
	case int:
		return strconv.Itoa(t)
	case int64:
		return strconv.FormatInt(t, 10)
	case float64:
		s, err := pythonFloatRepr(t)
		if err != nil {
			return strconv.FormatFloat(t, 'g', -1, 64)
		}
		return s
	case json.Number:
		return string(t)
	default:
		return fmt.Sprintf("%v", v)
	}
}

// copyStrAny 浅拷贝 map（Python dict(tpl) 防外部污染同构）；nil 返回 nil.
func copyStrAny(src map[string]any) map[string]any {
	if src == nil {
		return nil
	}
	out := make(map[string]any, len(src))
	for k, v := range src {
		out[k] = v
	}
	return out
}

// copyAnySlice 浅拷贝 slice（Python list(tpl) 同构）；nil 返回 nil.
func copyAnySlice(src []any) []any {
	if src == nil {
		return nil
	}
	out := make([]any, len(src))
	copy(out, src)
	return out
}
