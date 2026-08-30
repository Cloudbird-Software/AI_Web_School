package api

// papers_test.go：组卷/出题三端（#148）的 HTTP 行为测试。全链 Memory fake
// （PaperItemSource 假题源 + MemoryPaperArtifacts，零 DB），httptest 直驱
// mux（无 goroutine，兼容 goleak TestMain 与 -race）。断言面：
//   - POST /papers：staff 201 卷元数据 → 制品可从 Memory 账回读；蓝图非法 /
//     非法 JSON 422 bad_request；编排失败（不可行）500 internal 单字段脱敏；
//     候选面或制品账未注入 501 占位；
//   - GET /papers/{paper_id}：已落账制品回读、无行 404 not_found；
//   - POST /generate：确定性出题 200（草稿不入账）；入参非法 422；
//   - 角色面：三端 student/service 一律 403、匿名 401（staffOrOps 盾不因
//     业务接线而松动）。

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"strings"
	"testing"

	"github.com/Cloudbird-Software/AI_Web_School/api/middleware"
	"github.com/Cloudbird-Software/AI_Web_School/core/assembly"
	"github.com/Cloudbird-Software/AI_Web_School/core/auth"
)

// paperBlueprintJSON 是最小可行蓝图的契约形态（字段面 = PaperBlueprint 的
// JSON 投影；题量 3–3 对齐编排层测试的可行域）.
const paperBlueprintJSON = `{
	"profile_id": "bp-api-148",
	"profile_version": "1",
	"purpose": "practice",
	"gradeband": "M",
	"pack_id": "pack-1",
	"kp_codes": ["KP1", "KP2"],
	"seed": 7,
	"snapshot_ref": "snap-148",
	"base": {"item_count_range": [3, 3]}
}`

// fakePaperSource 是 PaperItemSource 的 Memory fake：过滤语义对齐 DB 实现
// （pack × objective->>gradeband 两维过滤；core/assembly 编排层测试同构）.
type fakePaperSource struct {
	rows []map[string]any
	err  error
}

func (f *fakePaperSource) LoadPublishedItemVersions(_ context.Context, packID, gradeband string) ([]map[string]any, error) {
	if f.err != nil {
		return nil, f.err
	}
	out := []map[string]any{}
	for _, r := range f.rows {
		if r["pack_id"] != packID {
			continue
		}
		obj, _ := r["objective"].(map[string]any)
		if gb, _ := obj["gradeband"].(string); gb != gradeband {
			continue
		}
		out = append(out, r)
	}
	return out, nil
}

// paperCandDict 构造一份合法候选 item_version dict（objective/interaction_ref/
// lineage/content 四块齐备，可直接进候选规范化与渲染）.
func paperCandDict(packID, ivID, kp, gradeband string, prior float64) map[string]any {
	return map[string]any{
		"item_version_id":     ivID,
		"item_id":             "item-" + ivID,
		"template_version_id": "tpl-" + ivID,
		"pack_id":             packID,
		"objective": map[string]any{
			"gradeband":   gradeband,
			"kp_set":      []any{map[string]any{"code": kp}},
			"kp_set_mode": "single",
		},
		"interaction_ref": map[string]any{"interaction_id": "single_choice"},
		"lineage": map[string]any{"params": map[string]any{
			"p_correct_prior":  prior,
			"allowed_purposes": []any{"practice"},
		}},
		"content": map[string]any{"blocks": []any{
			map[string]any{"type": "text", "value": "3 + 5 = ?"},
			map[string]any{"type": "choice", "options": []any{
				map[string]any{"id": "A", "label": "8"},
				map[string]any{"id": "B", "label": "9"},
			}},
		}},
	}
}

// paperTestSource 四题候选池（KP1×2 + KP2×2，蓝图题量 3 可行）.
func paperTestSource() *fakePaperSource {
	return &fakePaperSource{rows: []map[string]any{
		paperCandDict("pack-1", "iv-1", "KP1", "M", 0.9),
		paperCandDict("pack-1", "iv-2", "KP1", "M", 0.8),
		paperCandDict("pack-1", "iv-3", "KP2", "M", 0.7),
		paperCandDict("pack-1", "iv-4", "KP2", "M", 0.6),
	}}
}

// withPapers 在标准 fixture 上注入组卷/出题装配重装 router.
func (f *apiFixture) withPapers(papers PapersWiring) {
	f.app = NewRouterWithPapers(f.signer, f.consent, nil, nil, nil, LearnerReads{}, papers)
}

// paperStaffTok 是 staff 主体的合法令牌（组卷面属教研/运维生产域）.
func paperStaffTok(t *testing.T, f *apiFixture) string {
	t.Helper()
	return f.tokenFor(t, auth.Principal{Role: auth.RoleStaff, SubjectID: "staff-paper"})
}

// --- POST /generate ---

// TestGenerate_DeterministicDraft 端点 3 正例：subjectmath 管线确定性出题
// → 200 三键回执；同 (template_id, n, seed) 重跑 digests 逐位相同（R-Z-01
// 可回放）；产物是草稿的语义由端点注释承载（账面零写入在 cmd/ingest 侧
// 断言，HTTP 层以响应键面不出现任何"已入库"声明为可执行形态）.
func TestGenerate_DeterministicDraft(t *testing.T) {
	f := newAPIFixture(t)
	f.withPapers(PapersWiring{})
	staff := paperStaffTok(t, f)
	body := `{"template_id":"tpl-sm-int-addsub-nb","n":2,"seed":42}`

	rec := f.do(http.MethodPost, "/generate", staff, body)
	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200（body=%s）", rec.Code, rec.Body.String())
	}
	var rep generateResponse
	if err := json.Unmarshal(rec.Body.Bytes(), &rep); err != nil {
		t.Fatalf("200 响应必须是 generateResponse JSON: %v（body=%q）", err, rec.Body.String())
	}
	if rep.Generated != 2 || rep.Accepted != 2 || len(rep.Digests) != 2 {
		t.Fatalf("回执面漂移: %+v", rep)
	}
	for i, d := range rep.Digests {
		if d == "" {
			t.Fatalf("digests[%d] 为空", i)
		}
	}

	again := f.do(http.MethodPost, "/generate", staff, body)
	var rep2 generateResponse
	if err := json.Unmarshal(again.Body.Bytes(), &rep2); err != nil {
		t.Fatalf("二次请求解析失败: %v", err)
	}
	for i := range rep.Digests {
		if rep.Digests[i] != rep2.Digests[i] {
			t.Fatalf("确定性破坏：digests[%d] 漂移 %s vs %s", i, rep.Digests[i], rep2.Digests[i])
		}
	}
}

// TestGenerate_InvalidInput422 入参违例 → 422 bad_request：非法 JSON /
// 缺 template_id / n 非正 / 未知母题 / n 超参数空间（确定性拒绝，同参重试
// 无意义）.
func TestGenerate_InvalidInput422(t *testing.T) {
	f := newAPIFixture(t)
	f.withPapers(PapersWiring{})
	staff := paperStaffTok(t, f)
	for name, body := range map[string]string{
		"非法 JSON":    `{"template_id":`,
		"空请求体":       "",
		"缺 template": `{"n":2,"seed":1}`,
		"n 非正":       `{"template_id":"tpl-sm-int-addsub-nb","n":0,"seed":1}`,
		"未知母题":       `{"template_id":"tpl-no-such","n":2,"seed":1}`,
		"n 超空间":      `{"template_id":"tpl-sm-int-addsub-nb","n":999999,"seed":1}`,
	} {
		rec := f.do(http.MethodPost, "/generate", staff, body)
		if rec.Code != http.StatusUnprocessableEntity {
			t.Fatalf("%s: status = %d, want 422（body=%s）", name, rec.Code, rec.Body.String())
		}
		expectSingleFieldError(t, rec, middleware.ErrorClassBadRequest)
	}
}

// TestGenerate_FakePipelineInjection 注入面：Generator 接缝注入 fake 后端点
// 走 fake（api 层与学科包解耦的接缝可执行性）.
func TestGenerate_FakePipelineInjection(t *testing.T) {
	f := newAPIFixture(t)
	f.withPapers(PapersWiring{Generator: func(string, int, uint64) (int, int, []string, error) {
		return 1, 1, []string{"digest-x"}, nil
	}})
	rec := f.do(http.MethodPost, "/generate", paperStaffTok(t, f), `{"template_id":"whatever","n":1,"seed":1}`)
	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200", rec.Code)
	}
	var rep generateResponse
	if err := json.Unmarshal(rec.Body.Bytes(), &rep); err != nil {
		t.Fatalf("解析回执: %v", err)
	}
	if rep.Digests[0] != "digest-x" {
		t.Fatalf("未走注入管线: %+v", rep)
	}
}

// --- GET /papers/{paper_id} ---

// failingPaperStore 驱动故障注入源（与 ErrUnknownPaper 异源）.
type failingPaperStore struct{ err error }

func (s *failingPaperStore) Save(context.Context, *assembly.PaperArtifact) error { return s.err }

func (s *failingPaperStore) Get(context.Context, string) (*assembly.PaperArtifact, error) {
	return nil, s.err
}

// TestReadPaper_ArtifactRoundtrip 端点 2 正例：POST /papers 落账后按
// paper_id GET 回读 → 200 制品 JSON（metadata 寻址一致、QR payload 可验、
// HTML 字节完整；html 字段线上形态为 base64——encoding/json 对 []byte 的
// 既定惯例——解码回原字节由同一惯例承担）.
func TestReadPaper_ArtifactRoundtrip(t *testing.T) {
	f := newAPIFixture(t)
	f.withPapers(PapersWiring{
		Orchestrator: &assembly.Orchestrator{Source: paperTestSource()},
		Artifacts:    NewMemoryPaperArtifacts(),
	})
	staff := paperStaffTok(t, f)

	created := f.do(http.MethodPost, "/papers", staff, paperBlueprintJSON)
	if created.Code != http.StatusCreated {
		t.Fatalf("前置创建失败: %d", created.Code)
	}
	var meta assembly.PaperMetadata
	if err := json.Unmarshal(created.Body.Bytes(), &meta); err != nil {
		t.Fatalf("解析 201 元数据: %v", err)
	}

	rec := f.do(http.MethodGet, "/papers/"+meta.PaperID, staff, "")
	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200（body=%s）", rec.Code, rec.Body.String())
	}
	var art assembly.PaperArtifact
	if err := json.Unmarshal(rec.Body.Bytes(), &art); err != nil {
		t.Fatalf("200 响应必须是 PaperArtifact JSON: %v", err)
	}
	if art.Metadata.PaperID != meta.PaperID {
		t.Fatalf("回读 paper_id 漂移: %s vs %s", art.Metadata.PaperID, meta.PaperID)
	}
	if art.QR.Payload == "" {
		t.Fatalf("回读制品缺 QR payload")
	}
	if !strings.Contains(string(art.HTML), meta.PaperID) {
		t.Fatalf("回读卷面缺 paper_id 锚")
	}
}

// TestReadPaper_Unknown404 账面无行 → 404 not_found（单字段脱敏）.
func TestReadPaper_Unknown404(t *testing.T) {
	f := newAPIFixture(t)
	f.withPapers(PapersWiring{
		Orchestrator: &assembly.Orchestrator{Source: paperTestSource()},
		Artifacts:    NewMemoryPaperArtifacts(),
	})
	rec := f.do(http.MethodGet, "/papers/no-such-paper", paperStaffTok(t, f), "")
	if rec.Code != http.StatusNotFound {
		t.Fatalf("status = %d, want 404", rec.Code)
	}
	expectSingleFieldError(t, rec, middleware.ErrorClassNotFound)
}

// TestReadPaper_StoreFailure500 存储驱动故障 → 500 internal（不与无行混同）.
func TestReadPaper_StoreFailure500(t *testing.T) {
	f := newAPIFixture(t)
	f.withPapers(PapersWiring{
		Orchestrator: &assembly.Orchestrator{Source: paperTestSource()},
		Artifacts:    &failingPaperStore{err: errors.New("storage boom")},
	})
	rec := f.do(http.MethodGet, "/papers/whatever", paperStaffTok(t, f), "")
	if rec.Code != http.StatusInternalServerError {
		t.Fatalf("status = %d, want 500", rec.Code)
	}
	expectSingleFieldError(t, rec, middleware.ErrorClassInternal)
}

// --- POST /papers ---

// TestCreatePaper_StaffFullChain 契约正例：staff 出蓝图 → 201 卷元数据
// → 同 paper_id 从制品账回读（编排→落账的最小闭环）.
func TestCreatePaper_StaffFullChain(t *testing.T) {
	f := newAPIFixture(t)
	arts := NewMemoryPaperArtifacts()
	f.withPapers(PapersWiring{
		Orchestrator: &assembly.Orchestrator{Source: paperTestSource()},
		Artifacts:    arts,
	})

	rec := f.do(http.MethodPost, "/papers", paperStaffTok(t, f), paperBlueprintJSON)
	if rec.Code != http.StatusCreated {
		t.Fatalf("status = %d, want 201（body=%s）", rec.Code, rec.Body.String())
	}
	var meta assembly.PaperMetadata
	if err := json.Unmarshal(rec.Body.Bytes(), &meta); err != nil {
		t.Fatalf("201 响应必须是 PaperMetadata JSON: %v（body=%q）", err, rec.Body.String())
	}
	if meta.PaperID == "" || meta.BlueprintDigest == "" || meta.SelectionDigest == "" {
		t.Fatalf("卷元数据寻址/摘要面不得为空: %+v", meta)
	}
	if meta.ItemCount != 3 || len(meta.ItemVersionIDs) != 3 {
		t.Fatalf("item_count = %d, want 3（蓝图 item_count_range [3,3]）", meta.ItemCount)
	}
	if meta.Purpose != "practice" || meta.Gradeband != "M" || meta.PackID != "pack-1" || meta.Seed != 7 {
		t.Fatalf("卷元数据定位面漂移: %+v", meta)
	}
	// 制品落账可回读（端口直查；HTTP 回读面在端点 2 测试）.
	art, err := arts.Get(context.Background(), meta.PaperID)
	if err != nil {
		t.Fatalf("201 后制品账必须可回读: %v", err)
	}
	if len(art.HTML) == 0 || art.QR.Payload == "" {
		t.Fatalf("制品 HTML/QR payload 不得为空")
	}
	// 卷面锚定 paper_id（内容寻址 D3 的可回放声明）.
	if !strings.Contains(string(art.HTML), `data-paper-id="`+meta.PaperID+`"`) {
		t.Fatalf("卷面缺 paper_id 锚")
	}
}

// TestCreatePaper_InvalidBlueprint422 蓝图非法（缺必填字段）与请求体非 JSON
// 都归 422 bad_request（调用方可修的输入违例；哨兵映射不做字符串匹配）.
func TestCreatePaper_InvalidBlueprint422(t *testing.T) {
	f := newAPIFixture(t)
	f.withPapers(PapersWiring{
		Orchestrator: &assembly.Orchestrator{Source: paperTestSource()},
		Artifacts:    NewMemoryPaperArtifacts(),
	})

	for name, body := range map[string]string{
		"缺 pack_id":  `{"profile_id":"bp","profile_version":"1","kp_codes":["KP1"]}`,
		"缺 kp_codes": `{"profile_id":"bp","profile_version":"1","pack_id":"pack-1"}`,
		"非法 JSON":    `{"profile_id":`,
		"空请求体":       "",
	} {
		rec := f.do(http.MethodPost, "/papers", paperStaffTok(t, f), body)
		if rec.Code != http.StatusUnprocessableEntity {
			t.Fatalf("%s: status = %d, want 422（body=%s）", name, rec.Code, rec.Body.String())
		}
		expectSingleFieldError(t, rec, middleware.ErrorClassBadRequest)
	}
}

// TestCreatePaper_OrchestrationFailureSanitized 编排失败（池不可行）→ 500
// internal 单字段脱敏：InfeasibleError 的结构化冲突清单只进服务端日志，
// 绝不外泄（脱敏形态与认证错误同构）.
func TestCreatePaper_OrchestrationFailureSanitized(t *testing.T) {
	f := newAPIFixture(t)
	starved := paperTestSource()
	starved.rows = starved.rows[:1] // 池 1 题，蓝图要 3 题：不可行
	f.withPapers(PapersWiring{
		Orchestrator: &assembly.Orchestrator{Source: starved},
		Artifacts:    NewMemoryPaperArtifacts(),
	})

	rec := f.do(http.MethodPost, "/papers", paperStaffTok(t, f), paperBlueprintJSON)
	if rec.Code != http.StatusInternalServerError {
		t.Fatalf("status = %d, want 500（body=%s）", rec.Code, rec.Body.String())
	}
	expectSingleFieldError(t, rec, middleware.ErrorClassInternal)
	if strings.Contains(rec.Body.String(), "conflict") || strings.Contains(rec.Body.String(), "assembly") {
		t.Fatalf("500 响应泄露内部细节: %s", rec.Body.String())
	}
}

// TestCreatePaper_SourceFailure500 候选池装载故障 → 500 internal（fail-loud
// 不降级，脱敏同上）.
func TestCreatePaper_SourceFailure500(t *testing.T) {
	f := newAPIFixture(t)
	f.withPapers(PapersWiring{
		Orchestrator: &assembly.Orchestrator{Source: &fakePaperSource{err: errors.New("db down")}},
		Artifacts:    NewMemoryPaperArtifacts(),
	})
	rec := f.do(http.MethodPost, "/papers", paperStaffTok(t, f), paperBlueprintJSON)
	if rec.Code != http.StatusInternalServerError {
		t.Fatalf("status = %d, want 500", rec.Code)
	}
	expectSingleFieldError(t, rec, middleware.ErrorClassInternal)
}

// TestPapers_DependenciesNotWired501 装配语义：候选面（编排器）或制品账
// 未注入 → 对应端点保持 501 占位（fail-closed，认证盾照挂）.
func TestPapers_DependenciesNotWired501(t *testing.T) {
	orch := &assembly.Orchestrator{Source: paperTestSource()}
	cases := map[string]PapersWiring{
		"全未注入":   {},
		"缺制品账":   {Orchestrator: orch},
		"缺编排器":   {Artifacts: NewMemoryPaperArtifacts()},
		"题源为nil": {Orchestrator: &assembly.Orchestrator{}, Artifacts: NewMemoryPaperArtifacts()},
	}
	for name, wiring := range cases {
		f := newAPIFixture(t)
		f.withPapers(wiring)
		rec := f.do(http.MethodPost, "/papers", paperStaffTok(t, f), paperBlueprintJSON)
		if rec.Code != http.StatusNotImplemented {
			t.Fatalf("%s: status = %d, want 501", name, rec.Code)
		}
		expectPlaceholder(t, rec)
	}
}

// TestPapers_RoleMatrix 角色面：组卷/出题三端属 staffOrOps 域——student 与
// service 一律 403；匿名 401（盾在业务之前，接线不松动）.
func TestPapers_RoleMatrix(t *testing.T) {
	targets := []struct{ method, path, body string }{
		{http.MethodPost, "/papers", paperBlueprintJSON},
		{http.MethodGet, "/papers/some-paper-id", ""},
		{http.MethodPost, "/generate", `{"template_id":"t","n":1,"seed":1}`},
	}
	for _, tt := range targets {
		t.Run(tt.method+" "+tt.path, func(t *testing.T) {
			f := newAPIFixture(t)
			f.withPapers(PapersWiring{
				Orchestrator: &assembly.Orchestrator{Source: paperTestSource()},
				Artifacts:    NewMemoryPaperArtifacts(),
			})
			expectUnauthorized(t, f.do(tt.method, tt.path, "", tt.body))
			for _, p := range []auth.Principal{
				studentOf(apiAliasSelf),
				{Role: auth.RoleService, SubjectID: "svc-job"},
			} {
				rec := f.do(tt.method, tt.path, f.tokenFor(t, p), tt.body)
				expectForbidden(t, rec)
			}
			// staff/ops 盾贯通（业务态由端点各自测试断言，这里只证链路可达）.
			for _, role := range []auth.Role{auth.RoleStaff, auth.RoleOps} {
				rec := f.do(tt.method, tt.path, f.tokenFor(t, auth.Principal{Role: role, SubjectID: string(role) + "-ok"}), tt.body)
				if rec.Code == http.StatusForbidden || rec.Code == http.StatusUnauthorized {
					t.Fatalf("role=%s 不应被盾拒绝, got %d", role, rec.Code)
				}
			}
		})
	}
}
