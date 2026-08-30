// Package difficulty 承载难度重估触发器（Python 冻结基准
// src/core/instantiation/difficulty/trigger.py 的 Go 移植；T-W2-006）。
//
// 检测逻辑：比较当前 params 与 baseline_params，若任何
// difficulty_relevant=true 的槽值发生变化 → 触发难度重估事件。
//
// 事件 schema：specs/contracts/events/difficulty_reestimate_event.md v1.0.0
//
// 设计要点（对齐冻结实现）：
//  1. 学科无关：只读 spec.slots[*].difficulty_relevant 标志；
//  2. 确定性：同一 (template_version, params, baseline_params) 必得同一结果；
//  3. PII 安全：事件不含学生信息（D7），仅含题目参数级数据；
//  4. 分场景：事件含 scene 字段（D5 禁止混估）。
//  5. 纯函数面：Redis 传输（W2 阶段 RPUSH 队列）不进本包，由调用方承载；
//     本包只产出已通过 schema 校验的事件。
//
// 宪法 X6：本包不 import 任何学科/学段包。
package difficulty

import (
	"crypto/rand"
	"errors"
	"fmt"
	"strings"
	"time"

	"github.com/Cloudbird-Software/AI_Web_School/core/gate/validators"
	"github.com/Cloudbird-Software/AI_Web_School/core/instantiation/dsl"
	"github.com/Cloudbird-Software/AI_Web_School/core/instantiation/expr"
)

// Scene 事件场景（D5 分场景独立估计，禁止混估）。
type Scene string

// 三类合法场景（对齐契约 v1.0.0）。
const (
	ScenePractice    Scene = "practice"
	SceneDiagnosis   Scene = "diagnosis"
	SceneMeasurement Scene = "measurement"
)

func (s Scene) valid() bool {
	return s == ScenePractice || s == SceneDiagnosis || s == SceneMeasurement
}

// EventType 事件类型固定值。
const EventType = "difficulty_reestimate"

// ReestimateEvent 难度重估事件（对齐契约 v1.0.0）。
type ReestimateEvent struct {
	EventType         string         `json:"event_type"`
	EventID           string         `json:"event_id"`
	ItemVersionID     string         `json:"item_version_id"`
	TemplateVersionID string         `json:"template_version_id"`
	PackDigest        string         `json:"pack_digest"`
	ChangedSlots      []string       `json:"changed_slots"`
	Params            map[string]any `json:"params"`
	BaselineParams    map[string]any `json:"baseline_params"`
	Scene             Scene          `json:"scene"`
	CreatedAt         string         `json:"created_at"`
}

// validate 事件 schema 校验（对齐 Pydantic field_validator 全集）。
func (e *ReestimateEvent) validate() error {
	if e.EventType != EventType {
		return fmt.Errorf("event_type 必须为 'difficulty_reestimate'，实际为 %q", e.EventType)
	}
	if len(e.ChangedSlots) == 0 {
		return errors.New("changed_slots 不得为空（空列表不应触发事件）")
	}
	for _, f := range []struct{ name, val string }{
		{"item_version_id", e.ItemVersionID},
		{"template_version_id", e.TemplateVersionID},
		{"pack_digest", e.PackDigest},
	} {
		if !strings.HasPrefix(f.val, validators.DigestPrefix) {
			return fmt.Errorf("%s 必须以 'sha256:' 开头，实际为 %q", f.name, f.val)
		}
	}
	if !e.Scene.valid() {
		return fmt.Errorf("scene 必须为 practice/diagnosis/measurement，实际为 %q", e.Scene)
	}
	return nil
}

// DetectDifficultyChange 检测 params 是否变更了 difficulty_relevant 槽。
//
// 比较 params 与 baselineParams 中 difficulty_relevant=true 的槽值：
//   - 任一槽值不同 → true（命中）
//   - 无 difficulty_relevant 槽、或全部相同 → false
//   - baselineParams 为 nil → false（无基准，无法检测变更）
//
// 仅检测两者都提供的槽（缺失视为未变更，避免误报）；值相等走跨类型
// 精确比较（对齐 Python !=：1 != 1.0 为 False）。
func DetectDifficultyChange(templateVersion map[string]any, params, baselineParams map[string]any) (bool, error) {
	if baselineParams == nil {
		return false, nil
	}
	spec, err := specOf(templateVersion)
	if err != nil {
		return false, err
	}
	for name, slot := range spec.Slots {
		if !slot.DifficultyRelevant {
			continue
		}
		p, pok := params[name]
		b, bok := baselineParams[name]
		if !pok || !bok {
			continue
		}
		if !valueEqualAny(p, b) {
			return true, nil
		}
	}
	return false, nil
}

// EmitOptions 事件构造参数。
type EmitOptions struct {
	ItemVersionID string
	PackDigest    string
	Scene         Scene
	EventID       string           // 空 → 自动生成 UUID v4
	CreatedAt     string           // 空 → 当前 UTC（RFC3339）
	Now           func() time.Time // 可注入时钟（测试用；nil 用 time.Now）
}

// EmitDifficultyReestimate 构造难度重估事件（已通过 schema 校验）。
//
// 流程（对齐冻结实现）：比较 params 与 baselineParams 找出变更的
// difficulty_relevant 槽；无变更槽 → 拒绝（调用方应先调
// DetectDifficultyChange）；baselineParams 为 nil → 拒绝。
// Redis 传输不进本包（调用方持队列客户端），只返回事件。
func EmitDifficultyReestimate(templateVersion map[string]any, params, baselineParams map[string]any, opt EmitOptions) (*ReestimateEvent, error) {
	spec, err := specOf(templateVersion)
	if err != nil {
		return nil, err
	}
	if baselineParams == nil {
		return nil, errors.New(
			"baseline_params 为 nil，无法检测变更；请先调 DetectDifficultyChange 确认有变更")
	}
	changed := changedSlots(spec, params, baselineParams)
	if len(changed) == 0 {
		return nil, errors.New("未检测到 difficulty_relevant 槽变更，不应发布事件")
	}

	tvID, _ := templateVersion["template_version_id"].(string)
	eventID := opt.EventID
	if eventID == "" {
		eventID = uuid4()
	}
	createdAt := opt.CreatedAt
	if createdAt == "" {
		now := time.Now
		if opt.Now != nil {
			now = opt.Now
		}
		createdAt = now().UTC().Format("2006-01-02T15:04:05.999999999-07:00")
	}
	event := &ReestimateEvent{
		EventType:         EventType,
		EventID:           eventID,
		ItemVersionID:     opt.ItemVersionID,
		TemplateVersionID: tvID,
		PackDigest:        opt.PackDigest,
		ChangedSlots:      changed,
		Params:            params,
		BaselineParams:    baselineParams,
		Scene:             opt.Scene,
		CreatedAt:         createdAt,
	}
	if err := event.validate(); err != nil {
		return nil, err
	}
	return event, nil
}

// ────────────────────────────────────────────────────────────────────
// 内部
// ────────────────────────────────────────────────────────────────────

// specOf 从 template_version 提取并校验 spec。
func specOf(templateVersion map[string]any) (*dsl.ItemTemplateSpec, error) {
	specDict, ok := templateVersion["spec"]
	if !ok {
		return nil, errors.New("template_version.spec 必须为 dict")
	}
	return dsl.ParseSpec(specDict)
}

// changedSlots 返回所有发生变更的 difficulty_relevant 槽名列表
// （map 遍历序 → 排序稳定化，保证事件确定性）。
func changedSlots(spec *dsl.ItemTemplateSpec, params, baselineParams map[string]any) []string {
	names := make([]string, 0, len(spec.Slots))
	for name := range spec.Slots {
		names = append(names, name)
	}
	sortStrings(names)
	changed := make([]string, 0, len(names))
	for _, name := range names {
		slot := spec.Slots[name]
		if !slot.DifficultyRelevant {
			continue
		}
		p, pok := params[name]
		b, bok := baselineParams[name]
		if !pok || !bok {
			continue
		}
		if !valueEqualAny(p, b) {
			changed = append(changed, name)
		}
	}
	return changed
}

// valueEqualAny 对齐 Python !=：数值跨表示按精确值（1 == 1.0），
// 字符串不冒充数值，其余类型直接比较。
func valueEqualAny(a, b any) bool {
	av, aerr := expr.ToValue(a)
	bv, berr := expr.ToValue(b)
	if aerr != nil || berr != nil {
		// 非可求值域值（如嵌套 dict）：退化到深比较语义由调用方保证；
		// 本触发器只处理参数级标量，双不可转换视为不等。
		return false
	}
	return expr.ValuesEqual(av, bv)
}

func sortStrings(s []string) {
	for i := 1; i < len(s); i++ {
		for j := i; j > 0 && s[j] < s[j-1]; j-- {
			s[j], s[j-1] = s[j-1], s[j]
		}
	}
}

// uuid4 生成 RFC 4122 v4 UUID（crypto/rand；无外部依赖）。
func uuid4() string {
	var b [16]byte
	if _, err := rand.Read(b[:]); err != nil {
		// 随机源不可用属环境级故障：fail-loud。
		panic("difficulty: crypto/rand 不可用: " + err.Error())
	}
	b[6] = (b[6] & 0x0f) | 0x40
	b[8] = (b[8] & 0x3f) | 0x80
	const hex = "0123456789abcdef"
	out := make([]byte, 0, 36)
	for i, c := range b {
		if i == 4 || i == 6 || i == 8 || i == 10 {
			out = append(out, '-')
		}
		out = append(out, hex[c>>4], hex[c&0x0f])
	}
	return string(out)
}
