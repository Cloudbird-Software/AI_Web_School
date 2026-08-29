// candidates.go 承载组卷候选筛选的规范化模型（Python 冻结基准
// src/core/assembly/candidates.py 的 Go 移植）。
//
// 候选 = published 实例池 × 学段 × 用途许可 × 曝光历史（架构 v2 §4.4 求解段）。
// 求解器消费的规范化候选模型 CandidateItem + serving 视图行的行→模型转换。
//
// 为什么是独立规范化模型而非直接消费 item_version dict：
//   - 求解器是纯函数（确定性要求），不应感知 JSONB 六大块的嵌套结构；
//   - 用途许可/正确率先验等派生字段在加载期解析一次，求解期只做比较。
//
// 用途许可来源（v1 约定）：lineage.params.allowed_purposes（list[str]）；
// 缺失时默认全场景许可（向后兼容 W2 已发布实例）。正确率先验来源：
// lineage.params.p_correct_prior（float, 0–1）；缺失时 nil，求解器按
// Profile 的冷启动策略处理（§4.4 冷启动降级：纯先验区间+保守宽度）。
//
// 本包是纯逻辑层：DB 读取面经 CandidateStore 端口注入（Memory 实现供测试）。
// 宪法 A5/A7：不 import 任何学科包/学段包。
package assembly

import (
	"context"
	"fmt"
	"sort"
	"sync"
)

// 全用途域（Python _ALL_PURPOSES）.
var allPurposes = []string{PurposePractice, PurposeDiagnosis, PurposeMeasurement}

// kpSetMode 值域（Python Literal["single","all_required","compensatory"]）.
const (
	KpSetModeSingle       = "single"
	KpSetModeAllRequired  = "all_required"
	KpSetModeCompensatory = "compensatory"
)

// mixTag 值域（Python Literal["new","review","confusable"]）.
const (
	MixTagNew        = "new"
	MixTagReview     = "review"
	MixTagConfusable = "confusable"
)

// CandidateItem 求解器消费的规范化候选题（Python CandidateItem）。
//
// 字段全部从 item_version 六大块 + item 谱系派生；PCorrectPrior /
// AllowedPurposes / MixTag 为 v1 先验元数据约定（见包内 candidates 注释），
// S8 数据域落地后改由 item_param 供给。
type CandidateItem struct {
	ItemVersionID     string
	ItemID            string
	TemplateVersionID *string
	KpCodes           []string
	KpSetMode         string
	Gradeband         string
	InteractionID     string
	PCorrectPrior     *float64
	AllowedPurposes   []string
	// MixTag 内容配比标签（新学/复习/易混淆；nil=未标注，不参与配比统计）.
	MixTag *string
	// GroupID 题组 id（同一题组的题作为整体入选/排除；nil=孤立题）.
	GroupID *string
}

// IsIsolated 孤立题：单知识点且声明为 single（诊断归因的定位题，§4.5）
// （Python CandidateItem.is_isolated）.
func (c *CandidateItem) IsIsolated() bool {
	return len(c.KpCodes) == 1 && c.KpSetMode == KpSetModeSingle
}

// validate 对齐 pydantic CandidateItem 的构造期校验（extra=forbid 领域约束）.
func (c *CandidateItem) validate() error {
	if len(c.KpCodes) < 1 {
		return fmt.Errorf("assembly: candidate %q kp_codes 不能为空（min_length=1）", c.ItemVersionID)
	}
	switch c.KpSetMode {
	case KpSetModeSingle, KpSetModeAllRequired, KpSetModeCompensatory:
	default:
		return fmt.Errorf("assembly: candidate %q kp_set_mode %q 越域；合法域 [single, all_required, compensatory]", c.ItemVersionID, c.KpSetMode)
	}
	switch c.Gradeband {
	case GradebandL, GradebandM, GradebandH:
	default:
		return fmt.Errorf("assembly: candidate %q gradeband %q 越域；合法域 [L, M, H]", c.ItemVersionID, c.Gradeband)
	}
	if c.PCorrectPrior != nil && (*c.PCorrectPrior < 0.0 || *c.PCorrectPrior > 1.0) {
		return fmt.Errorf("assembly: candidate %q p_correct_prior %v 越域（ge=0, le=1）", c.ItemVersionID, *c.PCorrectPrior)
	}
	if c.MixTag != nil {
		switch *c.MixTag {
		case MixTagNew, MixTagReview, MixTagConfusable:
		default:
			return fmt.Errorf("assembly: candidate %q mix_tag %q 越域；合法域 [new, review, confusable]", c.ItemVersionID, *c.MixTag)
		}
	}
	return nil
}

// ServingRow 是 v_serving_item_version 一行的最小投影（serving_views.sql §2
// 的列子集；Objective/InteractionRef/Lineage 为 JSONB 解码后的值树）.
type ServingRow struct {
	PackID            string
	ItemVersionID     string
	ItemID            string
	TemplateVersionID string // 空 = 无母题（NULL）
	Objective         map[string]any
	InteractionRef    map[string]any
	Lineage           map[string]any
}

// CandidateFromServingRow 从 v_serving_item_version 行构建候选（Python
// candidate_from_serving_row；row 至少含 item_version_id / item_id /
// template_version_id / objective / interaction_ref / lineage）.
func CandidateFromServingRow(row ServingRow) (CandidateItem, error) {
	objective := row.Objective
	if objective == nil {
		objective = map[string]any{}
	}
	kpSet, _ := objective["kp_set"].([]any)
	kpCodes := []string{}
	for _, k := range kpSet {
		entry, ok := k.(map[string]any)
		if !ok {
			continue
		}
		if code, ok := entry["code"].(string); ok && code != "" {
			kpCodes = append(kpCodes, code)
		}
	}
	if len(kpCodes) == 0 {
		return CandidateItem{}, fmt.Errorf(
			"assembly: item_version %s 的 objective.kp_set 为空，无法组卷", row.ItemVersionID)
	}
	interactionRef := row.InteractionRef
	if interactionRef == nil {
		interactionRef = map[string]any{}
	}
	lineage := row.Lineage
	if lineage == nil {
		lineage = map[string]any{}
	}
	params, _ := lineage["params"].(map[string]any)
	if params == nil {
		params = map[string]any{}
	}

	var purposes []string
	if raw, present := params["allowed_purposes"].([]any); present {
		purposes = []string{}
		for _, p := range raw {
			s, _ := p.(string)
			purposes = append(purposes, s)
		}
		known := map[string]struct{}{}
		for _, p := range allPurposes {
			known[p] = struct{}{}
		}
		for _, p := range purposes {
			if _, ok := known[p]; !ok {
				return CandidateItem{}, fmt.Errorf("assembly: allowed_purposes 含未知场景 %q", p)
			}
		}
	}

	c := CandidateItem{
		ItemVersionID:     row.ItemVersionID,
		ItemID:            row.ItemID,
		TemplateVersionID: strPtrIfSet(row.TemplateVersionID),
		KpCodes:           kpCodes,
		KpSetMode:         KpSetModeSingle,
		Gradeband:         fmt.Sprintf("%v", objective["gradeband"]),
		InteractionID:     fmt.Sprintf("%v", interactionRef["interaction_id"]),
		AllowedPurposes:   purposes,
	}
	if mode, ok := objective["kp_set_mode"].(string); ok && mode != "" {
		c.KpSetMode = mode
	}
	if prior, ok := params["p_correct_prior"]; ok && prior != nil {
		f, err := asFloat(prior)
		if err != nil {
			return CandidateItem{}, fmt.Errorf("assembly: item_version %s p_correct_prior 非法: %v", row.ItemVersionID, err)
		}
		c.PCorrectPrior = &f
	}
	if len(c.AllowedPurposes) == 0 {
		c.AllowedPurposes = append([]string(nil), allPurposes...)
	}
	if tag, ok := params["mix_tag"].(string); ok && tag != "" {
		c.MixTag = &tag
	}
	if gid, ok := params["group_id"].(string); ok && gid != "" {
		c.GroupID = &gid
	}
	if err := (&c).validate(); err != nil {
		return CandidateItem{}, err
	}
	return c, nil
}

// CandidateStore 是候选池查询面端口（Python load_candidates 的 SQL 读取语义：
// serving 视图 published 且未退役 × 学科 × 学段）。曝光历史与用途许可的过滤
// 在求解期进行（曝光集随 队列/学生 变化，池加载保持与曝光无关，便于快照
// 固化与确定性重放）。
type CandidateStore interface {
	// LoadCandidates 返回某 学科×学段 的候选池行（实现方负责 published 过滤）.
	LoadCandidates(ctx context.Context, subjectPackID, gradeband string) ([]ServingRow, error)
}

// MemoryCandidateStore 是 CandidateStore 的内存实现（测试面；语义对齐
// _SERVING_POOL_SQL 的 WHERE pack_id = :pack_id AND objective->>'gradeband' = :gradeband）.
type MemoryCandidateStore struct {
	mu   sync.RWMutex
	rows []ServingRow
}

// NewMemoryCandidateStore 构造内存候选池.
func NewMemoryCandidateStore(rows ...ServingRow) *MemoryCandidateStore {
	return &MemoryCandidateStore{rows: append([]ServingRow(nil), rows...)}
}

// Add 追加候选行.
func (m *MemoryCandidateStore) Add(rows ...ServingRow) {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.rows = append(m.rows, rows...)
}

// LoadCandidates 按 学科×学段 过滤（顺序保持插入序——与 SQL 无 ORDER BY 的
// 结果序一致，池排序由求解器稳定哈希负责）.
func (m *MemoryCandidateStore) LoadCandidates(_ context.Context, subjectPackID, gradeband string) ([]ServingRow, error) {
	m.mu.RLock()
	defer m.mu.RUnlock()
	out := []ServingRow{}
	for _, r := range m.rows {
		if r.PackID != subjectPackID {
			continue
		}
		if gb := fmt.Sprintf("%v", r.Objective["gradeband"]); gb != gradeband {
			continue
		}
		out = append(out, r)
	}
	return out, nil
}

// LoadCandidateItems 便捷装载：查询面行 → 规范化候选（Python load_candidates
// 返回形态；转换失败即报错——坏行不允许静默混入池）.
func LoadCandidateItems(ctx context.Context, store CandidateStore, subjectPackID, gradeband string) ([]CandidateItem, error) {
	rows, err := store.LoadCandidates(ctx, subjectPackID, gradeband)
	if err != nil {
		return nil, err
	}
	out := make([]CandidateItem, 0, len(rows))
	for _, r := range rows {
		c, err := CandidateFromServingRow(r)
		if err != nil {
			return nil, err
		}
		out = append(out, c)
	}
	return out, nil
}

// strPtrIfSet 空串→nil（serving 行 template_version_id 的 NULL 同形）.
func strPtrIfSet(s string) *string {
	if s == "" {
		return nil
	}
	return &s
}

// sortedIDSet 返回集合的排序键列表（测试与审计输出确定性用）.
func sortedIDSet(s map[string]struct{}) []string {
	out := make([]string, 0, len(s))
	for k := range s {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}
