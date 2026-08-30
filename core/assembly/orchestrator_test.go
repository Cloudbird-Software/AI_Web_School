package assembly

// orchestrator_test.go：编排层（#147）的测试。全链走 Memory fake 题源——
// 覆盖：编译→装载→曝光过滤→求解→渲染→制品的主链、paper_id 确定性、
// 蓝图/坏行/不可行的 fail-loud、QR 哨兵的如实入档（不吞不伪造）。

import (
	"context"
	"errors"
	"strings"
	"testing"
	"time"

	"github.com/Cloudbird-Software/AI_Web_School/core/render"
)

// fakeSource 是 PaperItemSource 的 Memory fake：过滤语义对齐 DB 实现
// （pack/objective->>gradeband 两维过滤，id 升序），供编排层全链测试.
type fakeSource struct {
	rows []map[string]any
	err  error
}

func (f *fakeSource) LoadPublishedItemVersions(_ context.Context, packID, gradeband string) ([]map[string]any, error) {
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

// candDict 构造一份合法候选 item_version dict（objective/interaction_ref/
// lineage/content 四块齐备，可直接进候选规范化与渲染）.
func candDict(packID, ivID, kp, gradeband string, prior float64) map[string]any {
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

// testBlueprint 构造最小可行蓝图：practice/M，题量 3–3，两个知识点.
func testBlueprint(packID string) PaperBlueprint {
	return PaperBlueprint{
		ProfileID:      "bp-test",
		ProfileVersion: "1",
		Purpose:        PurposePractice,
		Gradeband:      GradebandM,
		PackID:         packID,
		KpCodes:        []string{"KP1", "KP2"},
		Seed:           7,
		SnapshotRef:    "snap-test-1",
		Base:           map[string]any{"item_count_range": []any{3, 3}},
	}
}

func testSource() *fakeSource {
	return &fakeSource{rows: []map[string]any{
		candDict("pack-1", "iv-1", "KP1", "M", 0.9),
		candDict("pack-1", "iv-2", "KP1", "M", 0.8),
		candDict("pack-1", "iv-3", "KP2", "M", 0.7),
		candDict("pack-1", "iv-4", "KP2", "M", 0.6),
		// 他包/他学段行：编排过滤面必须透传维度（DB 实现的 WHERE 语义）.
		candDict("pack-2", "iv-x", "KP1", "M", 0.9),
		candDict("pack-1", "iv-y", "KP1", "L", 0.9),
	}}
}

func TestOrchestrateFullChain(t *testing.T) {
	orch := &Orchestrator{Source: testSource()}
	now := time.Date(2026, 8, 30, 10, 0, 0, 0, time.UTC)
	art, err := orch.Orchestrate(context.Background(), testBlueprint("pack-1"),
		OrchestrateOptions{Channel: "print", WeekLabel: "2026-W36", Now: now})
	if err != nil {
		t.Fatalf("编排失败: %v", err)
	}

	// 制品元数据：题量 3、题序留档、摘要非空.
	m := art.Metadata
	if m.ItemCount != 3 || len(m.ItemVersionIDs) != 3 {
		t.Fatalf("item_count = %d（ids %v），want 3", m.ItemCount, m.ItemVersionIDs)
	}
	if m.BlueprintDigest == "" || m.SelectionDigest == "" || m.PaperID == "" {
		t.Fatalf("摘要/id 不得为空: %+v", m)
	}
	if m.GeneratedAt != now.UTC().Format(time.RFC3339) {
		t.Fatalf("GeneratedAt = %q, want 注入时钟 %q", m.GeneratedAt, now.UTC().Format(time.RFC3339))
	}
	if m.Purpose != PurposePractice || m.Gradeband != GradebandM || m.PackID != "pack-1" {
		t.Fatalf("卷元数据定位面漂移: %+v", m)
	}

	// HTML：卷头 + 全部入选题块 + 题号.
	html := string(art.HTML)
	for _, id := range m.ItemVersionIDs {
		if !strings.Contains(html, `data-item-version-id="`+id+`"`) {
			t.Fatalf("卷面缺入选题 %s", id)
		}
	}
	for _, n := range []string{"1.", "2.", "3."} {
		if !strings.Contains(html, "<div class=\"item-number\">"+n+"</div>") {
			t.Fatalf("卷面缺题号 %s", n)
		}
	}
	if !strings.Contains(html, "paper-code") {
		t.Fatalf("卷面缺卷头")
	}

	// QR 槽位（#152 接线后）：payload 可验、位图真实产出、零哨兵残留.
	if !render.VerifyQRPayload(art.QR.Payload) {
		t.Fatalf("QR payload 未过 Luhn 校验: %q", art.QR.Payload)
	}
	if !strings.HasPrefix(art.QR.SVG, "<svg") {
		t.Fatalf("#152 接线后卷头必须携带真实位图: %q", art.QR.SVG)
	}
	if art.QR.Err != "" {
		t.Fatalf("位图生成成功不得残留哨兵错误: %q", art.QR.Err)
	}
	// payload 锚定 paper_id（扫码回查的既定口径）.
	if !strings.HasPrefix(art.QR.Payload, m.PaperID) {
		t.Fatalf("QR payload 未锚定 paper_id")
	}
}

func TestOrchestratePaperIDDeterministic(t *testing.T) {
	orch := &Orchestrator{Source: testSource()}
	a1, err := orch.Orchestrate(context.Background(), testBlueprint("pack-1"), OrchestrateOptions{Now: time.Unix(1, 0)})
	if err != nil {
		t.Fatalf("编排 1 失败: %v", err)
	}
	a2, err := orch.Orchestrate(context.Background(), testBlueprint("pack-1"), OrchestrateOptions{Now: time.Unix(999, 0)})
	if err != nil {
		t.Fatalf("编排 2 失败: %v", err)
	}
	if a1.Metadata.PaperID != a2.Metadata.PaperID {
		t.Fatalf("同蓝图同池同种子 paper_id 漂移: %s vs %s", a1.Metadata.PaperID, a2.Metadata.PaperID)
	}

	bp := testBlueprint("pack-1")
	bp.Seed = 8
	a3, err := orch.Orchestrate(context.Background(), bp, OrchestrateOptions{})
	if err != nil {
		t.Fatalf("编排 3 失败: %v", err)
	}
	if a3.Metadata.PaperID == a1.Metadata.PaperID {
		t.Fatalf("种子变化 paper_id 不应不变（确定性内容寻址失效）")
	}
}

func TestOrchestrateInvalidBlueprintFailsLoud(t *testing.T) {
	orch := &Orchestrator{Source: testSource()}
	cases := map[string]func(*PaperBlueprint){
		"缺 pack_id":    func(b *PaperBlueprint) { b.PackID = "" },
		"缺 profile_id": func(b *PaperBlueprint) { b.ProfileID = "" },
		"缺 kp_codes":   func(b *PaperBlueprint) { b.KpCodes = nil },
		"kp 空码":        func(b *PaperBlueprint) { b.KpCodes = []string{"KP1", ""} },
	}
	for name, mutate := range cases {
		bp := testBlueprint("pack-1")
		mutate(&bp)
		art, err := orch.Orchestrate(context.Background(), bp, OrchestrateOptions{})
		if !errors.Is(err, ErrInvalidBlueprint) {
			t.Fatalf("%s: 应返回 ErrInvalidBlueprint, got %v", name, err)
		}
		if art != nil {
			t.Fatalf("%s: 非法蓝图不得返回制品", name)
		}
	}

	// 用途越域：编译面（CompileProfile）的值域裁决原样上抛，不归入蓝图哨兵.
	bp := testBlueprint("pack-1")
	bp.Purpose = "exam"
	if _, err := orch.Orchestrate(context.Background(), bp, OrchestrateOptions{}); err == nil ||
		strings.Contains(err.Error(), ErrInvalidBlueprint.Error()) {
		t.Fatalf("用途越域应走编译错误面: %v", err)
	}
}

func TestOrchestrateNilSourceFailsClosed(t *testing.T) {
	orch := &Orchestrator{}
	if art, err := orch.Orchestrate(context.Background(), testBlueprint("pack-1"), OrchestrateOptions{}); err == nil || art != nil {
		t.Fatalf("题源未注入必须拒绝编排: %v, %v", art, err)
	}
	var nilOrch *Orchestrator
	if _, err := nilOrch.Orchestrate(context.Background(), testBlueprint("pack-1"), OrchestrateOptions{}); err == nil {
		t.Fatalf("nil 编排器必须拒绝")
	}
}

func TestOrchestrateSourceErrorPassthrough(t *testing.T) {
	boom := errors.New("db down")
	orch := &Orchestrator{Source: &fakeSource{err: boom}}
	_, err := orch.Orchestrate(context.Background(), testBlueprint("pack-1"), OrchestrateOptions{})
	if !errors.Is(err, boom) {
		t.Fatalf("题源故障应原样透传, got %v", err)
	}
}

func TestOrchestrateBadRowFailsLoud(t *testing.T) {
	bad := candDict("pack-1", "iv-1", "KP1", "M", 0.9)
	delete(bad, "item_version_id") // 身份字段缺失：池装载期必须拒绝
	orch := &Orchestrator{Source: &fakeSource{rows: []map[string]any{bad}}}
	_, err := orch.Orchestrate(context.Background(), testBlueprint("pack-1"), OrchestrateOptions{})
	if !errors.Is(err, ErrInvalidCandidateRow) {
		t.Fatalf("坏行应 ErrInvalidCandidateRow, got %v", err)
	}

	// 内容块坏行（kp_set 空）：候选规范化面（既有校验）fail-loud.
	noKp := candDict("pack-1", "iv-1", "KP1", "M", 0.9)
	noKp["objective"] = map[string]any{"gradeband": "M", "kp_set": []any{}}
	orch = &Orchestrator{Source: &fakeSource{rows: []map[string]any{noKp}}}
	if _, err := orch.Orchestrate(context.Background(), testBlueprint("pack-1"), OrchestrateOptions{}); err == nil {
		t.Fatalf("kp_set 空的坏行必须拒绝")
	}
}

func TestOrchestrateInfeasibleFailsLoud(t *testing.T) {
	// 池只有 1 题，蓝图要 3 题：不可行按结构化报告上抛，绝不降级凑卷.
	src := testSource()
	src.rows = src.rows[:1]
	orch := &Orchestrator{Source: src}
	art, err := orch.Orchestrate(context.Background(), testBlueprint("pack-1"), OrchestrateOptions{})
	if err == nil || art != nil {
		t.Fatalf("不可行必须失败且无制品: %v, %v", art, err)
	}
	var infeasible *InfeasibleError
	if !errors.As(err, &infeasible) {
		t.Fatalf("不可行应保留 InfeasibleError 结构化报告, got %T", err)
	}
	if len(infeasible.Report.Conflicts) == 0 {
		t.Fatalf("冲突报告不得为空")
	}
}

func TestOrchestrateExposureFilterExcludes(t *testing.T) {
	exposure := NewMemoryExposureStore()
	exposed := candDict("pack-1", "iv-1", "KP1", "M", 0.9)
	exposedCand, err := CandidateFromServingRow(ServingRow{
		ItemVersionID:     "iv-1",
		ItemID:            "item-iv-1",
		TemplateVersionID: "tpl-1",
		Objective:         exposed["objective"].(map[string]any),
		InteractionRef:    exposed["interaction_ref"].(map[string]any),
		Lineage:           exposed["lineage"].(map[string]any),
	})
	if err != nil {
		t.Fatalf("构造曝光候选失败: %v", err)
	}
	// 预录当期曝光（store 的 Queue 查询是 exact-week 语义，与冻结 SQL 同口径；
	// 跨期窗口属 store 层查询语义扩展，不在编排层私造）.
	if _, err := exposure.RecordPaperExposures(context.Background(), PaperExposureInput{
		Channel: "print", SubjectPackID: "pack-1",
		Gradeband: "M", WeekLabel: "2026-W35",
		Items:   []CandidateItem{exposedCand},
		PaperID: nil,
	}); err != nil {
		t.Fatalf("预录曝光失败: %v", err)
	}
	orch := &Orchestrator{Source: testSource(), Exposure: exposure}
	art, err := orch.Orchestrate(context.Background(), testBlueprint("pack-1"),
		OrchestrateOptions{Channel: "print", WeekLabel: "2026-W35"})
	if err != nil {
		t.Fatalf("编排失败: %v", err)
	}
	for _, id := range art.Metadata.ItemVersionIDs {
		if id == "iv-1" {
			t.Fatalf("当期已曝光题 %s 不应再次入选（静态轨防重复）", id)
		}
	}
	// 曝光排除后剩余候选恰满足题量下限.
	if art.Metadata.ItemCount != 3 {
		t.Fatalf("曝光过滤后题量 = %d, want 3", art.Metadata.ItemCount)
	}
}

func TestOrchestrateRenderFailureFailsLoud(t *testing.T) {
	badBlocks := func(d map[string]any) map[string]any {
		d["content"] = map[string]any{"blocks": []any{
			map[string]any{"type": "unknown_block"}, // 未知块类型：IR 转换必须失败
		}}
		return d
	}
	// 候选覆盖 KP1+KP2（蓝图配额可满足，失败点才落在渲染哨兵而非不可行报告）
	src := &fakeSource{rows: []map[string]any{
		badBlocks(candDict("pack-1", "iv-1", "KP1", "M", 0.9)),
		badBlocks(candDict("pack-1", "iv-2", "KP2", "M", 0.8)),
		badBlocks(candDict("pack-1", "iv-3", "KP1", "M", 0.7)),
	}}
	orch := &Orchestrator{Source: src}
	art, err := orch.Orchestrate(context.Background(), testBlueprint("pack-1"), OrchestrateOptions{})
	if err == nil || art != nil {
		t.Fatalf("渲染失败必须 fail-loud 且无制品: %v, %v", art, err)
	}
	if !errors.Is(err, render.ErrInvalidItemVersion) {
		t.Fatalf("渲染失败应保留 render 哨兵, got %v", err)
	}
}
