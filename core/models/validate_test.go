// validate_test.go 构造期校验的验收测试：0002/0005/0013/0018 CHECK 约束的
// 等价实现逐条正负例覆盖 + 应用层语义（published 需门证书、quarantined 需
// 渲染快照、生命周期门证书）正负例。
//
// 每个 CHECK 约束至少 1 正例（放行）1 负例（拒绝），错误信息含约束名，
// 便于与 DB 报错对照。
package models

import (
	"strings"
	"testing"
	"time"
)

// ────────────────────────────────────────────────────────────────────
// 构造辅助
// ────────────────────────────────────────────────────────────────────

func strPtr(s string) *string { return &s }
func timePtr(t time.Time) *time.Time {
	tm := t
	return &tm
}

var testNow = time.Date(2026, 8, 30, 0, 0, 0, 0, time.UTC)

func validItemVersion() ItemVersion {
	return ItemVersion{
		ItemVersionID:  "sha256:iv",
		ItemID:         "item-1",
		Status:         VersionDraft,
		Objective:      map[string]any{"kp_set": []any{}},
		InteractionRef: map[string]any{"interaction_id": "single_choice"},
		Content:        map[string]any{"blocks": []any{}},
		ScoringRef:     map[string]any{"scorer_id": "exact_match"},
		ErrorBindings:  []any{},
		Lineage:        map[string]any{"tier": "A"},
	}
}

// ────────────────────────────────────────────────────────────────────
// ItemVersion
// ────────────────────────────────────────────────────────────────────

func TestValidateItemVersionPositive(t *testing.T) {
	// draft：六大块齐即可，无需快照/证书
	if err := ValidateItemVersion(validItemVersion()); err != nil {
		t.Fatalf("合法 draft 被拒绝: %v", err)
	}
	// quarantined：需渲染快照，证书可空
	q := validItemVersion()
	q.Status = VersionQuarantined
	q.RenderedSnapshot = map[string]any{"html": "<p/>"}
	if err := ValidateItemVersion(q); err != nil {
		t.Fatalf("合法 quarantined 被拒绝: %v", err)
	}
	// published：快照 + 门证书 + published_at 齐备
	p := q
	p.Status = VersionPublished
	p.GateCertificateID = strPtr("cert-1")
	p.PublishedAt = timePtr(testNow)
	if err := ValidateItemVersion(p); err != nil {
		t.Fatalf("合法 published 被拒绝: %v", err)
	}
	// retired：快照齐备即可（retired_at 可空）
	r := p
	r.Status = VersionRetired
	if err := ValidateItemVersion(r); err != nil {
		t.Fatalf("合法 retired 被拒绝: %v", err)
	}
}

func TestValidateItemVersionQuarantineRequiresRendered(t *testing.T) {
	// ck_iv_quarantine_requires_rendered：status = 'draft' OR rendered_snapshot IS NOT NULL
	for _, status := range []ItemVersionStatus{VersionQuarantined, VersionPublished, VersionRetired} {
		v := validItemVersion()
		v.Status = status
		err := ValidateItemVersion(v)
		if err == nil {
			t.Fatalf("status=%s 缺 rendered_snapshot 未被拒绝", status)
		}
		if !strings.Contains(err.Error(), "ck_iv_quarantine_requires_rendered") {
			t.Fatalf("错误信息缺约束名: %v", err)
		}
	}
}

func TestValidateItemVersionPublishedRequiresGateCert(t *testing.T) {
	// 应用层：status=published 必须持门证书（D2）
	v := validItemVersion()
	v.Status = VersionPublished
	v.RenderedSnapshot = map[string]any{"html": "<p/>"}
	if err := ValidateItemVersion(v); err == nil {
		t.Fatal("published 缺门证书未被拒绝")
	}
	// ck_iv_published_requires_gate_cert（DDL 字面）：published_at 非空必伴随证书
	w := validItemVersion()
	w.PublishedAt = timePtr(testNow)
	w.GateCertificateID = nil
	err := ValidateItemVersion(w)
	if err == nil {
		t.Fatal("published_at 非空但缺证书未被拒绝")
	}
	if !strings.Contains(err.Error(), "ck_iv_published_requires_gate_cert") {
		t.Fatalf("错误信息缺约束名: %v", err)
	}
	// 证书为空串等同 NULL（fail-closed）
	x := validItemVersion()
	x.PublishedAt = timePtr(testNow)
	x.GateCertificateID = strPtr("")
	if err := ValidateItemVersion(x); err == nil {
		t.Fatal("空串门证书未被拒绝")
	}
}

func TestValidateItemVersionNotNullColumns(t *testing.T) {
	// 六大块 JSONB NOT NULL 逐块负例
	for _, col := range []string{"objective", "interaction_ref", "content", "scoring_ref", "error_bindings", "lineage"} {
		v := validItemVersion()
		switch col {
		case "objective":
			v.Objective = nil
		case "interaction_ref":
			v.InteractionRef = nil
		case "content":
			v.Content = nil
		case "scoring_ref":
			v.ScoringRef = nil
		case "error_bindings":
			v.ErrorBindings = nil
		case "lineage":
			v.Lineage = nil
		}
		if err := ValidateItemVersion(v); err == nil {
			t.Fatalf("%s = NULL 未被拒绝（JSONB NOT NULL）", col)
		} else if !strings.Contains(err.Error(), col) {
			t.Fatalf("错误信息缺列名 %s: %v", col, err)
		}
	}
	// 主键/外键 NOT NULL
	v := validItemVersion()
	v.ItemVersionID = ""
	if err := ValidateItemVersion(v); err == nil {
		t.Fatal("item_version_id 空串未被拒绝")
	}
	v = validItemVersion()
	v.ItemID = ""
	if err := ValidateItemVersion(v); err == nil {
		t.Fatal("item_id 空串未被拒绝")
	}
	// 非法 status
	v = validItemVersion()
	v.Status = ItemVersionStatus("PUBLISHED")
	if err := ValidateItemVersion(v); err == nil {
		t.Fatal("非法 status 未被拒绝")
	}
}

// ────────────────────────────────────────────────────────────────────
// MaterialVersion / CorpusVersion：ck_mv / ck_cv 同族约束
// ────────────────────────────────────────────────────────────────────

func validMaterialVersion() MaterialVersion {
	return MaterialVersion{
		MaterialVersionID: "sha256:mv",
		MaterialID:        "mat-1",
		ContentRef:        "minio:materials/sha256:abc",
		LicenseID:         "lic-1",
		Status:            VersionDraft,
		Lineage:           map[string]any{"tier": "C"},
	}
}

func TestValidateMaterialVersion(t *testing.T) {
	if err := ValidateMaterialVersion(validMaterialVersion()); err != nil {
		t.Fatalf("合法 draft 素材版本被拒绝: %v", err)
	}
	// published 需门证书
	p := validMaterialVersion()
	p.Status = VersionPublished
	p.GateCertificateID = strPtr("cert-1")
	p.PublishedAt = timePtr(testNow)
	if err := ValidateMaterialVersion(p); err != nil {
		t.Fatalf("合法 published 素材版本被拒绝: %v", err)
	}
	q := validMaterialVersion()
	q.Status = VersionPublished
	if err := ValidateMaterialVersion(q); err == nil {
		t.Fatal("published 素材版本缺门证书未被拒绝")
	}
	// ck_mv_published_requires_gate_cert 字面
	r := validMaterialVersion()
	r.PublishedAt = timePtr(testNow)
	err := ValidateMaterialVersion(r)
	if err == nil {
		t.Fatal("published_at 非空但缺证书未被拒绝")
	}
	if !strings.Contains(err.Error(), "ck_mv_published_requires_gate_cert") {
		t.Fatalf("错误信息缺约束名: %v", err)
	}
	// NOT NULL 列
	for _, mutate := range []func(*MaterialVersion){
		func(v *MaterialVersion) { v.MaterialID = "" },
		func(v *MaterialVersion) { v.ContentRef = "" },
		func(v *MaterialVersion) { v.LicenseID = "" },
		func(v *MaterialVersion) { v.Lineage = nil },
	} {
		v := validMaterialVersion()
		mutate(&v)
		if err := ValidateMaterialVersion(v); err == nil {
			t.Fatalf("NOT NULL 列为空的素材版本未被拒绝: %+v", v)
		}
	}
}

func validCorpusVersion() CorpusVersion {
	return CorpusVersion{
		VersionID:  "sha256:cv",
		AssetID:    "asset-1",
		ContentRef: "minio:corpus/sha256:def",
		LicenseID:  "lic-1",
		Lineage:    map[string]any{"tier": "B"},
		Status:     VersionDraft,
	}
}

func TestValidateCorpusVersion(t *testing.T) {
	if err := ValidateCorpusVersion(validCorpusVersion()); err != nil {
		t.Fatalf("合法 draft 语料版本被拒绝: %v", err)
	}
	// published 需门证书（0005 补的 ck_cv）
	p := validCorpusVersion()
	p.Status = VersionPublished
	p.GateCertificateID = strPtr("cert-1")
	p.PublishedAt = timePtr(testNow)
	if err := ValidateCorpusVersion(p); err != nil {
		t.Fatalf("合法 published 语料版本被拒绝: %v", err)
	}
	q := validCorpusVersion()
	q.Status = VersionPublished
	if err := ValidateCorpusVersion(q); err == nil {
		t.Fatal("published 语料版本缺门证书未被拒绝")
	}
	// ck_cv_published_requires_gate_cert 字面
	r := validCorpusVersion()
	r.PublishedAt = timePtr(testNow)
	err := ValidateCorpusVersion(r)
	if err == nil {
		t.Fatal("published_at 非空但缺证书未被拒绝")
	}
	if !strings.Contains(err.Error(), "ck_cv_published_requires_gate_cert") {
		t.Fatalf("错误信息缺约束名: %v", err)
	}
}

// ────────────────────────────────────────────────────────────────────
// ItemTemplateVersion / ItemGroup：枚举域 + ck_ig_max_six_items
// ────────────────────────────────────────────────────────────────────

func TestValidateItemTemplateVersion(t *testing.T) {
	v := ItemTemplateVersion{
		TemplateVersionID: "sha256:tv",
		TemplateID:        "tpl-1",
		DSLVersion:        "dsl-v1",
		Spec:              map[string]any{"slots": []any{}},
		Status:            TemplateDraft,
	}
	if err := ValidateItemTemplateVersion(v); err != nil {
		t.Fatalf("合法母题版本被拒绝: %v", err)
	}
	// 母题无 quarantined 态
	q := v
	q.Status = TemplateVersionStatus("quarantined")
	if err := ValidateItemTemplateVersion(q); err == nil {
		t.Fatal("母题版本 quarantined 态未被拒绝（母题不直接过门）")
	}
	s := v
	s.Spec = nil
	if err := ValidateItemTemplateVersion(s); err == nil {
		t.Fatal("spec = NULL 未被拒绝")
	}
}

func ids(n int) []string {
	out := make([]string, n)
	for i := range out {
		out[i] = "sha256:iv-" + string(rune('a'+i))
	}
	return out
}

func TestValidateItemGroupSixItemsCap(t *testing.T) {
	// 边界内：6 题放行（ck_ig_max_six_items：array_length ≤ 6）
	g := ItemGroup{
		ItemGroupID:       "ig-1",
		MaterialVersionID: strPtr("sha256:mv"),
		ItemVersionIDs:    ids(MaxItemGroupItems),
		Ordered:           true,
		Testlet:           true,
	}
	if err := ValidateItemGroup(g); err != nil {
		t.Fatalf("6 题题组被拒绝: %v", err)
	}
	// 边界外：7 题拒绝
	g.ItemVersionIDs = ids(MaxItemGroupItems + 1)
	err := ValidateItemGroup(g)
	if err == nil {
		t.Fatal("7 题题组未被拒绝（R-Z-06）")
	}
	if !strings.Contains(err.Error(), "ck_ig_max_six_items") {
		t.Fatalf("错误信息缺约束名: %v", err)
	}
	// 空数组与 DDL 同构放行（array_length 为 NULL，CHECK 不触发）
	e := ItemGroup{ItemGroupID: "ig-2", ItemVersionIDs: []string{}}
	if err := ValidateItemGroup(e); err != nil {
		t.Fatalf("空题组被拒绝（与 DDL 语义不符）: %v", err)
	}
	// nil 数组 = NULL 拒绝
	n := ItemGroup{ItemGroupID: "ig-3", ItemVersionIDs: nil}
	if err := ValidateItemGroup(n); err == nil {
		t.Fatal("item_version_ids = NULL 未被拒绝")
	}
}

// ────────────────────────────────────────────────────────────────────
// ItemParam：0013 三条 CHECK
// ────────────────────────────────────────────────────────────────────

func validItemParam() ItemParam {
	return ItemParam{
		ParamID:       "param-1",
		ItemVersionID: "sha256:iv",
		PurposeScope:  "practice",
		Source:        "prior_rule",
		Params:        map[string]any{"difficulty": "0"},
		SampleSize:    100,
		MethodVersion: "m1",
		AsOf:          testNow,
	}
}

func TestValidateItemParamChecks(t *testing.T) {
	if err := ValidateItemParam(validItemParam()); err != nil {
		t.Fatalf("合法标定行被拒绝: %v", err)
	}
	// ck_item_param_purpose_scope_domain
	v := validItemParam()
	v.PurposeScope = "mixed"
	err := ValidateItemParam(v)
	if err == nil || !strings.Contains(err.Error(), "ck_item_param_purpose_scope_domain") {
		t.Fatalf("purpose_scope 非法域未被拒绝（含约束名）: %v", err)
	}
	// ck_item_param_source_domain（正则 ^(...|measured_.+)$）
	v = validItemParam()
	v.Source = "measured_irt_2pl"
	if err := ValidateItemParam(v); err != nil {
		t.Fatalf("measured_irt_2pl 被拒绝: %v", err)
	}
	v = validItemParam()
	v.Source = "measured_"
	err = ValidateItemParam(v)
	if err == nil || !strings.Contains(err.Error(), "ck_item_param_source_domain") {
		t.Fatalf("measured_（空后缀）未被拒绝（含约束名）: %v", err)
	}
	// ck_item_param_sample_size_nonneg
	v = validItemParam()
	v.SampleSize = -1
	err = ValidateItemParam(v)
	if err == nil || !strings.Contains(err.Error(), "ck_item_param_sample_size_nonneg") {
		t.Fatalf("负 sample_size 未被拒绝（含约束名）: %v", err)
	}
	// params NOT NULL
	v = validItemParam()
	v.Params = nil
	if err := ValidateItemParam(v); err == nil {
		t.Fatal("params = NULL 未被拒绝")
	}
}

// ────────────────────────────────────────────────────────────────────
// ItemLifecycleTransition：ck_ilt_health_score_domain + 状态机/门证书
// ────────────────────────────────────────────────────────────────────

func TestValidateLifecycleTransition(t *testing.T) {
	// 初始 → ACTIVE（无需证书）
	init := ItemLifecycleTransition{
		TransitionID: "t-1",
		ItemID:       "item-1",
		FromState:    "",
		ToState:      LifecycleActive,
	}
	if err := ValidateLifecycleTransition(init); err != nil {
		t.Fatalf("初始 → ACTIVE 被拒绝: %v", err)
	}
	// ACTIVE ↔ WATCH 自动转换（无需证书）
	aw := init
	aw.TransitionID = "t-2"
	aw.FromState = LifecycleActive
	aw.ToState = LifecycleWatch
	if err := ValidateLifecycleTransition(aw); err != nil {
		t.Fatalf("ACTIVE → WATCH 被拒绝: %v", err)
	}
	// WATCH → QUARANTINED 需门证书（负例 + 正例）
	wq := init
	wq.TransitionID = "t-3"
	wq.FromState = LifecycleWatch
	wq.ToState = LifecycleQuarantined
	if err := ValidateLifecycleTransition(wq); err == nil {
		t.Fatal("WATCH → QUARANTINED 缺门证书未被拒绝")
	}
	wq.GateCertificateID = strPtr("cert-1")
	if err := ValidateLifecycleTransition(wq); err != nil {
		t.Fatalf("WATCH → QUARANTINED 持证书被拒绝: %v", err)
	}
	// ACTIVE → RETIRED 需门证书
	ar := init
	ar.TransitionID = "t-4"
	ar.FromState = LifecycleActive
	ar.ToState = LifecycleRetired
	if err := ValidateLifecycleTransition(ar); err == nil {
		t.Fatal("ACTIVE → RETIRED 缺门证书未被拒绝")
	}
	// RETIRED 终态回边拒绝
	re := init
	re.TransitionID = "t-5"
	re.FromState = LifecycleRetired
	re.ToState = LifecycleWatch
	err := ValidateLifecycleTransition(re)
	if err == nil {
		t.Fatal("RETIRED → WATCH 未被拒绝（终态回边）")
	}
	if !strings.Contains(err.Error(), "终态") {
		t.Fatalf("终态错误信息不明: %v", err)
	}
	// 非法转换：QUARANTINED → ACTIVE
	qa := init
	qa.TransitionID = "t-6"
	qa.FromState = LifecycleQuarantined
	qa.ToState = LifecycleActive
	if err := ValidateLifecycleTransition(qa); err == nil {
		t.Fatal("QUARANTINED → ACTIVE 未被拒绝（§4.7 状态机）")
	}
	// to_state = NULL 拒绝
	nt := init
	nt.TransitionID = "t-7"
	nt.ToState = ""
	if err := ValidateLifecycleTransition(nt); err == nil {
		t.Fatal("to_state = NULL 未被拒绝")
	}
	// 非法状态值
	is := init
	is.TransitionID = "t-8"
	is.FromState = LifecycleState("SLEEPING")
	if err := ValidateLifecycleTransition(is); err == nil {
		t.Fatal("非法 from_state 未被拒绝")
	}
}

func TestValidateLifecycleHealthScoreDomain(t *testing.T) {
	// ck_ilt_health_score_domain：health_score IS NULL OR [0,1]
	base := ItemLifecycleTransition{
		TransitionID:      "t-9",
		ItemID:            "item-1",
		FromState:         LifecycleWatch,
		ToState:           LifecycleQuarantined,
		GateCertificateID: strPtr("cert-1"),
	}
	// NULL 放行
	if err := ValidateLifecycleTransition(base); err != nil {
		t.Fatalf("health_score = NULL 被拒绝: %v", err)
	}
	// 边界内：0 与 1 放行（含三位小数快照）
	for _, ok := range []float64{0, 0.001, 0.5, 0.999, 1} {
		v := base
		v.HealthScore = &ok
		if err := ValidateLifecycleTransition(v); err != nil {
			t.Fatalf("health_score=%v 被拒绝: %v", ok, err)
		}
	}
	// 边界外：负数与超 1 拒绝
	for _, bad := range []float64{-0.001, 1.001, 1.5} {
		v := base
		v.HealthScore = &bad
		err := ValidateLifecycleTransition(v)
		if err == nil {
			t.Fatalf("health_score=%v 未被拒绝", bad)
		}
		if !strings.Contains(err.Error(), "ck_ilt_health_score_domain") {
			t.Fatalf("错误信息缺约束名: %v", err)
		}
	}
}

// ────────────────────────────────────────────────────────────────────
// RequireApprovedLicense（R-Q-18）
// ────────────────────────────────────────────────────────────────────

func TestRequireApprovedLicense(t *testing.T) {
	if err := RequireApprovedLicense(LicenseApproved); err != nil {
		t.Fatalf("approved 被拒绝: %v", err)
	}
	for _, d := range []LicenseDecision{LicenseRejected, LicenseExpired} {
		err := RequireApprovedLicense(d)
		if err == nil {
			t.Fatalf("decision=%s 未被拒绝（R-Q-18）", d)
		}
		if !strings.Contains(err.Error(), "R-Q-18") {
			t.Fatalf("错误信息缺规则号: %v", err)
		}
	}
	// 非法 decision 值同样拒绝
	if err := RequireApprovedLicense(LicenseDecision("pending")); err == nil {
		t.Fatal("非法 decision 未被拒绝")
	}
}
