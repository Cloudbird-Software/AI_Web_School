// status.go 承载核心域 PG ENUM 的 Go 类型与两台状态机的合法转换表
// （Python 冻结基准 src/core/models/_base.py、src/core/models/item_lifecycle.py、
// src/core/data/health.py、src/core/content/publication.py 的 Go 移植）。
//
// 枚举值与迁移 DDL 逐字对齐（ENUM 由迁移 CREATE TYPE，Go 端仅声明引用）：
//   - 0002：item_tier_enum / item_version_status_enum /
//     item_template_version_status_enum / material_kind_enum /
//     material_license_decision_enum
//   - 0018：item_lifecycle_state_enum
//
// 两台状态机（职责不同，勿混淆）：
//  1. ItemVersionStatus（§4 版本状态机，0002 enum）：draft → quarantined →
//     published → retired（无回边）；签发允许 draft → published 一跳
//     （publication.py：「门证书签发 → published」）。作用于
//     item_version / material_version / corpus_version 三表共用 enum。
//  2. LifecycleState（§4.7 生命周期状态机，0018 enum + health.py
//     _ALLOWED_TRANSITIONS）：None(初始) → ACTIVE；ACTIVE ↔ WATCH（自动）；
//     WATCH → QUARANTINED；QUARANTINED → WATCH；任何非终态 → RETIRED；
//     RETIRED 终态无回边。转入 QUARANTINED / RETIRED 需门证书。
package models

import "fmt"

// ────────────────────────────────────────────────────────────────────
// 枚举类型（命名 string，零值一律为非法值——fail-closed）
// ────────────────────────────────────────────────────────────────────

// Tier 题目生产线四级（A7：谱系字段，不是分区键；四级对等）。
type Tier string

// Tier 取值（item_tier_enum）。
const (
	TierA Tier = "A"
	TierB Tier = "B"
	TierC Tier = "C"
	TierD Tier = "D"
)

// ItemVersionStatus 版本状态机枚举（item_version_status_enum；被
// item_version / material_version / corpus_version 三表复用）。
type ItemVersionStatus string

// 版本状态取值（§4：无回边）。
const (
	VersionDraft       ItemVersionStatus = "draft"
	VersionQuarantined ItemVersionStatus = "quarantined"
	VersionPublished   ItemVersionStatus = "published"
	VersionRetired     ItemVersionStatus = "retired"
)

// TemplateVersionStatus 母题版本状态（item_template_version_status_enum；
// 无 quarantined——母题不直接过门）。
type TemplateVersionStatus string

// 母题版本状态取值。
const (
	TemplateDraft     TemplateVersionStatus = "draft"
	TemplatePublished TemplateVersionStatus = "published"
	TemplateRetired   TemplateVersionStatus = "retired"
)

// MaterialKind 素材类型（material_kind_enum）。
type MaterialKind string

// 素材类型取值。
const (
	MaterialPassage MaterialKind = "passage"
	MaterialImage   MaterialKind = "image"
	MaterialTable   MaterialKind = "table"
	MaterialAudio   MaterialKind = "audio"
)

// LicenseDecision 许可决策（material_license_decision_enum；R-Q-18：
// 仅 Approved 可被素材/语料库版本引用）。
type LicenseDecision string

// 许可决策取值。
const (
	LicenseApproved LicenseDecision = "approved"
	LicenseRejected LicenseDecision = "rejected"
	LicenseExpired  LicenseDecision = "expired"
)

// LifecycleState 生命周期四态（item_lifecycle_state_enum，0018）。
type LifecycleState string

// 生命周期状态取值。零值 "" 表示 NULL（初始：该 item 尚无 transition）。
const (
	LifecycleActive      LifecycleState = "ACTIVE"
	LifecycleWatch       LifecycleState = "WATCH"
	LifecycleQuarantined LifecycleState = "QUARANTINED"
	LifecycleRetired     LifecycleState = "RETIRED"
)

// ────────────────────────────────────────────────────────────────────
// 枚举解析（DB 文本 → 类型；非法值报错，fail-closed）
// ────────────────────────────────────────────────────────────────────

// ParseTier 解析 item_tier_enum 文本。
func ParseTier(s string) (Tier, error) {
	switch Tier(s) {
	case TierA, TierB, TierC, TierD:
		return Tier(s), nil
	}
	return "", fmt.Errorf("models: 非法 tier %q（合法值 A/B/C/D）", s)
}

// ParseVersionStatus 解析 item_version_status_enum 文本。
func ParseVersionStatus(s string) (ItemVersionStatus, error) {
	switch ItemVersionStatus(s) {
	case VersionDraft, VersionQuarantined, VersionPublished, VersionRetired:
		return ItemVersionStatus(s), nil
	}
	return "", fmt.Errorf("models: 非法版本状态 %q（合法值 draft/quarantined/published/retired）", s)
}

// ParseTemplateStatus 解析 item_template_version_status_enum 文本。
func ParseTemplateStatus(s string) (TemplateVersionStatus, error) {
	switch TemplateVersionStatus(s) {
	case TemplateDraft, TemplatePublished, TemplateRetired:
		return TemplateVersionStatus(s), nil
	}
	return "", fmt.Errorf("models: 非法母题版本状态 %q（合法值 draft/published/retired）", s)
}

// ParseMaterialKind 解析 material_kind_enum 文本。
func ParseMaterialKind(s string) (MaterialKind, error) {
	switch MaterialKind(s) {
	case MaterialPassage, MaterialImage, MaterialTable, MaterialAudio:
		return MaterialKind(s), nil
	}
	return "", fmt.Errorf("models: 非法素材类型 %q（合法值 passage/image/table/audio）", s)
}

// ParseLicenseDecision 解析 material_license_decision_enum 文本。
func ParseLicenseDecision(s string) (LicenseDecision, error) {
	switch LicenseDecision(s) {
	case LicenseApproved, LicenseRejected, LicenseExpired:
		return LicenseDecision(s), nil
	}
	return "", fmt.Errorf("models: 非法许可决策 %q（合法值 approved/rejected/expired）", s)
}

// ParseLifecycleState 解析 item_lifecycle_state_enum 文本
// （空串 = NULL = 初始，返回零值）。
func ParseLifecycleState(s string) (LifecycleState, error) {
	if s == "" {
		return "", nil
	}
	switch LifecycleState(s) {
	case LifecycleActive, LifecycleWatch, LifecycleQuarantined, LifecycleRetired:
		return LifecycleState(s), nil
	}
	return "", fmt.Errorf("models: 非法生命周期状态 %q（合法值 ACTIVE/WATCH/QUARANTINED/RETIRED）", s)
}

// ────────────────────────────────────────────────────────────────────
// 状态机一：版本状态机（§4；对齐 publication.py 签发语义）
// ────────────────────────────────────────────────────────────────────

// versionStatusTransitions 版本状态机合法转换表（键 = 当前状态，值 =
// 允许的目标状态集合）。依据：
//   - _base.py：draft → quarantined → published → retired（无回边）；
//   - publication.py 签发一跳：draft/quarantined → published（跳过隔离）；
//   - publication.py：retired 为终态，无回边。
var versionStatusTransitions = map[ItemVersionStatus][]ItemVersionStatus{
	VersionDraft:       {VersionQuarantined, VersionPublished},
	VersionQuarantined: {VersionPublished},
	VersionPublished:   {VersionRetired},
	VersionRetired:     {}, // 终态
}

// CanTransitionVersionStatus 判断版本状态机 from → to 是否合法。
func CanTransitionVersionStatus(from, to ItemVersionStatus) bool {
	for _, t := range versionStatusTransitions[from] {
		if t == to {
			return true
		}
	}
	return false
}

// IsVersionStatusTerminal 报告状态是否为终态（retired 无任何回边）。
func IsVersionStatusTerminal(s ItemVersionStatus) bool {
	return s == VersionRetired
}

// VersionStatusRequiresGateCert 报告前移到 to 是否需门证书。
// 仅 published 强制（D2 门证书唯一真源；publication.py 签发必传证书，
// ck_*_published_requires_gate_cert 以 published_at 非空兜底同一约束）。
func VersionStatusRequiresGateCert(to ItemVersionStatus) bool {
	return to == VersionPublished
}

// ────────────────────────────────────────────────────────────────────
// 状态机二：生命周期状态机（§4.7；对齐 0018 迁移 + health.py）
// ────────────────────────────────────────────────────────────────────

// lifecycleTransitions 生命周期状态机合法转换表（键 = 当前状态，"" = NULL
// 初始）。逐条对齐 health.py _ALLOWED_TRANSITIONS：
//   - 初始（无既有状态）仅允许 → ACTIVE；
//   - ACTIVE ↔ WATCH（自动，基于健康度）；
//   - WATCH → QUARANTINED；QUARANTINED → WATCH（释放回观察）；
//   - ACTIVE/WATCH/QUARANTINED → RETIRED；
//   - RETIRED 终态，无任何回边。
var lifecycleTransitions = map[LifecycleState][]LifecycleState{
	"":                   {LifecycleActive},
	LifecycleActive:      {LifecycleWatch, LifecycleRetired},
	LifecycleWatch:       {LifecycleActive, LifecycleQuarantined, LifecycleRetired},
	LifecycleQuarantined: {LifecycleWatch, LifecycleRetired},
	LifecycleRetired:     {}, // 终态
}

// CanTransitionLifecycle 判断生命周期状态机 from → to 是否合法
// （from 零值 "" 表示初始状态）。
func CanTransitionLifecycle(from, to LifecycleState) bool {
	for _, t := range lifecycleTransitions[from] {
		if t == to {
			return true
		}
	}
	return false
}

// IsLifecycleTerminal 报告状态是否为终态（RETIRED 无任何回边）。
func IsLifecycleTerminal(s LifecycleState) bool {
	return s == LifecycleRetired
}

// lifecycleGateCertRequiredTargets 转入需门证书的目标状态集合
// （health.py GATE_CERT_REQUIRED_STATES）。
var lifecycleGateCertRequiredTargets = map[LifecycleState]bool{
	LifecycleQuarantined: true,
	LifecycleRetired:     true,
}

// LifecycleRequiresGateCert 报告前移到 to 是否需门证书
// （→QUARANTINED / →RETIRED 必填；ACTIVE ↔ WATCH 自动转换无需证书）。
func LifecycleRequiresGateCert(to LifecycleState) bool {
	return lifecycleGateCertRequiredTargets[to]
}

// IsInActivePool 报告状态是否属于活跃池（查询活跃池时排除 QUARANTINED
// 与 RETIRED；health.py ACTIVE_POOL_STATES）。
func IsInActivePool(s LifecycleState) bool {
	return s == LifecycleActive || s == LifecycleWatch
}
