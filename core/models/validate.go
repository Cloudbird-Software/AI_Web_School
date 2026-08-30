// validate.go 承载核心实体构造期校验（Python 冻结基准 src/core/models/*.py
// 与 db/migrations/*.sql 中 CHECK 约束的 Go 等价实现；不接 DB，纯校验面）。
//
// 校验分两层，均 fail-closed（首个失败即返回错误）：
//  1. CHECK 等价层：逐条复刻迁移 DDL 的 CHECK 约束（约束名保留在错误
//     信息中，便于与 DB 报错对照）：
//     - ck_iv_published_requires_gate_cert（0002）：published_at 非空必伴随
//     gate_certificate_id 非空；
//     - ck_iv_quarantine_requires_rendered（0002）：status 非 draft 必有
//     rendered_snapshot；
//     - ck_mv_published_requires_gate_cert（0002）/ ck_cv_published_requires_gate_cert
//     （0005）：同 ck_iv 口径；
//     - ck_ig_max_six_items（0002）：题组 ≤ 6 题（R-Z-06）；
//     - ck_ilt_health_score_domain（0018）：health_score 为 NULL 或 [0,1]；
//     - ck_item_param_purpose_scope_domain / ck_item_param_source_domain /
//     ck_item_param_sample_size_nonneg（0013）。
//  2. 应用层语义层：冻结实现中由服务层强制、DB 不设 CHECK 的规则
//     （writer.py / publication.py / health.py）：
//     - status=published 必须持门证书（签发一跳必传，D2 门证书唯一真源）；
//     - published 必有 published_at（writer.py 前移时同时写入）；
//     - 生命周期转入 QUARANTINED / RETIRED 必须持门证书；
//     - R-Q-18：素材/语料库版本引用的许可必须是 approved。
package models

import (
	"fmt"
	"regexp"
	"time"
)

// itemParamSourcePattern ck_item_param_source_domain 的正则
// （source ~ '^(prior_rule|prior_expert|measured_.+)$'，0013 DDL 原文）。
var itemParamSourcePattern = regexp.MustCompile(`^(prior_rule|prior_expert|measured_.+)$`)

// errNilJSONB / errEmptyID 公共校验错误构造器（保持错误信息含列语义）。
func errNilJSONB(col string) error {
	return fmt.Errorf("models: %s 不得为空（JSONB NOT NULL）", col)
}

func errEmptyID(col string) error {
	return fmt.Errorf("models: %s 不得为空（TEXT NOT NULL）", col)
}

// checkPublishedGate 复刻 ck_*_published_requires_gate_cert 族：
// published_at 非空必伴随 gate_certificate_id 非空。
func checkPublishedGate(publishedAt *time.Time, gateCertID *string, constraint string) error {
	if publishedAt != nil && (gateCertID == nil || *gateCertID == "") {
		return fmt.Errorf("models: %s 违反（published_at 非空必须持 gate_certificate_id，D2）", constraint)
	}
	return nil
}

// ────────────────────────────────────────────────────────────────────
// ItemVersion（0002：六大块 NOT NULL + 两条约束）
// ────────────────────────────────────────────────────────────────────

// ValidateItemVersion 校验 item_version 行的构造期约束。
func ValidateItemVersion(v ItemVersion) error {
	if v.ItemVersionID == "" {
		return errEmptyID("item_version_id")
	}
	if v.ItemID == "" {
		return errEmptyID("item_id")
	}
	if _, err := ParseVersionStatus(string(v.Status)); err != nil {
		return err
	}
	// 六大块 NOT NULL（按列序检查，保证错误信息确定）
	for _, blk := range []struct {
		col string
		val map[string]any
	}{
		{"objective", v.Objective},
		{"interaction_ref", v.InteractionRef},
		{"content", v.Content},
		{"scoring_ref", v.ScoringRef},
		{"lineage", v.Lineage},
	} {
		if blk.val == nil {
			return errNilJSONB(blk.col)
		}
	}
	if v.ErrorBindings == nil {
		return errNilJSONB("error_bindings")
	}
	// ck_iv_quarantine_requires_rendered：status = 'draft' OR rendered_snapshot IS NOT NULL
	if v.Status != VersionDraft && v.RenderedSnapshot == nil {
		return fmt.Errorf(
			"models: ck_iv_quarantine_requires_rendered 违反（status=%s 非 draft 必须有 rendered_snapshot，契约 §2.2）",
			v.Status)
	}
	// ck_iv_published_requires_gate_cert（DDL 字面）
	if err := checkPublishedGate(v.PublishedAt, v.GateCertificateID, "ck_iv_published_requires_gate_cert"); err != nil {
		return err
	}
	// 应用层：published 状态本身必须持门证书（publication.py 签发必传；D2）
	if v.Status == VersionPublished && (v.GateCertificateID == nil || *v.GateCertificateID == "") {
		return fmt.Errorf("models: status=published 必须持 gate_certificate_id（D2 门证书唯一真源）")
	}
	return nil
}

// ────────────────────────────────────────────────────────────────────
// MaterialVersion（0002：ck_mv_published_requires_gate_cert）
// ────────────────────────────────────────────────────────────────────

// ValidateMaterialVersion 校验 material_version 行的构造期约束
// （lineage 复用 §2.2.2 结构；license 合法性另见 RequireApprovedLicense）。
func ValidateMaterialVersion(v MaterialVersion) error {
	if v.MaterialVersionID == "" {
		return errEmptyID("material_version_id")
	}
	if v.MaterialID == "" {
		return errEmptyID("material_id")
	}
	if v.ContentRef == "" {
		return errEmptyID("content_ref")
	}
	if v.LicenseID == "" {
		return errEmptyID("license_id")
	}
	if _, err := ParseVersionStatus(string(v.Status)); err != nil {
		return err
	}
	if v.Lineage == nil {
		return errNilJSONB("lineage")
	}
	if err := checkPublishedGate(v.PublishedAt, v.GateCertificateID, "ck_mv_published_requires_gate_cert"); err != nil {
		return err
	}
	if v.Status == VersionPublished && (v.GateCertificateID == nil || *v.GateCertificateID == "") {
		return fmt.Errorf("models: status=published 必须持 gate_certificate_id（D2 门证书唯一真源）")
	}
	return nil
}

// ────────────────────────────────────────────────────────────────────
// CorpusVersion（0002 建表 + 0005 补门字段）
// ────────────────────────────────────────────────────────────────────

// ValidateCorpusVersion 校验 corpus_version 行的构造期约束。
func ValidateCorpusVersion(v CorpusVersion) error {
	if v.VersionID == "" {
		return errEmptyID("version_id")
	}
	if v.AssetID == "" {
		return errEmptyID("asset_id")
	}
	if v.ContentRef == "" {
		return errEmptyID("content_ref")
	}
	if v.LicenseID == "" {
		return errEmptyID("license_id")
	}
	if _, err := ParseVersionStatus(string(v.Status)); err != nil {
		return err
	}
	if v.Lineage == nil {
		return errNilJSONB("lineage")
	}
	if err := checkPublishedGate(v.PublishedAt, v.GateCertificateID, "ck_cv_published_requires_gate_cert"); err != nil {
		return err
	}
	if v.Status == VersionPublished && (v.GateCertificateID == nil || *v.GateCertificateID == "") {
		return fmt.Errorf("models: status=published 必须持 gate_certificate_id（D2 门证书唯一真源）")
	}
	return nil
}

// ────────────────────────────────────────────────────────────────────
// ItemTemplateVersion（0002：draft/published/retired，无 quarantined）
// ────────────────────────────────────────────────────────────────────

// ValidateItemTemplateVersion 校验母题版本行的构造期约束。
func ValidateItemTemplateVersion(v ItemTemplateVersion) error {
	if v.TemplateVersionID == "" {
		return errEmptyID("template_version_id")
	}
	if v.TemplateID == "" {
		return errEmptyID("template_id")
	}
	if v.DSLVersion == "" {
		return errEmptyID("dsl_version")
	}
	if v.Spec == nil {
		return errNilJSONB("spec")
	}
	if _, err := ParseTemplateStatus(string(v.Status)); err != nil {
		return err
	}
	return nil
}

// ────────────────────────────────────────────────────────────────────
// ItemGroup（0002：ck_ig_max_six_items，R-Z-06）
// ────────────────────────────────────────────────────────────────────

// MaxItemGroupItems 题组容量上限（ck_ig_max_six_items：array_length ≤ 6）。
const MaxItemGroupItems = 6

// ValidateItemGroup 校验题组行的构造期约束。空数组与 DDL 同构放行
// （array_length(ARRAY[]::text[], 1) 为 NULL，CHECK 不触发）。
func ValidateItemGroup(g ItemGroup) error {
	if g.ItemGroupID == "" {
		return errEmptyID("item_group_id")
	}
	if g.ItemVersionIDs == nil {
		return fmt.Errorf("models: item_version_ids 不得为 NULL（TEXT[] NOT NULL）")
	}
	if len(g.ItemVersionIDs) > MaxItemGroupItems {
		return fmt.Errorf(
			"models: ck_ig_max_six_items 违反（题组 %d 题 > 上限 %d，R-Z-06）",
			len(g.ItemVersionIDs), MaxItemGroupItems)
	}
	return nil
}

// ────────────────────────────────────────────────────────────────────
// ItemParam（0013：三条 CHECK）
// ────────────────────────────────────────────────────────────────────

// ValidateItemParam 校验参数标定行的构造期约束（D5 场景域 / D6 来源域）。
func ValidateItemParam(p ItemParam) error {
	if p.ParamID == "" {
		return errEmptyID("param_id")
	}
	if p.ItemVersionID == "" {
		return errEmptyID("item_version_id")
	}
	switch p.PurposeScope {
	case "practice", "diagnosis", "measurement":
	default:
		return fmt.Errorf(
			"models: ck_item_param_purpose_scope_domain 违反（purpose_scope=%q 不在 practice/diagnosis/measurement）",
			p.PurposeScope)
	}
	if !itemParamSourcePattern.MatchString(p.Source) {
		return fmt.Errorf(
			"models: ck_item_param_source_domain 违反（source=%q 不匹配 ^(prior_rule|prior_expert|measured_.+)$）",
			p.Source)
	}
	if p.SampleSize < 0 {
		return fmt.Errorf("models: ck_item_param_sample_size_nonneg 违反（sample_size=%d < 0）", p.SampleSize)
	}
	if p.Params == nil {
		return errNilJSONB("params")
	}
	return nil
}

// ────────────────────────────────────────────────────────────────────
// ItemLifecycleTransition（0018：ck_ilt_health_score_domain + 应用层状态机）
// ────────────────────────────────────────────────────────────────────

// ValidateLifecycleTransition 校验生命周期 transition 行的构造期约束：
// DDL CHECK（health_score 域）+ health.py 状态机与门证书的应用层前置校验。
func ValidateLifecycleTransition(t ItemLifecycleTransition) error {
	if t.TransitionID == "" {
		return errEmptyID("transition_id")
	}
	if t.ItemID == "" {
		return errEmptyID("item_id")
	}
	if _, err := ParseLifecycleState(string(t.FromState)); err != nil {
		return err
	}
	if _, err := ParseLifecycleState(string(t.ToState)); err != nil {
		return err
	}
	if t.ToState == "" {
		return fmt.Errorf("models: to_state 不得为 NULL（item_lifecycle_state_enum NOT NULL）")
	}
	// health.py：非法转换 / 终态回边拒绝
	if !CanTransitionLifecycle(t.FromState, t.ToState) {
		if IsLifecycleTerminal(t.FromState) {
			return fmt.Errorf(
				"models: %s 为终态，禁止任何转换（→ %s）", t.FromState, t.ToState)
		}
		return fmt.Errorf(
			"models: 非法生命周期转换 %q → %q（§4.7 状态机，0018）", t.FromState, t.ToState)
	}
	// health.py：转入 QUARANTINED / RETIRED 需门证书
	if LifecycleRequiresGateCert(t.ToState) && (t.GateCertificateID == nil || *t.GateCertificateID == "") {
		return fmt.Errorf(
			"models: 转入 %s 需门证书（gate_certificate_id 必填）", t.ToState)
	}
	// ck_ilt_health_score_domain：health_score IS NULL OR (0 <= health_score <= 1)
	if t.HealthScore != nil && (*t.HealthScore < 0 || *t.HealthScore > 1) {
		return fmt.Errorf(
			"models: ck_ilt_health_score_domain 违反（health_score=%v 不在 [0,1]）", *t.HealthScore)
	}
	return nil
}

// ────────────────────────────────────────────────────────────────────
// 许可决策（R-Q-18：来源不合规无法入库）
// ────────────────────────────────────────────────────────────────────

// RequireApprovedLicense 校验许可可被 material_version / corpus_version
// 引用（R-Q-18：仅 approved；decision 非法值同样拒绝）。
func RequireApprovedLicense(d LicenseDecision) error {
	if _, err := ParseLicenseDecision(string(d)); err != nil {
		return err
	}
	if d != LicenseApproved {
		return fmt.Errorf(
			"models: R-Q-18 违反（license decision=%s 非 approved，来源不合规无法入库）", d)
	}
	return nil
}
