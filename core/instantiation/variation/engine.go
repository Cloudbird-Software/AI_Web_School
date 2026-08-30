// Package variation 承载受控变式引擎与变体证书（Python 冻结基准
// src/core/instantiation/variation/engine.py + certificate.py 的 Go 移植；
// T-W2-005）。
//
// 按母题 variation_axes 中指定轴的槽子集重采样，其余槽冻结；
// 对每个变式调用 Instantiate() 生成 ItemVersion；最后签发
// VariantCertificate 记录目标不变性证据。
//
// 两条建模纪律（ADR §4.1）：
//
//	①凡改变考查目标的参数必须拆母题，不得作为变式维度——本引擎检测
//	  objective 依赖槽被变更时拒绝发证（Certified=false, UNPROVEN）；
//	②优先"按构造必然合法"的生成器设计——默认采样器在槽取值域内生成值，
//	  不依赖随机源（可复现）。
//
// AI 自由改写：永远标记 UNPROVEN，不产出已认证 VariantCertificate。
//
// 证书 content-addressed：certificate_id 由 (operator_id, axis_id,
// variant_ids, objective_signature, certified) 哈希得出，同一组变式 +
// 同一 objective 必得同一证书 id（D3）。
//
// 宪法 X6：本包不 import 任何学科/学段包。
package variation

import (
	"errors"
	"fmt"
	"sort"

	"github.com/Cloudbird-Software/AI_Web_School/core/gate/validators"
	"github.com/Cloudbird-Software/AI_Web_School/core/instantiation"
	"github.com/Cloudbird-Software/AI_Web_School/core/instantiation/dsl"
	"github.com/Cloudbird-Software/AI_Web_School/core/instantiation/expr"
)

// ControlLedVariationOperator 受控变式操作者 id 常量。
const ControlLedVariationOperator = "controlled-variation-engine"

// ────────────────────────────────────────────────────────────────────
// VariantCertificate：受控变式目标不变性证书
// ────────────────────────────────────────────────────────────────────

// VariantCertificate 受控变式目标不变性证书。
//
//   - CertificateID：证书 id（内容寻址：operator+axis+variants+objective_sig）
//   - InvariantEvidence：三类证据——objective_signature（objective 的
//     kp_set + cognitive_level + gradeband 哈希）、kp_set_unchanged、
//     skill_set_unchanged，以及 axis_slots / frozen_slots；
//   - Certified：true=已认证（受控变式且不变性验证通过）；
//     false=UNPROVEN（AI 自由改写、objective 依赖槽被变更、校验失败）。
type VariantCertificate struct {
	CertificateID     string         `json:"certificate_id"`
	OperatorID        string         `json:"operator_id"`
	AxisID            string         `json:"axis_id"`
	Certified         bool           `json:"certified"`
	Reason            string         `json:"reason"`
	InvariantEvidence map[string]any `json:"invariant_evidence"`
	VariantIDs        []string       `json:"variant_ids"`
}

// IsUnproven 是否为 UNPROVEN（未认证）。
func (c *VariantCertificate) IsUnproven() bool { return !c.Certified }

// ComputeObjectiveSignature 计算 objective 的技能集合签名。
//
// 签名内容：kp_set 编码（升序）+ kp_set_mode + cognitive_level + gradeband。
// 这四项定义了"考什么技能"，变式过程中必须不变（ADR §4.1 纪律①）。
// obj 接受 map 形态（model_dump 口径）。
func ComputeObjectiveSignature(obj map[string]any) (string, error) {
	kpSet, ok := obj["kp_set"].([]any)
	if !ok {
		return "", errors.New("objective.kp_set 必须为 list")
	}
	codes := make([]string, 0, len(kpSet))
	for _, kp := range kpSet {
		m, ok := kp.(map[string]any)
		if !ok {
			return "", errors.New("objective.kp_set[*] 必须为 object")
		}
		code, _ := m["code"].(string)
		codes = append(codes, code)
	}
	sort.Strings(codes)
	cd := make([]any, len(codes))
	for i, c := range codes {
		cd[i] = c
	}
	mode, _ := obj["kp_set_mode"].(string)
	level, _ := obj["cognitive_level"].(string)
	band, _ := obj["gradeband"].(string)
	payload := map[string]any{
		"kp_codes":        cd,
		"kp_set_mode":     mode,
		"cognitive_level": level,
		"gradeband":       band,
	}
	canon, err := validators.CanonicalJSON(payload)
	if err != nil {
		return "", fmt.Errorf("objective 签名序列化失败: %w", err)
	}
	return certPayloadDigest(canon), nil
}

// objectiveSignatureOf 从强类型 spec 计算 objective 签名。
func objectiveSignatureOf(spec *dsl.ItemTemplateSpec) (string, error) {
	kpSet := make([]any, 0, len(spec.Objective.KPSet))
	for _, kp := range spec.Objective.KPSet {
		kpSet = append(kpSet, map[string]any{"dimension": kp.Dimension, "code": kp.Code})
	}
	var steps any
	if spec.Objective.Steps != nil {
		lst := make([]any, 0, len(spec.Objective.Steps))
		for _, st := range spec.Objective.Steps {
			kps := make([]any, 0, len(st.KP))
			for _, k := range st.KP {
				kps = append(kps, k)
			}
			lst = append(lst, map[string]any{"step_id": st.StepID, "kp": kps})
		}
		steps = lst
	}
	return ComputeObjectiveSignature(map[string]any{
		"kp_set":          kpSet,
		"kp_set_mode":     spec.Objective.KPSetMode,
		"cognitive_level": spec.Objective.CognitiveLevel,
		"gradeband":       spec.Objective.Gradeband,
		"graph_release":   spec.Objective.GraphRelease,
		"steps":           steps,
	})
}

// IssueCertificate 构造 VariantCertificate 并自动计算 certificate_id。
// 用工厂函数而非直接构造：certificate_id 需由其他字段派生，工厂保证
// id 一致性（避免调用方手算 id 出错）。id 不含 reason：reason 是人类可读
// 描述，同一逻辑结果可有不同措辞；id 应稳定，只依赖确定性字段。
func IssueCertificate(
	operatorID, axisID string,
	certified bool,
	reason string,
	objectiveSignature string,
	kpSetUnchanged, skillSetUnchanged bool,
	axisSlots, frozenSlots, variantIDs []string,
) *VariantCertificate {
	invariantEvidence := map[string]any{
		"objective_signature": objectiveSignature,
		"kp_set_unchanged":    kpSetUnchanged,
		"skill_set_unchanged": skillSetUnchanged,
		"axis_slots":          sortedAny(axisSlots),
		"frozen_slots":        sortedAny(frozenSlots),
	}
	certPayload := canonicalJSONString(map[string]any{
		"op":   operatorID,
		"axis": axisID,
		"vids": strAnyList(variantIDs),
		"osig": objectiveSignature,
		"cert": certified,
	})
	return &VariantCertificate{
		CertificateID:     certPayloadDigest(certPayload),
		OperatorID:        operatorID,
		AxisID:            axisID,
		Certified:         certified,
		Reason:            reason,
		InvariantEvidence: invariantEvidence,
		VariantIDs:        variantIDs,
	}
}

// MarkUnproven 标记变式为 UNPROVEN（未认证）。用于两种场景：
// objective 依赖槽被变更 → 拒绝发证；AI 自由改写 → 永远 UNPROVEN。
// UNPROVEN 时不变性证据为"无法证明"，不是"已验证不变"。
func MarkUnproven(operatorID, reason string, objectiveSignature, axisID string, axisSlots, frozenSlots, variantIDs []string) *VariantCertificate {
	return IssueCertificate(operatorID, axisID, false, reason, objectiveSignature,
		false, false, axisSlots, frozenSlots, variantIDs)
}

// MarkAIFreeRewrite 标记 AI 自由改写产物为 UNPROVEN。
// AI 自由改写可能改变表达式/结构/知识点，无法证明 objective 不变，
// 永远标记 UNPROVEN，不产出已认证 VariantCertificate（验收 §4）。
func MarkAIFreeRewrite(variant *instantiation.ItemVersionResult, aiOperatorID string, axisID, objectiveSignature string) *VariantCertificate {
	return MarkUnproven(
		aiOperatorID,
		"AI 自由改写：无法证明 objective 不变（ADR §4.1：AI 改写永标 UNPROVEN）",
		objectiveSignature,
		axisID,
		nil, nil,
		[]string{variant.ItemVersionID},
	)
}

// ────────────────────────────────────────────────────────────────────
// 默认采样器
// ────────────────────────────────────────────────────────────────────

// SamplerFunc 采样器类型：(slotName, slotDef, baseValue, variantIndex) → new_value。
type SamplerFunc func(slotName string, slot dsl.Slot, baseValue any, variantIndex int) (any, error)

// DefaultSampler 默认采样器：在槽取值域内确定性生成新值。
//
// 为什么用确定性而非随机：D3 可复现性——同一 (base_params, seed=variant_index)
// 必得同一变式集；"按构造必然合法"——在取值域内递增，避免碰撞与退化。
//
// 采样策略（按 slot.type，对齐 _default_sampler）：
//   - int：base + (index+1)；若有 min/max 则取模回绕到区间内
//   - decimal：base + Decimal(index+1)，输出最短定表示
//   - fraction：base + Fraction(index+1, 1)，输出最简 "n/d"
//   - choice：choices[(base_index + index + 1) % len(choices)]
//   - string/bool：原值（不参与数值变式，由调用方提供采样器）
func DefaultSampler(slotName string, slot dsl.Slot, baseValue any, variantIndex int) (any, error) {
	switch slot.Type {
	case "int":
		base, err := toInt64(baseValue)
		if err != nil {
			return nil, fmt.Errorf("int 槽 %q 基准值不可用: %w", slotName, err)
		}
		val := base + int64(variantIndex) + 1
		if slot.HasMin() {
			mn, err := toInt64(slot.Min)
			if err != nil {
				return nil, fmt.Errorf("int 槽 %q min 不可用: %w", slotName, err)
			}
			if val < mn {
				val = mn + int64(variantIndex)
			}
		}
		if slot.HasMax() {
			mx, err := toInt64(slot.Max)
			if err != nil {
				return nil, fmt.Errorf("int 槽 %q max 不可用: %w", slotName, err)
			}
			if val > mx {
				var span int64 = 1
				if slot.HasMin() {
					mn, _ := toInt64(slot.Min)
					span = mx - mn
				}
				if span < 1 {
					span = 1
				}
				if slot.HasMin() {
					mn, _ := toInt64(slot.Min)
					val = mn + (val-mn)%span
				} else {
					// 冻结实现对 min=None + max 的组合会 TypeError；
					// 本移植 fail-closed 拒绝。
					return nil, fmt.Errorf("int 槽 %q 配置 max 而无 min，默认采样器无法回绕", slotName)
				}
			}
		}
		return val, nil
	case "decimal":
		r, ok := newRatSetString(numberLiteral(baseValue))
		if !ok {
			return nil, fmt.Errorf("decimal 槽 %q 基准值 %v 非法", slotName, baseValue)
		}
		r.Add(r, bigRatFromInt(int64(variantIndex)+1))
		s, err := expr.RatDecimalString(r)
		if err != nil {
			return nil, fmt.Errorf("decimal 槽 %q 采样失败: %w", slotName, err)
		}
		return s, nil
	case "fraction":
		r, ok := newRatSetString(numberLiteral(baseValue))
		if !ok {
			return nil, fmt.Errorf("fraction 槽 %q 基准值 %v 非法", slotName, baseValue)
		}
		r.Add(r, bigRatFromInt(int64(variantIndex)+1))
		return r.RatString(), nil
	case "choice":
		choices := slot.Choices
		if len(choices) == 0 {
			return baseValue, nil
		}
		baseIdx := 0
		for i, c := range choices {
			if valuesEqualAny(c, baseValue) {
				baseIdx = i
				break
			}
		}
		return choices[(baseIdx+variantIndex+1)%len(choices)], nil
	default:
		// string / bool：默认不变（调用方应提供自定义采样器）
		return baseValue, nil
	}
}

// ────────────────────────────────────────────────────────────────────
// objective 依赖检测
// ────────────────────────────────────────────────────────────────────

// checkObjectiveDependency 检测变式轴是否包含 objective 依赖槽。
// 两条判定规则（任一命中即视为 objective 依赖）：
//  1. 全槽变式：轴覆盖 spec.slots 的全部槽（无冻结槽）→
//     变式改变整个题目，很可能改变考查目标；
//  2. choice 槽进表达式：轴包含 type=choice 且出现在 answer_program
//     表达式中的槽 → choice 槽在表达式中通常选择运算类型，变式会改变
//     考查的知识点（如加法→乘法）。
func checkObjectiveDependency(spec *dsl.ItemTemplateSpec, axis dsl.VariationAxis) (bool, []string) {
	allSlotNames := make(map[string]bool, len(spec.Slots))
	for name := range spec.Slots {
		allSlotNames[name] = true
	}
	axisSlotSet := map[string]bool{}
	for _, s := range axis.Slots {
		axisSlotSet[s] = true
	}

	var dependent []string
	inDep := map[string]bool{}

	// 规则 1：全槽变式（无冻结槽）
	if len(allSlotNames) > 0 {
		covers := true
		for name := range allSlotNames {
			if !axisSlotSet[name] {
				covers = false
				break
			}
		}
		if covers {
			names := make([]string, 0)
			for name := range axisSlotSet {
				if allSlotNames[name] {
					names = append(names, name)
				}
			}
			sort.Strings(names)
			for _, n := range names {
				dependent = append(dependent, n)
				inDep[n] = true
			}
		}
	}

	// 规则 2：choice 槽进表达式
	exprSlots := map[string]bool{}
	names, _ := expr.Names(spec.AnswerProgram.Expression)
	for _, n := range names {
		exprSlots[n] = true
	}
	for _, slotName := range axis.Slots {
		slot, ok := spec.Slots[slotName]
		if !ok {
			continue
		}
		if slot.Type == "choice" && exprSlots[slotName] && !inDep[slotName] {
			dependent = append(dependent, slotName)
			inDep[slotName] = true
		}
	}

	return len(dependent) > 0, dependent
}

// ────────────────────────────────────────────────────────────────────
// 主入口
// ────────────────────────────────────────────────────────────────────

// GenerateOptions 变式生成参数。
type GenerateOptions struct {
	PackDigest    string
	InteractionID string
	ScorerID      string
	ScorerParams  map[string]any
	Locale        string // 空 → zh-CN
	CorpusDigests []string
	Seed          int64
	Sampler       SamplerFunc // nil → DefaultSampler
	OperatorID    string      // 空 → ControlLedVariationOperator
}

// GenerateVariants 按变式轴重采样生成 n 个变式实例 + VariantCertificate。
//
// 流程：
//  1. 解析母题版本 spec
//  2. 查找 axisID 对应的 VariationAxis
//  3. 检测 objective 依赖：若轴含 objective 依赖槽 → 空列表 + UNPROVEN 证书
//  4. 按 axis.slots 重采样（其余槽冻结），生成 n 组 params
//  5. 对每组 params 调用 Instantiate() 生成 ItemVersion
//  6. 计算 objective 签名，作为不变性证据
//  7. 签发 VariantCertificate
func GenerateVariants(templateVersion map[string]any, axisID string, n int, baseParams map[string]any, opt GenerateOptions) ([]*instantiation.ItemVersionResult, *VariantCertificate, error) {
	if n <= 0 {
		return nil, nil, fmt.Errorf("n 必须为正整数，实际为 %d", n)
	}

	// ── 1. 解析母题版本 ──
	specDict, ok := templateVersion["spec"]
	if !ok {
		return nil, nil, errors.New("template_version.spec 必须为 dict")
	}
	spec, err := dsl.ParseSpec(specDict)
	if err != nil {
		return nil, nil, err
	}

	// ── 2. 查找变式轴 ──
	var axis *dsl.VariationAxis
	available := make([]string, 0, len(spec.VariationAxes.Axes))
	for i := range spec.VariationAxes.Axes {
		available = append(available, spec.VariationAxes.Axes[i].AxisID)
		if spec.VariationAxes.Axes[i].AxisID == axisID {
			axis = &spec.VariationAxes.Axes[i]
		}
	}
	if axis == nil {
		return nil, nil, fmt.Errorf("变式轴 %q 不存在；可用轴：%v", axisID, available)
	}

	// ── 3. 检测 objective 依赖 ──
	hasDep, depSlots := checkObjectiveDependency(spec, *axis)
	if hasDep {
		sig, sigErr := objectiveSignatureOf(spec)
		if sigErr != nil {
			return nil, nil, sigErr
		}
		frozen := make([]string, 0, len(spec.Slots))
		inAxis := map[string]bool{}
		for _, s := range axis.Slots {
			inAxis[s] = true
		}
		for name := range spec.Slots {
			if !inAxis[name] {
				frozen = append(frozen, name)
			}
		}
		cert := MarkUnproven(
			operatorID(opt),
			fmt.Sprintf("变式轴 %q 包含 objective 依赖槽 %v：改变考查目标的参数必须拆母题（ADR §4.1 纪律①）", axisID, depSlots),
			sig, axisID, axis.Slots, frozen, nil,
		)
		return nil, cert, nil
	}

	// ── 4. 按轴重采样 ──
	sampler := opt.Sampler
	if sampler == nil {
		sampler = DefaultSampler
	}
	axisSlots := map[string]bool{}
	for _, s := range axis.Slots {
		axisSlots[s] = true
	}
	frozenSlots := make([]string, 0, len(spec.Slots))
	// 校验轴内槽名都存在 + 基准参数覆盖所有槽
	sortedNames := make([]string, 0, len(spec.Slots))
	for name := range spec.Slots {
		sortedNames = append(sortedNames, name)
	}
	sort.Strings(sortedNames)
	for _, slotName := range axis.Slots {
		if _, exists := spec.Slots[slotName]; !exists {
			return nil, nil, fmt.Errorf("变式轴 %q 引用了未知槽 %q", axisID, slotName)
		}
	}
	for _, slotName := range sortedNames {
		if _, exists := baseParams[slotName]; !exists {
			return nil, nil, fmt.Errorf("基准参数缺少槽 %q（base_params 必须覆盖全部槽）", slotName)
		}
		if !axisSlots[slotName] {
			frozenSlots = append(frozenSlots, slotName)
		}
	}

	// ── 5. 生成 n 组变式参数并实例化 ──
	variants := make([]*instantiation.ItemVersionResult, 0, n)
	for i := range n {
		variantParams := map[string]any{}
		// 冻结槽：直接取基准值
		for _, slotName := range frozenSlots {
			variantParams[slotName] = baseParams[slotName]
		}
		// 轴槽：采样器生成新值
		for _, slotName := range axis.Slots {
			slotDef := spec.Slots[slotName]
			v, err := sampler(slotName, slotDef, baseParams[slotName], i)
			if err != nil {
				return nil, nil, fmt.Errorf("变式 [%d] 槽 %q 采样失败: %w", i, slotName, err)
			}
			variantParams[slotName] = v
		}
		result, err := instantiation.Instantiate(templateVersion, variantParams, instantiation.InstantiateOptions{
			PackDigest:    opt.PackDigest,
			InteractionID: opt.InteractionID,
			ScorerID:      opt.ScorerID,
			ScorerParams:  opt.ScorerParams,
			Locale:        opt.Locale,
			CorpusDigests: opt.CorpusDigests,
			Seed:          opt.Seed + int64(i),
		})
		if err != nil {
			return nil, nil, fmt.Errorf("变式 [%d] 实例化失败: %w", i, err)
		}
		variants = append(variants, result)
	}

	// ── 6. 校验 objective 不变性 ──
	// objective 来自母题（静态），所有变式共享同一 objective；
	// 此处计算签名作为不变性证据。受控变式不改母题，kp_set / skill_set 必然不变。
	sig, err := objectiveSignatureOf(spec)
	if err != nil {
		return nil, nil, err
	}

	// ── 7. 签发证书 ──
	variantIDs := make([]string, 0, len(variants))
	for _, v := range variants {
		variantIDs = append(variantIDs, v.ItemVersionID)
	}
	axisSlotList := make([]string, 0, len(axisSlots))
	for s := range axisSlots {
		axisSlotList = append(axisSlotList, s)
	}
	sort.Strings(axisSlotList)
	cert := IssueCertificate(
		operatorID(opt),
		axisID,
		true,
		fmt.Sprintf("受控变式（轴=%q, n=%d）：objective 签名一致，kp_set 与 skill_set 未变", axisID, n),
		sig,
		true, true,
		axisSlotList, frozenSlots, variantIDs,
	)
	return variants, cert, nil
}

func operatorID(opt GenerateOptions) string {
	if opt.OperatorID == "" {
		return ControlLedVariationOperator
	}
	return opt.OperatorID
}
