// Package dsl 承载母题 DSL v1 的六大块 Schema 与 Linter（Python 冻结基准
// src/core/instantiation/dsl/schema.py + linter.py 的 Go 移植；T-W2-001）。
//
// 六大块对齐架构 v2 §4.1 A 线与 specs/contracts/db/item-model.md §2.3：
// objective / slots / variation_axes / presentation / answer_program /
// distractor_rules。
//
// 所有模型 extra='forbid'（验收 §1）：DSL 结构冻结，未声明字段一律拒绝，
// 新增字段必须升级 dsl_version。结构/类型/枚举错误以结构化 LintError 收集
// （code 机器可判定、path 为 JSON 路径、message 供人读），单次调用收集
// 全部问题而非首错即停（对齐 Pydantic ValidationError 转换口径）。
//
// 宪法 X6：本包不 import 任何学科/学段包。
package dsl

import (
	"fmt"
	"sort"
)

// AllowedSlotTypes 槽类型允许列表（验收 §2：slot 类型不在允许列表 → lint 报错）。
// 为什么这 6 种：覆盖数学/语文/英语三科母题的参数化需求——
// int/fraction/decimal 服务数学数值槽（定点/分数运算，禁浮点漂移，D2）；
// string 服务语文/英语文本槽与通用标识；bool 服务判断/开关槽；
// choice 服务有限选项枚举。
var AllowedSlotTypes = map[string]bool{
	"int": true, "decimal": true, "fraction": true,
	"string": true, "bool": true, "choice": true,
}

// AllowedSlotTypesSorted 允许列表的升序形态（错误信息用）。
func AllowedSlotTypesSorted() []string {
	out := make([]string, 0, len(AllowedSlotTypes))
	for k := range AllowedSlotTypes {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}

// ────────────────────────────────────────────────────────────────────
// 六大块强类型
// ────────────────────────────────────────────────────────────────────

// KPPoint 知识点标注项：维度 × 编码。
type KPPoint struct {
	Dimension string `json:"dimension"`
	Code      string `json:"code"`
}

// ObjectiveStep 分步过程题的步骤级知识点标注（R-Q-15）。
type ObjectiveStep struct {
	StepID string   `json:"step_id"`
	KP     []string `json:"kp"`
}

// Objective 块：知识标注集 + 认知层级 + 多点关系声明 + 学段。
type Objective struct {
	KPSet          []KPPoint       `json:"kp_set"`
	KPSetMode      string          `json:"kp_set_mode"`
	CognitiveLevel string          `json:"cognitive_level"`
	Gradeband      string          `json:"gradeband"`
	GraphRelease   string          `json:"graph_release"`
	Steps          []ObjectiveStep `json:"steps"` // 可选；nil 时对外呈现为 null（对齐 model_dump）
}

// Slot 单个槽定义。type 必须在 AllowedSlotTypes 内（Linter 强制）；
// DifficultyRelevant 标记该槽变更是否触发难度重估（T-W2-006）。
type Slot struct {
	Type               string `json:"type"`
	DifficultyRelevant bool   `json:"difficulty_relevant"`
	Min                any    `json:"min"`     // 可选（数值类型有效）
	Max                any    `json:"max"`     // 可选
	Choices            []any  `json:"choices"` // 可选（choice 类型）
	Unit               string `json:"unit"`    // 可选（单位 id）
}

// HasMin / HasMax 区分"未提供"与零值。
func (s Slot) HasMin() bool { return s.Min != nil }
func (s Slot) HasMax() bool { return s.Max != nil }

// VariationAxis 单条变式轴：按 axis_id 取 slots 子集重采样，其余槽冻结。
type VariationAxis struct {
	AxisID string   `json:"axis_id"`
	Slots  []string `json:"slots"`
}

// VariationAxes 变式轴集合。
type VariationAxes struct {
	Axes []VariationAxis `json:"axes"`
}

// PresentationBlock 单个呈现块：纯插值模板，禁止控制流（架构 v2 §4.1）。
type PresentationBlock struct {
	Kind     string `json:"kind"`
	Template string `json:"template"`
}

// Presentation 题面语义 AST（块序列）。
type Presentation struct {
	Blocks []PresentationBlock `json:"blocks"`
}

// AnswerProgram 块：计算正解的安全表达式（求值在 instantiation 引擎）。
type AnswerProgram struct {
	Expression string `json:"expression"`
	Returns    string `json:"returns"`
}

// DistractorRule 单条干扰项规则。
//
// rule_type=deterministic：用 expression（安全表达式）计算干扰项值；
// rule_type=corpus_sample：返回带 corpus_ref 的占位，等待 B 线语料装配。
// 每条规则绑定一个 error_type_id（选项→错误类型确定映射，§4.5）。
type DistractorRule struct {
	RuleType    string  `json:"rule_type"`
	ErrorTypeID string  `json:"error_type_id"`
	Expression  *string `json:"expression"` // deterministic 规则用
	CorpusRef   *string `json:"corpus_ref"` // corpus_sample 规则用
	Label       *string `json:"label"`      // 可选显示标签
}

// DistractorRules 干扰项规则集合。
type DistractorRules struct {
	Rules []DistractorRule `json:"rules"`
}

// ItemTemplateSpec 母题 DSL v1 顶层结构：六大块（架构 v2 §4.1）。
// 对应 item_template_version.spec 字段（item-model.md §2.3）。
type ItemTemplateSpec struct {
	Objective       Objective       `json:"objective"`
	Slots           map[string]Slot `json:"slots"`
	VariationAxes   VariationAxes   `json:"variation_axes"`
	Presentation    Presentation    `json:"presentation"`
	AnswerProgram   AnswerProgram   `json:"answer_program"`
	DistractorRules DistractorRules `json:"distractor_rules"`
}

// ────────────────────────────────────────────────────────────────────
// 结构化错误（对齐 linter.LintError）
// ────────────────────────────────────────────────────────────────────

// LintError 单条校验错误：code 机器可判定（snake_case）、path 为 JSON 路径、
// message 供人类阅读。
type LintError struct {
	Code    string `json:"code"`
	Path    string `json:"path"`
	Message string `json:"message"`
}

// key 用于阶段间去重（对齐 linter.py 的 (code, path) 去重）。
func (e LintError) key() string { return e.Code + "\x00" + e.Path }

// LintResult 校验结果：Valid 当且仅当 Errors 为空。
type LintResult struct {
	Valid  bool        `json:"valid"`
	Errors []LintError `json:"errors"`
}

// ────────────────────────────────────────────────────────────────────
// 结构解析（对齐 ItemTemplateSpec.model_validate：extra=forbid + 类型 +
// 必填 + 枚举；路径感知，多错收集）
// ────────────────────────────────────────────────────────────────────

// specReader 收集式解析器：错误带路径逐条收集，不在首错即停。
type specReader struct {
	errs []LintError
}

func (r *specReader) errf(code, path, format string, args ...any) {
	r.errs = append(r.errs, LintError{Code: code, Path: path, Message: fmt.Sprintf(format, args...)})
}

func (r *specReader) obj(v any, path string) (map[string]any, bool) {
	m, ok := v.(map[string]any)
	if !ok {
		r.errf("schema_violation", path, "期望 object，实际为 %T", v)
		return nil, false
	}
	return m, true
}

func (r *specReader) str(m map[string]any, key, path string) (string, bool) {
	v, ok := m[key]
	if !ok {
		r.errf("missing_field", path, "field required")
		return "", false
	}
	s, ok := v.(string)
	if !ok {
		r.errf("str_type", path, "期望 string，实际为 %T", v)
		return "", false
	}
	return s, true
}

func (r *specReader) optStr(m map[string]any, key, path string) *string {
	v, ok := m[key]
	if !ok || v == nil {
		return nil
	}
	s, ok := v.(string)
	if !ok {
		r.errf("str_type", path, "期望 string，实际为 %T", v)
		return nil
	}
	return &s
}

func (r *specReader) boolean(m map[string]any, key, path string) (bool, bool) {
	v, ok := m[key]
	if !ok {
		r.errf("missing_field", path, "field required")
		return false, false
	}
	b, ok := v.(bool)
	if !ok {
		r.errf("invalid_difficulty_relevant_type", path,
			"difficulty_relevant 必须为 boolean，实际为 %T", v)
		return false, false
	}
	return b, true
}

func (r *specReader) list(m map[string]any, key, path string) ([]any, bool) {
	v, ok := m[key]
	if !ok {
		r.errf("missing_field", path, "field required")
		return nil, false
	}
	l, ok := v.([]any)
	if !ok {
		r.errf("list_type", path, "期望 list，实际为 %T", v)
		return nil, false
	}
	return l, true
}

func (r *specReader) optList(m map[string]any, key, path string) []any {
	v, ok := m[key]
	if !ok || v == nil {
		return nil
	}
	l, ok := v.([]any)
	if !ok {
		r.errf("list_type", path, "期望 list，实际为 %T", v)
		return nil
	}
	return l
}

func (r *specReader) enum(m map[string]any, key, path string, allowed ...string) (string, bool) {
	s, ok := r.str(m, key, path)
	if !ok {
		return "", false
	}
	for _, a := range allowed {
		if s == a {
			return s, true
		}
	}
	r.errf("invalid_enum_value", path, "值 %q 不在允许列表 %v", s, allowed)
	return "", false
}

// forbidExtra 拒绝未声明字段（extra='forbid'）。
func (r *specReader) forbidExtra(m map[string]any, path string, allowed ...string) {
	ok := map[string]bool{}
	for _, a := range allowed {
		ok[a] = true
	}
	for k := range m {
		if !ok[k] {
			r.errf("extra_field_forbidden", joinPath(path, k), "Extra inputs are not permitted")
		}
	}
}

func joinPath(base, field string) string {
	if base == "" {
		return field
	}
	return base + "." + field
}

func indexPath(base string, i int) string {
	return base + "." + itoa(i)
}

func itoa(i int) string {
	if i == 0 {
		return "0"
	}
	neg := i < 0
	if neg {
		i = -i
	}
	var b [20]byte
	pos := len(b)
	for i > 0 {
		pos--
		b[pos] = byte('0' + i%10)
		i /= 10
	}
	if neg {
		pos--
		b[pos] = '-'
	}
	return string(b[pos:])
}

var (
	cognitiveLevels = []string{"remember", "understand", "apply", "analyze", "evaluate", "create"}
	kpSetModes      = []string{"single", "all_required", "compensatory"}
	gradebands      = []string{"L", "M", "H"}
	distractorTypes = []string{"deterministic", "corpus_sample"}
	objectiveFields = []string{"kp_set", "kp_set_mode", "cognitive_level", "gradeband", "graph_release", "steps"}
	slotFields      = []string{"type", "difficulty_relevant", "min", "max", "choices", "unit"}
	axisFields      = []string{"axis_id", "slots"}
	axesFields      = []string{"axes"}
	blockFields     = []string{"kind", "template"}
	blocksFields    = []string{"blocks"}
	programFields   = []string{"expression", "returns"}
	ruleFields      = []string{"rule_type", "error_type_id", "expression", "corpus_ref", "label"}
	rulesFields     = []string{"rules"}
	kpPointFields   = []string{"dimension", "code"}
	objStepFields   = []string{"step_id", "kp"}
	specFields      = []string{"objective", "slots", "variation_axes", "presentation", "answer_program", "distractor_rules"}
)

// parseSpec 把 spec（map 形态，六大块）解析为强类型并收集全部结构错误。
// 输入非 map 报单个 schema_violation（对齐 Pydantic 非 dict 输入）。
func parseSpec(v any) (*ItemTemplateSpec, []LintError) {
	r := &specReader{}
	m, ok := r.obj(v, "")
	if !ok {
		return nil, r.errs
	}
	r.forbidExtra(m, "", specFields...)
	spec := &ItemTemplateSpec{}

	// objective
	if om, ok := r.obj(m["objective"], "objective"); ok {
		r.forbidExtra(om, "objective", objectiveFields...)
		spec.Objective = r.parseObjective(om)
	}

	// slots
	if sm, ok := r.obj(m["slots"], "slots"); ok {
		spec.Slots = map[string]Slot{}
		for name, def := range sm {
			dm, ok := def.(map[string]any)
			if !ok {
				r.errf("schema_violation", joinPath("slots", name), "期望 object，实际为 %T", def)
				continue
			}
			r.forbidExtra(dm, joinPath("slots", name), slotFields...)
			slot := Slot{}
			if t, ok := r.str(dm, "type", joinPath("slots", name)+".type"); ok {
				slot.Type = t
			}
			if b, ok := r.boolean(dm, "difficulty_relevant", joinPath("slots", name)+".difficulty_relevant"); ok {
				slot.DifficultyRelevant = b
			}
			slot.Min = dm["min"]
			slot.Max = dm["max"]
			if ch, present := dm["choices"]; present && ch != nil {
				if l, ok := ch.([]any); ok {
					slot.Choices = l
				} else {
					r.errf("list_type", joinPath("slots", name)+".choices", "期望 list，实际为 %T", ch)
				}
			}
			if u := r.optStr(dm, "unit", joinPath("slots", name)+".unit"); u != nil {
				slot.Unit = *u
			}
			spec.Slots[name] = slot
		}
	}

	// variation_axes
	if vm, ok := r.obj(m["variation_axes"], "variation_axes"); ok {
		r.forbidExtra(vm, "variation_axes", axesFields...)
		if axes, ok := r.optList2(vm, "axes", "variation_axes.axes"); ok {
			spec.VariationAxes.Axes = r.parseAxes(axes)
		}
	}

	// presentation
	if pm, ok := r.obj(m["presentation"], "presentation"); ok {
		r.forbidExtra(pm, "presentation", blocksFields...)
		if blocks, ok := r.optList2(pm, "blocks", "presentation.blocks"); ok {
			spec.Presentation.Blocks = r.parseBlocks(blocks)
		}
	}

	// answer_program
	if am, ok := r.obj(m["answer_program"], "answer_program"); ok {
		r.forbidExtra(am, "answer_program", programFields...)
		if e, ok := r.str(am, "expression", "answer_program.expression"); ok {
			spec.AnswerProgram.Expression = e
		}
		if ret, ok := r.str(am, "returns", "answer_program.returns"); ok {
			spec.AnswerProgram.Returns = ret
		}
	}

	// distractor_rules
	if dm, ok := r.obj(m["distractor_rules"], "distractor_rules"); ok {
		r.forbidExtra(dm, "distractor_rules", rulesFields...)
		if rules, ok := r.optList2(dm, "rules", "distractor_rules.rules"); ok {
			spec.DistractorRules.Rules = r.parseRules(rules)
		}
	}

	return spec, r.errs
}

func (r *specReader) optList2(m map[string]any, key, path string) ([]any, bool) {
	if v, present := m[key]; present && v != nil {
		l, ok := v.([]any)
		if !ok {
			r.errf("list_type", path, "期望 list，实际为 %T", v)
			return nil, false
		}
		return l, true
	}
	return nil, true // 缺省 → 空集合（default_factory=list）
}

func (r *specReader) parseObjective(om map[string]any) Objective {
	obj := Objective{}
	// kp_set：min_length=1
	if kps, ok := r.list(om, "kp_set", "objective.kp_set"); ok {
		if len(kps) < 1 {
			r.errf("too_short", "objective.kp_set", "List should have at least 1 item")
		}
		for i, item := range kps {
			path := indexPath("objective.kp_set", i)
			im, ok := item.(map[string]any)
			if !ok {
				r.errf("schema_violation", path, "期望 object，实际为 %T", item)
				continue
			}
			r.forbidExtra(im, path, kpPointFields...)
			kp := KPPoint{}
			if d, ok := r.str(im, "dimension", joinPath(path, "dimension")); ok {
				kp.Dimension = d
			}
			if c, ok := r.str(im, "code", joinPath(path, "code")); ok {
				kp.Code = c
			}
			obj.KPSet = append(obj.KPSet, kp)
		}
	}
	if mode, ok := r.enum(om, "kp_set_mode", "objective.kp_set_mode", kpSetModes...); ok {
		obj.KPSetMode = mode
	}
	if cl, ok := r.enum(om, "cognitive_level", "objective.cognitive_level", cognitiveLevels...); ok {
		obj.CognitiveLevel = cl
	}
	if gb, ok := r.enum(om, "gradeband", "objective.gradeband", gradebands...); ok {
		obj.Gradeband = gb
	}
	if gr, ok := r.str(om, "graph_release", "objective.graph_release"); ok {
		obj.GraphRelease = gr
	}
	// steps：可选
	if steps, present := om["steps"]; present && steps != nil {
		l, ok := steps.([]any)
		if !ok {
			r.errf("list_type", "objective.steps", "期望 list，实际为 %T", steps)
		} else {
			for i, item := range l {
				path := indexPath("objective.steps", i)
				im, ok := item.(map[string]any)
				if !ok {
					r.errf("schema_violation", path, "期望 object，实际为 %T", item)
					continue
				}
				r.forbidExtra(im, path, objStepFields...)
				st := ObjectiveStep{}
				if s, ok := r.str(im, "step_id", joinPath(path, "step_id")); ok {
					st.StepID = s
				}
				if kps, ok := r.list(im, "kp", joinPath(path, "kp")); ok {
					for j, kp := range kps {
						ks, ok := kp.(string)
						if !ok {
							r.errf("str_type", joinPath(path, "kp")+"."+itoa(j), "期望 string，实际为 %T", kp)
							continue
						}
						st.KP = append(st.KP, ks)
					}
				}
				obj.Steps = append(obj.Steps, st)
			}
		}
	}
	return obj
}

func (r *specReader) parseAxes(axes []any) []VariationAxis {
	out := make([]VariationAxis, 0, len(axes))
	for i, item := range axes {
		path := indexPath("variation_axes.axes", i)
		im, ok := item.(map[string]any)
		if !ok {
			r.errf("schema_violation", path, "期望 object，实际为 %T", item)
			continue
		}
		r.forbidExtra(im, path, axisFields...)
		ax := VariationAxis{}
		if id, ok := r.str(im, "axis_id", joinPath(path, "axis_id")); ok {
			ax.AxisID = id
		}
		if slots, ok := r.list(im, "slots", joinPath(path, "slots")); ok {
			for j, s := range slots {
				ss, ok := s.(string)
				if !ok {
					r.errf("str_type", joinPath(path, "slots")+"."+itoa(j), "期望 string，实际为 %T", s)
					continue
				}
				ax.Slots = append(ax.Slots, ss)
			}
		}
		out = append(out, ax)
	}
	return out
}

func (r *specReader) parseBlocks(blocks []any) []PresentationBlock {
	out := make([]PresentationBlock, 0, len(blocks))
	for i, item := range blocks {
		path := indexPath("presentation.blocks", i)
		im, ok := item.(map[string]any)
		if !ok {
			r.errf("schema_violation", path, "期望 object，实际为 %T", item)
			continue
		}
		r.forbidExtra(im, path, blockFields...)
		blk := PresentationBlock{}
		if k, ok := r.str(im, "kind", joinPath(path, "kind")); ok {
			blk.Kind = k
		}
		if t, ok := r.str(im, "template", joinPath(path, "template")); ok {
			blk.Template = t
		}
		out = append(out, blk)
	}
	return out
}

func (r *specReader) parseRules(rules []any) []DistractorRule {
	out := make([]DistractorRule, 0, len(rules))
	for i, item := range rules {
		path := indexPath("distractor_rules.rules", i)
		im, ok := item.(map[string]any)
		if !ok {
			r.errf("schema_violation", path, "期望 object，实际为 %T", item)
			continue
		}
		r.forbidExtra(im, path, ruleFields...)
		rule := DistractorRule{}
		if rt, ok := r.enum(im, "rule_type", joinPath(path, "rule_type"), distractorTypes...); ok {
			rule.RuleType = rt
		}
		if id, ok := r.str(im, "error_type_id", joinPath(path, "error_type_id")); ok {
			rule.ErrorTypeID = id
		}
		rule.Expression = r.optStr(im, "expression", joinPath(path, "expression"))
		rule.CorpusRef = r.optStr(im, "corpus_ref", joinPath(path, "corpus_ref"))
		rule.Label = r.optStr(im, "label", joinPath(path, "label"))
		out = append(out, rule)
	}
	return out
}
