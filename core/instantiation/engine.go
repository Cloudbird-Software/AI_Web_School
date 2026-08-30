// Package instantiation 承载确定性实例化引擎（Python 冻结基准
// src/core/instantiation/engine/engine.py 的 Go 移植；T-W2-004）。
//
// 核心流程：按母题 spec + params 确定性产出 ItemVersion——
//  1. 解析 spec 为六大块强类型（dsl.ParseSpec，T-W2-001）
//  2. NormalizeParams：按 slot.type 规范化为 JSON 兼容确定性表示
//     （decimal→定点字符串、fraction→"n/d"），避免浮点漂移
//  3. 求 answer_program：用安全表达式求值器（expr.Evaluate，T-W2-002）算正解
//  4. 生成 distractors：每条 rule 调用干扰项生成器（T-W2-003）
//  5. 装配 content：presentation.blocks 用 {slot_name} 插值
//  6. 装配 interaction_ref / scoring_ref / error_bindings / lineage / objective
//  7. ComputeInstanceID（公式一）= H(tvd, normalized_params, pack_digest,
//     engine_digest, corpus_digests, locale)
//
// 纯计算：不写 DB、不调 IO；同一 (template_version, params, pack_digest,
// engine_digest, corpus_digests, locale) 任意次实例化必得同一
// item_version_id（D3 可复现）。不签名：本引擎只产出 draft 状态的
// ItemVersion；签发由校验门承载（§4 状态机）。
//
// 宪法 X6：本包不 import 任何学科/学段包（tools/go-lint/import-boundary 强制）。
package instantiation

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"math/big"
	"sort"
	"strconv"
	"strings"
	"time"

	"github.com/Cloudbird-Software/AI_Web_School/core/instantiation/distractor"
	"github.com/Cloudbird-Software/AI_Web_School/core/instantiation/dsl"
	"github.com/Cloudbird-Software/AI_Web_School/core/instantiation/expr"
	"github.com/Cloudbird-Software/AI_Web_School/core/models"
)

// ────────────────────────────────────────────────────────────────────
// 引擎版本与摘要（进入公式一的 engine_digest）
// ────────────────────────────────────────────────────────────────────

// EngineVersion 引擎语义化版本。为什么用版本字符串而非代码 hash：
// 代码 hash 会随每次实现细节变化（如重构），破坏已发布实例的可复现性；
// 版本号语义化升级（破坏性变更必须升版本）。
const EngineVersion = "1.0.0"

// EngineDigest = sha256("1.0.0")（与冻结实现逐字节一致，golden 钉死）。
var EngineDigest = "sha256:" + func() string {
	sum := sha256.Sum256([]byte(EngineVersion))
	return hex.EncodeToString(sum[:])
}()

// 默认值与签名（对齐冻结实现常量）。
const (
	DefaultPipelineID = "instantiation-engine"
	DefaultSignedBy   = "instantiation-engine"
	DefaultLocale     = "zh-CN"
	defaultStatus     = "draft"
	lineageTierA      = "A"
)

// ────────────────────────────────────────────────────────────────────
// 结果模型
// ────────────────────────────────────────────────────────────────────

// ItemVersionResult 实例化结果（ItemVersion dict 的强类型表示）。
// 与 item_version 表六大块对齐（契约 §2.2）。
type ItemVersionResult struct {
	ItemVersionID  string           `json:"item_version_id"` // 公式一内容寻址哈希
	ItemID         string           `json:"item_id"`         // 不变身份，A/B 级 = item_version_id（自引用）
	Status         string           `json:"status"`          // 实例化产物默认 draft
	Objective      map[string]any   `json:"objective"`       // 知识标注集（来自母题）
	InteractionRef map[string]any   `json:"interaction_ref"` // 交互类型 + 参数
	Content        map[string]any   `json:"content"`         // 题面语义 AST + 素材引用
	ScoringRef     map[string]any   `json:"scoring_ref"`     // 评分器 + 参数
	ErrorBindings  []map[string]any `json:"error_bindings"`  // 选项/评分维度 → 错误类型
	Lineage        map[string]any   `json:"lineage"`         // 生产谱系
}

// InstantiateOptions 实例化参数（对齐 instantiate 关键字参数）。
type InstantiateOptions struct {
	PackDigest    string         // 所属学科包摘要（sha256:...）
	InteractionID string         // 交互类型 id（调用方保证已注册）
	ScorerID      string         // 评分器 id（调用方保证已注册）
	ScorerParams  map[string]any // 评分器参数；nil → {}
	Locale        string         // 空 → zh-CN
	CorpusDigests []string       // 语料库版本摘要链
	Seed          int64          // 实例化随机种子（当前确定性实例化不使用，仅记入 lineage）
	EngineDigest  string         // 空 → EngineDigest
	SignedBy      string         // 空 → instantiation-engine
	SignedAt      string         // 空 → 当前 UTC（lineage 元数据，不进 id）
}

// Instantiate 确定性实例化母题为 ItemVersion。
//
// templateVersion：母题版本 dict，必须含 template_version_id、template_id、
// spec（可选 dsl_version，默认 "1"）；params：实例化参数（槽名 → 值）。
//
// 错误（fail-closed）：spec 结构不合规（*dsl.SpecError）、参数规范化失败、
// answer_program 求值失败、干扰项碰撞/规则配置错误。
func Instantiate(templateVersion map[string]any, params map[string]any, opt InstantiateOptions) (*ItemVersionResult, error) {
	// ── 1. 解析母题版本 ──
	tvID, _ := templateVersion["template_version_id"].(string)
	tplID, _ := templateVersion["template_id"].(string)
	if tvID == "" {
		return nil, errors.New("template_version 缺少 template_version_id 字段")
	}
	if tplID == "" {
		return nil, errors.New("template_version 缺少 template_id 字段")
	}
	specDict, ok := templateVersion["spec"]
	if !ok {
		return nil, errors.New("template_version.spec 必须为 dict")
	}

	// ── 2. 解析 spec 为强类型（六大块校验） ──
	spec, err := dsl.ParseSpec(specDict)
	if err != nil {
		return nil, err
	}

	// ── 3. 规范化参数（禁浮点漂移） ──
	normalized, err := NormalizeParams(params, spec.Slots)
	if err != nil {
		return nil, err
	}

	// ── 4. 求正解（answer_program） ──
	env := evalEnv(params, spec.Slots)
	answerValue, err := expr.Evaluate(spec.AnswerProgram.Expression, env)
	if err != nil {
		return nil, fmt.Errorf("answer_program 求值失败：%w", err)
	}

	// ── 5. 生成干扰项 + error_bindings ──
	bindings, err := buildErrorBindings(spec, env, answerValue)
	if err != nil {
		return nil, err
	}

	// ── 6. 装配 content（presentation 插值） ──
	content, err := renderContent(spec, normalized)
	if err != nil {
		return nil, err
	}

	// ── 7. 装配 interaction_ref / scoring_ref / objective / lineage ──
	interactionRef := map[string]any{
		"interaction_id":     opt.InteractionID,
		"interaction_params": map[string]any{}, // 由交互类型 schema 决定，本引擎不绑定
	}
	scorerParams := opt.ScorerParams
	if scorerParams == nil {
		scorerParams = map[string]any{}
	}
	scoringRef := map[string]any{
		"scorer_id":     opt.ScorerID,
		"scorer_params": scorerParams,
	}
	corpusDigests := opt.CorpusDigests
	corpusRefs := make([]any, 0, len(corpusDigests))
	for _, d := range corpusDigests {
		corpusRefs = append(corpusRefs, map[string]any{
			"corpus_version_id": d,
			"digest":            d,
		})
	}
	signedAt := opt.SignedAt
	if signedAt == "" {
		signedAt = time.Now().UTC().Format("2006-01-02T15:04:05.999999999-07:00")
	}
	signedBy := opt.SignedBy
	if signedBy == "" {
		signedBy = DefaultSignedBy
	}
	lineage := map[string]any{
		"tier":                lineageTierA, // 规则模板实例化默认 A 级
		"pipeline":            map[string]any{"id": DefaultPipelineID, "version": EngineVersion},
		"template_version_id": tvID,
		"params":              map[string]any{"normalized": normalized},
		"seed":                opt.Seed,
		"corpus_refs":         corpusRefs,
		"ai_ledger_refs":      []any{}, // A 级实例无 AI 起草
		"signed_by":           signedBy,
		"signed_at":           signedAt,
	}

	// ── 8. 计算公式一：item_version_id ──
	engineDigest := opt.EngineDigest
	if engineDigest == "" {
		engineDigest = EngineDigest
	}
	if opt.Locale == "" {
		opt.Locale = DefaultLocale
	}
	itemVersionID, err := models.ComputeInstanceID(
		tvID, normalized, opt.PackDigest, engineDigest, corpusDigests, opt.Locale)
	if err != nil {
		return nil, fmt.Errorf("item_version_id 计算失败: %w", err)
	}

	// ── 9. A/B 级 item_id = item_version_id（自引用，不变身份） ──
	return &ItemVersionResult{
		ItemVersionID:  itemVersionID,
		ItemID:         itemVersionID,
		Status:         defaultStatus,
		Objective:      objectiveMap(spec.Objective),
		InteractionRef: interactionRef,
		Content:        content,
		ScoringRef:     scoringRef,
		ErrorBindings:  bindings,
		Lineage:        lineage,
	}, nil
}

// ────────────────────────────────────────────────────────────────────
// 规范化参数（禁浮点漂移）
// ────────────────────────────────────────────────────────────────────

// NormalizeParams 规范化参数字典：每个值按 slot.type 转为 JSON 兼容确定性
// 表示（int→int、decimal→定点字符串、fraction→"n/d"、其余字符串化）。
// 未在 slots 中声明的 params 键被拒绝（防止隐式参数影响 id）。
func NormalizeParams(params map[string]any, slots map[string]dsl.Slot) (map[string]any, error) {
	normalized := make(map[string]any, len(params))
	// 键序排序遍历：错误信息确定性（首个错误不随 map 序漂移）。
	names := make([]string, 0, len(params))
	for name := range params {
		names = append(names, name)
	}
	sort.Strings(names)
	for _, name := range names {
		slot, ok := slots[name]
		if !ok {
			return nil, fmt.Errorf("未知槽名 %q（不在 spec.slots 声明中）", name)
		}
		v, err := normalizeValue(params[name], slot.Type, name)
		if err != nil {
			return nil, err
		}
		normalized[name] = v
	}
	return normalized, nil
}

// normalizeValue 按 slot.type 规范化单个值（对齐 _normalize_value）。
func normalizeValue(value any, slotType, slotName string) (any, error) {
	switch slotType {
	case "int":
		// int(value)：接受整数字面量（int/json.Number/整值字符串）；
		// 非整数按 Python int() 截断语义处理 float；其他类型拒绝。
		return normalizeInt(value, slotName)
	case "decimal":
		// Decimal(str(value)) 后取最短定表示（去尾零、无 E 记号）——
		// 同值不同字面量（'3.10' / '3.1'）规范化后必相同（D3 可复现）。
		r, err := decimalToRat(numberLiteralString(value))
		if err != nil {
			return nil, fmt.Errorf("槽 %q (decimal) 规范化失败：%s", slotName, err.Error())
		}
		s, err := expr.RatDecimalString(r)
		if err != nil {
			return nil, fmt.Errorf("槽 %q (decimal) 规范化失败：%s", slotName, err.Error())
		}
		return s, nil
	case "fraction":
		// 接受 "3/4" 与 "0.75"（对齐 Fraction(str(value))），输出最简 "n/d"。
		r, ok := newRat().SetString(numberLiteralString(value))
		if !ok {
			return nil, fmt.Errorf("槽 %q (fraction) 规范化失败：%q", slotName, numberLiteralString(value))
		}
		return r.RatString(), nil
	case "string":
		return pythonStr(value), nil
	case "bool":
		return truthy(value), nil
	case "choice":
		return pythonStr(value), nil
	default:
		return nil, fmt.Errorf("槽 %q 类型未知：%q", slotName, slotType)
	}
}

func normalizeInt(value any, slotName string) (any, error) {
	switch v := value.(type) {
	case int:
		return int64(v), nil
	case int64:
		return v, nil
	case jsonNumber:
		i, ok := newBigInt().SetString(string(v), 10)
		if !ok || !i.IsInt64() {
			// 非整数字面量 → 按 float 截断语义
			f, ferr := parseNumberFloat(string(v))
			if ferr != nil {
				return nil, fmt.Errorf("槽 %q (int) 规范化失败：%q 无法转为 int", slotName, string(v))
			}
			return int64(f), nil
		}
		return i.Int64(), nil
	case float64:
		return int64(v), nil // Python int(5.7) 截断语义
	case string:
		if i, err := strconv.ParseInt(strings.TrimSpace(v), 10, 64); err == nil {
			return i, nil
		}
		f, ferr := strconv.ParseFloat(strings.TrimSpace(v), 64)
		if ferr != nil {
			return nil, fmt.Errorf("槽 %q (int) 规范化失败：%q 无法转为 int", slotName, v)
		}
		return int64(f), nil
	default:
		return nil, fmt.Errorf("槽 %q (int) 规范化失败：%v 无法转为 int", slotName, value)
	}
}

// ────────────────────────────────────────────────────────────────────
// 求值 env：把参数转为可求值的原生值
// ────────────────────────────────────────────────────────────────────

// evalEnv 构造求值器 env：decimal → Decimal（expr.NewDecimal）、
// fraction → Fraction（expr.NewFraction）、其余原值。
// 为什么不直接用 normalized 字符串：求值器 env 需要可计算值。
func evalEnv(params map[string]any, slots map[string]dsl.Slot) map[string]any {
	env := make(map[string]any, len(params))
	for name, value := range params {
		slot, ok := slots[name]
		if !ok {
			continue // NormalizeParams 已校验，这里兜底
		}
		switch slot.Type {
		case "decimal":
			r, err := decimalToRat(numberLiteralString(value))
			if err != nil {
				continue // 求值阶段由未知标识符/类型错兜底拒绝
			}
			env[name] = r
		case "fraction":
			r, ok := newRat().SetString(numberLiteralString(value))
			if !ok {
				continue
			}
			env[name] = r
		default:
			env[name] = value
		}
	}
	return env
}

// ────────────────────────────────────────────────────────────────────
// error_bindings 装配
// ────────────────────────────────────────────────────────────────────

// buildErrorBindings 遍历 distractor_rules，生成 error_bindings 列表。
// 每条 rule 产 1+ 个 option，每个 option 对应一个 error_binding：
// {option_value, label, error_type_id, collision, corpus_ref}。
// 碰撞策略：allow_collision=false（确定性场景必须严格不碰撞）；
// corpus_sample 的占位 value=nil 与任何非 nil 正解都不碰撞。
func buildErrorBindings(spec *dsl.ItemTemplateSpec, env map[string]any, answerValue expr.Value) ([]map[string]any, error) {
	bindings := make([]map[string]any, 0, len(spec.DistractorRules.Rules))
	for _, rule := range spec.DistractorRules.Rules {
		result, err := distractor.Generate(rule, env, answerValue, false)
		if err != nil {
			var coll *distractor.CollisionError
			if errors.As(err, &coll) {
				return nil, fmt.Errorf(
					"干扰项规则 (error_type_id=%q) 与正解碰撞：%w", rule.ErrorTypeID, err)
			}
			return nil, fmt.Errorf(
				"干扰项规则 (error_type_id=%q) 生成失败：%w", rule.ErrorTypeID, err)
		}
		for _, opt := range result.Options {
			bindings = append(bindings, map[string]any{
				"option_value":  opt.Value,
				"label":         labelAny(opt),
				"error_type_id": opt.ErrorBinding,
				"collision":     opt.Collision,
				"corpus_ref":    corpusRefAny(opt),
			})
		}
	}
	return bindings, nil
}

func labelAny(opt distractor.Option) any {
	if opt.HasLabel {
		return opt.Label
	}
	return nil
}

func corpusRefAny(opt distractor.Option) any {
	if opt.HasCorpusRef {
		return opt.CorpusRef
	}
	return nil
}

// ────────────────────────────────────────────────────────────────────
// presentation 插值
// ────────────────────────────────────────────────────────────────────

// renderContent 装配 content 块：presentation.blocks 全部插值。
// 返回 {"blocks": [...]}（与契约 §2.2 content 结构对齐）。
func renderContent(spec *dsl.ItemTemplateSpec, params map[string]any) (map[string]any, error) {
	blocks := make([]any, 0, len(spec.Presentation.Blocks))
	for _, block := range spec.Presentation.Blocks {
		rendered, err := interpolateBlock(block, params)
		if err != nil {
			return nil, err
		}
		blocks = append(blocks, map[string]any{
			"kind":     block.Kind,
			"template": block.Template,
			"rendered": rendered,
		})
	}
	return map[string]any{"blocks": blocks}, nil
}

// interpolateBlock 对单个 presentation block 做无逻辑插值：只支持
// {slot_name} 简单插值；{{ }} 转义字面花括号；format 规格（{a:>3}）与
// 属性/下标引用（{a.b}）fail-closed 拒绝（对齐 str.format_map 的
// _SafeFormatDict：缺失键显式报错而非静默保留）。
func interpolateBlock(block dsl.PresentationBlock, params map[string]any) (string, error) {
	var sb strings.Builder
	t := block.Template
	for i := 0; i < len(t); i++ {
		switch c := t[i]; c {
		case '{':
			if i+1 < len(t) && t[i+1] == '{' {
				sb.WriteByte('{')
				i++
				continue
			}
			end := strings.IndexByte(t[i:], '}')
			if end < 0 {
				return "", fmt.Errorf(
					"presentation block (kind=%q) 模板格式错误：未闭合的 '{'", block.Kind)
			}
			field := t[i+1 : i+end]
			rendered, err := formatField(field, block, params)
			if err != nil {
				return "", err
			}
			sb.WriteString(rendered)
			i += end
		case '}':
			if i+1 < len(t) && t[i+1] == '}' {
				sb.WriteByte('}')
				i++
				continue
			}
			return "", fmt.Errorf(
				"presentation block (kind=%q) 模板格式错误：单独的 '}'", block.Kind)
		default:
			sb.WriteByte(c)
		}
	}
	return sb.String(), nil
}

// formatField 解析单个替换字段：field_name ['!' conversion] [':' format_spec]。
func formatField(field string, block dsl.PresentationBlock, params map[string]any) (string, error) {
	if field == "" {
		return "", fmt.Errorf(
			"presentation block (kind=%q) 模板格式错误：空字段名（位置参数不在 DSL 子集）", block.Kind)
	}
	name := field
	for i := 0; i < len(field); i++ {
		if field[i] == '!' || field[i] == ':' {
			return "", fmt.Errorf(
				"presentation block (kind=%q) 不支持转换/格式规格 %q（fail-closed）",
				block.Kind, field)
		}
		if field[i] == '.' || field[i] == '[' {
			return "", fmt.Errorf(
				"presentation block (kind=%q) 不支持属性/下标引用 %q（fail-closed）",
				block.Kind, field)
		}
	}
	v, ok := params[name]
	if !ok {
		return "", fmt.Errorf(
			"presentation block (kind=%q) 插值失败：presentation 模板引用了未提供的槽：%q",
			block.Kind, name)
	}
	return pythonStr(v), nil
}

// ────────────────────────────────────────────────────────────────────
// 辅助
// ────────────────────────────────────────────────────────────────────

// objectiveMap 把强类型 Objective 转为 dict（对齐 model_dump：
// steps 缺省时显式 null 键）。
func objectiveMap(o dsl.Objective) map[string]any {
	kpSet := make([]any, 0, len(o.KPSet))
	for _, kp := range o.KPSet {
		kpSet = append(kpSet, map[string]any{
			"dimension": kp.Dimension,
			"code":      kp.Code,
		})
	}
	var steps any
	if o.Steps != nil {
		lst := make([]any, 0, len(o.Steps))
		for _, st := range o.Steps {
			kp := make([]any, 0, len(st.KP))
			for _, k := range st.KP {
				kp = append(kp, k)
			}
			lst = append(lst, map[string]any{"step_id": st.StepID, "kp": kp})
		}
		steps = lst
	}
	return map[string]any{
		"kp_set":          kpSet,
		"kp_set_mode":     o.KPSetMode,
		"cognitive_level": o.CognitiveLevel,
		"gradeband":       o.Gradeband,
		"graph_release":   o.GraphRelease,
		"steps":           steps,
	}
}

// pythonStr 对齐 Python str() 的字符串化（插值与规范化共用）：
// bool → "True"/"False"、nil → "None"、数字按字面量、其余字符串原样。
func pythonStr(v any) string {
	switch x := v.(type) {
	case nil:
		return "None"
	case bool:
		if x {
			return "True"
		}
		return "False"
	case string:
		return x
	case int:
		return strconv.Itoa(x)
	case int64:
		return strconv.FormatInt(x, 10)
	case float64:
		return strconv.FormatFloat(x, 'g', -1, 64)
	case jsonNumber:
		return string(x)
	default:
		if val, ok := v.(expr.Value); ok {
			return expr.String(val)
		}
		return fmt.Sprintf("%v", v)
	}
}

// truthy 对齐 Python bool(v)。
func truthy(v any) bool {
	switch x := v.(type) {
	case nil:
		return false
	case bool:
		return x
	case string:
		return x != ""
	case int:
		return x != 0
	case int64:
		return x != 0
	case float64:
		return x != 0
	case jsonNumber:
		f, err := parseNumberFloat(string(x))
		return err == nil && f != 0
	default:
		return true
	}
}

// numberLiteralString 把参数值还原为数字字面量文本（对齐 str(value) 后
// 交给 Decimal/Fraction 解析的口径；json.Number 保字面量避免浮点漂移）。
func numberLiteralString(v any) string {
	switch x := v.(type) {
	case string:
		return x
	case jsonNumber:
		return string(x)
	case int:
		return strconv.Itoa(x)
	case int64:
		return strconv.FormatInt(x, 10)
	case float64:
		return strconv.FormatFloat(x, 'g', -1, 64)
	default:
		return pythonStr(v)
	}
}

// jsonNumber 数字字面量（encoding/json 的 UseNumber 解码形态：
// 保字面量文本，避免 float64 舍入污染 id 计算）。
type jsonNumber = json.Number

// ────────────────────────────────────────────────────────────────────
// 数值解析辅助
// ────────────────────────────────────────────────────────────────────

// decimalToRat 把十进制字面量精确转为有理数（复用 expr.NewDecimal 的
// 严格字面量校验：拒绝 "1/2" 分数形式混入 decimal 槽）。
func decimalToRat(s string) (*big.Rat, error) {
	dv, err := expr.NewDecimal(s)
	if err != nil {
		return nil, err
	}
	return dv.R, nil
}

func newRat() *big.Rat    { return new(big.Rat) }
func newBigInt() *big.Int { return new(big.Int) }

func parseNumberFloat(s string) (float64, error) { return strconv.ParseFloat(s, 64) }
