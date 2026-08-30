package api

// GO-RW-001 内容只读四端点的 HTTP 行为测试：ContentQueries 接口注入 +
// Memory fake（in-memory 假实现，无 DB）。断言面：
//   - 正例：staff/ops 200 契约 JSON（键面恰好契约字段集，字段最小化）；
//   - 角色面：注入查询面后学生/service 仍 403（接线不松认证盾）；
//   - 无行 → 404 not_found；驱动故障 → 500 internal（单字段脱敏，无内部细节）；
//   - 查询面未注入（nil）→ 保持 501 占位（装配语义，与 authz_test 互证）。
//
// 全部 httptest.ResponseRecorder 直驱 mux：无 goroutine，兼容 TestMain
// goleak 与 -race。

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"strings"
	"testing"
	"time"

	"github.com/Cloudbird-Software/AI_Web_School/api/middleware"
	"github.com/Cloudbird-Software/AI_Web_School/core/auth"
	"github.com/Cloudbird-Software/AI_Web_School/core/content"
)

// errFakeQuery 驱动故障注入源（与业务哨兵异源）.
var errFakeQuery = errors.New("fakeq: 注入的取证故障")

// memoryContentQueries 是 ContentQueries 的内存假实现：map 命中即正例、
// 未命中即对应实体的无行哨兵；failID 命中则返回注入的驱动故障.
type memoryContentQueries struct {
	items        map[string]*content.ItemDetail
	versions     map[string]*content.ItemVersionView
	templates    map[string]*content.TemplateDetail
	certificates map[string]*content.GateCertificateDetail
	failID       string
}

func (m *memoryContentQueries) GetItem(_ context.Context, id string) (*content.ItemDetail, error) {
	if id == m.failID {
		return nil, errFakeQuery
	}
	if v, ok := m.items[id]; ok {
		return v, nil
	}
	return nil, content.ErrUnknownItem
}

func (m *memoryContentQueries) GetItemVersion(_ context.Context, id string) (*content.ItemVersionView, error) {
	if id == m.failID {
		return nil, errFakeQuery
	}
	if v, ok := m.versions[id]; ok {
		return v, nil
	}
	return nil, content.ErrUnknownItemVersion
}

func (m *memoryContentQueries) GetTemplate(_ context.Context, id string) (*content.TemplateDetail, error) {
	if id == m.failID {
		return nil, errFakeQuery
	}
	if v, ok := m.templates[id]; ok {
		return v, nil
	}
	return nil, content.ErrUnknownTemplate
}

func (m *memoryContentQueries) GetGateCertificate(_ context.Context, id string) (*content.GateCertificateDetail, error) {
	if id == m.failID {
		return nil, errFakeQuery
	}
	if v, ok := m.certificates[id]; ok {
		return v, nil
	}
	return nil, content.ErrUnknownGateCertificate
}

// withQueries 在标准 fixture 上注入查询面重装 router（授权账沿用 fixture）.
func (f *apiFixture) withQueries(q ContentQueries) {
	f.app = NewRouterWithQueries(f.signer, f.consent, q)
}

var (
	qTestTime  = time.Date(2026, 8, 30, 8, 0, 0, 0, time.UTC)
	qTestTime2 = time.Date(2026, 8, 30, 9, 0, 0, 0, time.UTC)
)

func queryStr(s string) *string { return &s }

func sampleItemDetail() *content.ItemDetail {
	return &content.ItemDetail{
		ItemID: "item_1", PackID: "pack_math", Tier: "B",
		TemplateVersionID: queryStr("tvd_1"), CurrentVersionID: queryStr("iv_1"),
		CreatedAt: &qTestTime,
		CurrentVersion: &content.ItemVersionView{
			ItemVersionID: "iv_1", ItemID: "item_1", Status: "published",
			Objective: json.RawMessage(`{"kp":[]}`), InteractionRef: json.RawMessage(`{"id":"single_choice"}`),
			Content: json.RawMessage(`{"blocks":[]}`), ScoringRef: json.RawMessage(`{"scorer":"exact_match"}`),
			ErrorBindings: json.RawMessage(`{}`), Lineage: json.RawMessage(`{"tier":"B"}`),
			RenderedSnapshot: json.RawMessage(`{"html":"<p/>"}`), GateCertificateID: queryStr("cert_1"),
			PublishedAt: &qTestTime2, CreatedAt: &qTestTime,
		},
	}
}

// TestContentReadQueries_Found 契约正例：staff 取证成功 → 200 契约 JSON。
// 键面断言对齐冻结契约 additionalProperties:false 的四个响应 schema——
// 多一少一键都是契约漂移.
func TestContentReadQueries_Found(t *testing.T) {
	f := newAPIFixture(t)
	seed := &memoryContentQueries{
		items:     map[string]*content.ItemDetail{"item_1": sampleItemDetail()},
		versions:  map[string]*content.ItemVersionView{"iv_1": sampleItemDetail().CurrentVersion},
		templates: map[string]*content.TemplateDetail{"tpl_1": {TemplateID: "tpl_1", PackID: "pack_e", CurrentVersionID: queryStr("tv_1"), CreatedAt: &qTestTime, CurrentVersion: &content.TemplateVersionView{TemplateVersionID: "tv_1", TemplateID: "tpl_1", DslVersion: "dsl-6", Spec: json.RawMessage(`{"blocks":[]}`), Status: "published", CreatedAt: &qTestTime}}},
		certificates: map[string]*content.GateCertificateDetail{"cert_1": {
			CertID: "cert_1", ArtifactRef: "iv_1", CertType: "publish", PolicyVersion: "pv-2026.1",
			IssuedBy: "workbench", IssuedAt: &qTestTime, CreatedAt: &qTestTime,
			Runs: []content.GateRunView{{
				RunID: "run_1", CertificateID: "cert_1", PolicyVersion: "pv-2026.1",
				ValidatorID: "v-digest", ValidatorVersion: "1.1", Verdict: "pass",
				Evidence: json.RawMessage(`{"checked":3}`), Confidence: "0.999",
				CostMs: 5, CostTokens: 7, RunAt: &qTestTime2, CreatedAt: &qTestTime,
				Verdicts: []content.GateVerdictView{{VerdictID: 1, RunID: "run_1", Detail: json.RawMessage(`{"d":1}`), CreatedAt: &qTestTime}},
			}},
		}},
	}
	f.withQueries(seed)
	staff := f.tokenFor(t, auth.Principal{Role: auth.RoleStaff, SubjectID: "staff-q"})

	cases := []struct {
		target   string
		wantKeys []string
	}{
		{"/items/item_1", []string{"item_id", "pack_id", "tier", "template_version_id", "current_version_id", "created_at", "current_version"}},
		{"/item_versions/iv_1", []string{"item_version_id", "item_id", "status", "objective", "interaction_ref", "content", "scoring_ref", "error_bindings", "lineage", "rendered_snapshot", "gate_certificate_id", "published_at", "retired_at", "created_at"}},
		{"/templates/tpl_1", []string{"template_id", "pack_id", "current_version_id", "created_at", "current_version"}},
		{"/gate_certificates/cert_1", []string{"cert_id", "artifact_ref", "cert_type", "policy_version", "issued_by", "issued_at", "created_at", "runs"}},
	}
	for _, tc := range cases {
		t.Run(tc.target, func(t *testing.T) {
			rec := f.do(http.MethodGet, tc.target, staff, "")
			if rec.Code != http.StatusOK {
				t.Fatalf("status = %d, want 200（body=%s）", rec.Code, rec.Body.String())
			}
			if ct := rec.Header().Get("Content-Type"); ct != "application/json" {
				t.Fatalf("Content-Type = %q, want application/json", ct)
			}
			var body map[string]json.RawMessage
			if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
				t.Fatalf("响应必须是 JSON: %v（body=%q）", err, rec.Body.String())
			}
			if len(body) != len(tc.wantKeys) {
				t.Fatalf("键数 = %d, want %d（契约 additionalProperties:false）: %v", len(body), len(tc.wantKeys), body)
			}
			for _, k := range tc.wantKeys {
				if _, ok := body[k]; !ok {
					t.Fatalf("缺契约键 %q", k)
				}
			}
		})
	}

	// ops 同权（角色矩阵的另一面）：任取一条代表断言.
	if rec := f.do(http.MethodGet, "/gate_certificates/cert_1", f.tokenFor(t, auth.Principal{Role: auth.RoleOps, SubjectID: "ops-q"}), ""); rec.Code != http.StatusOK {
		t.Fatalf("ops status = %d, want 200", rec.Code)
	}
}

// TestContentReadQueries_NullableShape 可空语义的契约形态：无当前版本的
// item 序列化 current_version/current_version_id = null；无 runs 的证书
// 序列化 runs = []（冻结 default_factory=list 语义）.
func TestContentReadQueries_NullableShape(t *testing.T) {
	f := newAPIFixture(t)
	f.withQueries(&memoryContentQueries{
		items: map[string]*content.ItemDetail{"item_naked": {ItemID: "item_naked", PackID: "pack_c", Tier: "C", CreatedAt: &qTestTime}},
		certificates: map[string]*content.GateCertificateDetail{"cert_naked": {
			CertID: "cert_naked", ArtifactRef: "cv_9", CertType: "retire",
			PolicyVersion: "pv-2026.1", IssuedBy: "ops", IssuedAt: &qTestTime, CreatedAt: &qTestTime,
			Runs: []content.GateRunView{}, // 空集 [] 语义由服务层保证（core 测试锁定），假件对齐同形态
		}},
	})
	staff := f.tokenFor(t, auth.Principal{Role: auth.RoleStaff, SubjectID: "staff-q"})

	rec := f.do(http.MethodGet, "/items/item_naked", staff, "")
	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200", rec.Code)
	}
	var body map[string]json.RawMessage
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
		t.Fatalf("反序列化: %v", err)
	}
	if string(body["current_version"]) != "null" || string(body["current_version_id"]) != "null" {
		t.Fatalf("无指针字段必须显式 null: %s", rec.Body.String())
	}

	rec = f.do(http.MethodGet, "/gate_certificates/cert_naked", staff, "")
	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200", rec.Code)
	}
	body = nil
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
		t.Fatalf("反序列化: %v", err)
	}
	if string(body["runs"]) != "[]" {
		t.Fatalf("空 runs 必须序列化 [] 而非 null: %s", rec.Body.String())
	}
}

// TestContentReadQueries_RoleMatrixStillEnforced 接线不松盾：学生与 service
// 主体对四端点仍 403（数据面对 staff/ops 开放不改变认证矩阵）.
func TestContentReadQueries_RoleMatrixStillEnforced(t *testing.T) {
	f := newAPIFixture(t)
	f.withQueries(&memoryContentQueries{
		items: map[string]*content.ItemDetail{"item_1": sampleItemDetail()},
	})
	targets := []string{"/items/item_1", "/item_versions/iv_1", "/templates/tpl_1", "/gate_certificates/cert_1"}
	for _, target := range targets {
		for _, p := range []auth.Principal{
			studentOf(apiAliasSelf),
			{Role: auth.RoleService, SubjectID: "svc-job"},
		} {
			rec := f.do(http.MethodGet, target, f.tokenFor(t, p), "")
			expectForbidden(t, rec)
		}
	}
}

// TestContentReadQueries_NotFound 无行 → 404 单字段 not_found（四个实体的
// 哨兵各自可判，api 层归一映射）.
func TestContentReadQueries_NotFound(t *testing.T) {
	f := newAPIFixture(t)
	f.withQueries(&memoryContentQueries{})
	staff := f.tokenFor(t, auth.Principal{Role: auth.RoleStaff, SubjectID: "staff-q"})
	for _, target := range []string{
		"/items/nope", "/item_versions/nope", "/templates/nope", "/gate_certificates/nope",
	} {
		rec := f.do(http.MethodGet, target, staff, "")
		if rec.Code != http.StatusNotFound {
			t.Fatalf("%s status = %d, want 404（body=%s）", target, rec.Code, rec.Body.String())
		}
		expectSingleFieldError(t, rec, middleware.ErrorClassNotFound)
	}
}

// TestContentReadQueries_InternalSanitized 驱动故障 → 500 单字段 internal：
// 原始错误消息绝不出现在响应体（脱敏红线），只进服务端日志.
func TestContentReadQueries_InternalSanitized(t *testing.T) {
	f := newAPIFixture(t)
	f.withQueries(&memoryContentQueries{failID: "boom"})
	staff := f.tokenFor(t, auth.Principal{Role: auth.RoleStaff, SubjectID: "staff-q"})
	for _, target := range []string{
		"/items/boom", "/item_versions/boom", "/templates/boom", "/gate_certificates/boom",
	} {
		rec := f.do(http.MethodGet, target, staff, "")
		if rec.Code != http.StatusInternalServerError {
			t.Fatalf("%s status = %d, want 500（body=%s）", target, rec.Code, rec.Body.String())
		}
		expectSingleFieldError(t, rec, middleware.ErrorClassInternal)
		if got := rec.Body.String(); strings.Contains(got, errFakeQuery.Error()) || strings.Contains(got, "fakeq") {
			t.Fatalf("响应泄漏内部错误细节: %s", got)
		}
	}
}

// TestContentReadQueries_NilStaysPlaceholder 未注入查询面 → 保持 501 占位
// （装配语义：认证盾照挂、业务面 fail-closed 不伪造数据）.
func TestContentReadQueries_NilStaysPlaceholder(t *testing.T) {
	f := newAPIFixture(t)
	f.app = NewRouterWithQueries(f.signer, f.consent, nil)
	staff := f.tokenFor(t, auth.Principal{Role: auth.RoleStaff, SubjectID: "staff-q"})
	expectPlaceholder(t, f.do(http.MethodGet, "/items/item_1", staff, ""))
	// 匿名仍 401：占位不豁免认证.
	expectUnauthorized(t, f.do(http.MethodGet, "/items/item_1", "", ""))
}
