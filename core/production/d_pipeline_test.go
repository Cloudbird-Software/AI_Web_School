// d_pipeline_test.go D 线流水线骨架 Go 移植的验收测试（对照冻结
// tests/unit/test_d_line_pipeline.py：验收①item_id+证书、②scoring_ref
// 指向 ai_rubric 且量规嵌入、③量规完整性校验、门失败不入库、阶段间
// 显式传递与 fail-loud）。
package production

import (
	"context"
	"errors"
	"strings"
	"sync"
	"testing"

	"github.com/Cloudbird-Software/AI_Web_School/core/scoring"
)

// ────────────────────────────────────────────────────────────────────
// 测试夹具：蓝图 / 量规 / Memory 端口
// ────────────────────────────────────────────────────────────────────

func mustPipelineBlueprint(t *testing.T, writingType string) *Blueprint {
	t.Helper()
	bp, err := MakeBlueprint(
		"bp-d-line-composition-test",
		writingType,
		"subject-chinese",
		"sha256:tpl-chinese-composition-v1",
		"sha256:test-d-line-rubric-M-v1",
		[]string{"春天", "秋天", "成长"},
		40,
		"1",
	)
	if err != nil {
		t.Fatalf("MakeBlueprint 失败: %v", err)
	}
	return bp
}

func mustPipelineRubric(t *testing.T) *RubricTemplate {
	t.Helper()
	rubric := mustRubric(t, GradebandM)
	rubric.RubricID = "sha256:test-d-line-rubric-M-v1"
	return rubric
}

// dLineDraftGenerator 模拟 A 线实例化引擎端口（验收②：scoring_ref 指向
// ai_rubric，量规嵌入 scorer_params；公式二给真实内容寻址 id）.
func dLineDraftGenerator(t *testing.T) GeneratorFunc {
	t.Helper()
	return func(req GenerateRequest) (*ItemVersion, error) {
		if req.Rubric == nil {
			t.Fatal("生成请求必须携带量规")
		}
		scoringRef := map[string]any{
			"scorer_id":     req.ScorerID,
			"scorer_params": map[string]any{"rubric": req.Rubric.ToScorerParams()},
		}
		objective := map[string]any{"gradeband": req.GradeBand, "topic": req.InstantiateParams["topic"]}
		interactionRef := map[string]any{"interaction_id": req.InteractionID}
		content := map[string]any{"blocks": []any{
			map[string]any{"type": "text", "value": "写作题：" + strParam(req.InstantiateParams, "topic")},
		}}
		ivID, err := ComputeCanonicalItemVersionID(objective, interactionRef, content, scoringRef, []any{}, req.Locale)
		if err != nil {
			t.Fatalf("公式二失败: %v", err)
		}
		return &ItemVersion{
			ItemVersionID:  ivID,
			ItemID:         ivID,
			Status:         "draft",
			Objective:      objective,
			InteractionRef: interactionRef,
			Content:        content,
			ScoringRef:     scoringRef,
			ErrorBindings:  []any{},
			Lineage: map[string]any{
				"tier": "A", "signed_by": req.SignedBy,
				"pack_digest": req.PackDigest,
			},
		}, nil
	}
}

// memGenerator Memory 生成端口：记录请求，回放预置产物/错误.
type memGenerator struct {
	reqs []GenerateRequest
	iv   *ItemVersion
	err  error
}

func (g *memGenerator) Generate(req GenerateRequest) (*ItemVersion, error) {
	g.reqs = append(g.reqs, req)
	if g.err != nil {
		return nil, g.err
	}
	return g.iv, nil
}

// memSink Memory 入库端口：记录产物，回执预置发布/错误.
type memSink struct {
	mu        sync.Mutex
	published []*ItemVersion
	certID    string
	err       error
}

func (s *memSink) Publish(iv *ItemVersion) (*Publication, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.err != nil {
		return nil, s.err
	}
	s.published = append(s.published, iv)
	return &Publication{ItemID: "item-" + iv.ItemVersionID, ItemVersionID: iv.ItemVersionID, CertificateID: s.certID}, nil
}

// capturingValidator 捕获校验阶段看到的 artifact（阶段显式性锚定用）.
type capturingValidator struct {
	id       string
	verdict  string
	blocking bool
	seen     *PipelineArtifact
	calls    int
}

func (c *capturingValidator) ValidatorID() string { return c.id }

func (c *capturingValidator) Validate(artifact *PipelineArtifact) ValidatorResult {
	c.calls++
	c.seen = artifact
	verdict := c.verdict
	if verdict == "" {
		verdict = VerdictPass
	}
	blocking := c.blocking
	return ValidatorResult{ValidatorID: c.id, Verdict: verdict, Blocking: blocking, Evidence: map[string]any{}}
}

func newTestPipeline(t *testing.T, gen Generator, sink ItemSink, extra ...ArtifactValidator) (*DPipeline, *BlueprintRegistry) {
	t.Helper()
	reg := NewBlueprintRegistry()
	bp := mustPipelineBlueprint(t, WritingComposition)
	if err := reg.Register(bp.BlueprintID, BlueprintEntry{
		Blueprint:       bp,
		Rubric:          mustPipelineRubric(t),
		TemplateVersion: map[string]any{"template_id": "tpl", "dsl_version": "1"},
		PackDigest:      "sha256:pack-subject-chinese-d-line-test",
	}); err != nil {
		t.Fatalf("注册蓝图失败: %v", err)
	}
	dp, err := NewDPipeline(reg, gen, sink, extra...)
	if err != nil {
		t.Fatalf("NewDPipeline 失败: %v", err)
	}
	return dp, reg
}

// ────────────────────────────────────────────────────────────────────
// 注册表（Memory 面）
// ────────────────────────────────────────────────────────────────────

func TestRegistryLifecycle(t *testing.T) {
	reg := NewBlueprintRegistry()
	bp := mustPipelineBlueprint(t, WritingComposition)
	entry := BlueprintEntry{Blueprint: bp, Rubric: mustPipelineRubric(t),
		TemplateVersion: map[string]any{}, PackDigest: "sha256:pack"}

	if err := reg.Register("other-id", entry); !errors.Is(err, ErrBlueprintIDMismatch) {
		t.Errorf("id 不一致应 fail-loud，实际 %v", err)
	}
	if err := reg.Register(bp.BlueprintID, entry); err != nil {
		t.Fatalf("注册失败: %v", err)
	}
	got, err := reg.Get(bp.BlueprintID)
	if err != nil {
		t.Fatalf("取蓝图失败: %v", err)
	}
	if got.Blueprint != bp {
		t.Errorf("取回应为同一条目")
	}
	if _, err := reg.Get("nope"); !errors.Is(err, ErrBlueprintNotRegistered) {
		t.Errorf("未注册应报 ErrBlueprintNotRegistered，实际 %v", err)
	}
	reg.Reset()
	if _, err := reg.Get(bp.BlueprintID); !errors.Is(err, ErrBlueprintNotRegistered) {
		t.Errorf("Reset 后应不可取")
	}
	// 空条目 fail-loud.
	if err := reg.Register("x", BlueprintEntry{}); !errors.Is(err, ErrNilPipelinePort) {
		t.Errorf("空条目应拒绝，实际 %v", err)
	}
}

func TestRegistryConcurrent(t *testing.T) {
	// -race 纪律：注册表并发读写安全.
	reg := NewBlueprintRegistry()
	var wg sync.WaitGroup
	for i := 0; i < 8; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			bp, err := MakeBlueprint("bp-concurrent", WritingComposition, "p", "t", "r", []string{"x"}, 30, "1")
			if err != nil {
				t.Errorf("构造失败: %v", err)
				return
			}
			for j := 0; j < 50; j++ {
				if err := reg.Register(bp.BlueprintID, BlueprintEntry{Blueprint: bp}); err != nil {
					t.Errorf("注册失败: %v", err)
					return
				}
				if _, err := reg.Get(bp.BlueprintID); err != nil {
					t.Errorf("读取失败: %v", err)
					return
				}
			}
		}(i)
	}
	wg.Wait()
}

// ────────────────────────────────────────────────────────────────────
// 量规完整性验证器（验收③）
// ────────────────────────────────────────────────────────────────────

func artifactWithScoringRef(scoringRef map[string]any) *PipelineArtifact {
	return &PipelineArtifact{ItemVersion: &ItemVersion{ScoringRef: scoringRef}}
}

func TestRubricCompletenessPass(t *testing.T) {
	rubric := mustPipelineRubric(t)
	art := artifactWithScoringRef(map[string]any{
		"scorer_id":     DLineScorerID,
		"scorer_params": map[string]any{"rubric": rubric.ToScorerParams()},
	})
	result := RubricCompleteness{}.Validate(art)
	if result.Verdict != VerdictPass {
		t.Fatalf("合法量规应 pass，实际 %v：%v", result.Verdict, result.Evidence["reason"])
	}
	dims, ok := result.Evidence["dimensions"].([]any)
	if !ok || len(dims) != 4 {
		t.Errorf("evidence 应含 4 维度 id：%v", result.Evidence["dimensions"])
	}
	if result.Evidence["total_max_score"] != float64(20) {
		t.Errorf("evidence total_max_score 应为 20：%v", result.Evidence["total_max_score"])
	}
}

func TestRubricCompletenessFail(t *testing.T) {
	dim := func(anchors []any, total any) map[string]any {
		return map[string]any{
			"scorer_id": DLineScorerID,
			"scorer_params": map[string]any{"rubric": map[string]any{
				"dimensions": []any{map[string]any{
					"id": "content", "name": "内容",
					"anchors":          anchors,
					"score_bands":      []any{map[string]any{"level": 1, "label": "优秀", "score": 5}, map[string]any{"level": 2, "label": "待改进", "score": 1}},
					"error_type_rules": []any{},
				}},
				"total_max_score": total,
			}},
		}
	}
	cases := []struct {
		name    string
		payload map[string]any
		wantIn  string
	}{
		{"缺 rubric", map[string]any{"scorer_id": DLineScorerID, "scorer_params": map[string]any{}}, "缺 rubric"},
		{"缺 dimensions", map[string]any{"scorer_id": DLineScorerID, "scorer_params": map[string]any{
			"rubric": map[string]any{"dimensions": []any{}, "total_max_score": 0},
		}}, "量规结构非法"},
		{"分值合计不正确", dim([]any{"主题明确", "主题模糊"}, 10), "分值合计不正确"},
		{"等级描述为空", dim([]any{"主题明确", ""}, 5), "等级描述为空"},
		{"等级描述非字符串", dim([]any{"主题明确", nil}, 5), "等级描述为空"},
	}
	for _, tc := range cases {
		result := RubricCompleteness{}.Validate(artifactWithScoringRef(tc.payload))
		if result.Verdict != VerdictFail || !result.Blocking {
			t.Errorf("%s：应 fail 且 blocking，实际 %v", tc.name, result.Verdict)
		}
		if !strings.Contains(result.Evidence["reason"].(string), tc.wantIn) {
			t.Errorf("%s：reason 应含 %q，实际 %q", tc.name, tc.wantIn, result.Evidence["reason"])
		}
	}
	// 非 dict scoring_ref.
	result := RubricCompleteness{}.Validate(artifactWithScoringRef(nil))
	if result.Verdict != VerdictFail {
		t.Errorf("缺 scoring_ref 应 fail")
	}
	// nil artifact.
	if (RubricCompleteness{}.Validate(nil)).Verdict != VerdictFail {
		t.Errorf("nil artifact 应 fail")
	}
}

// ────────────────────────────────────────────────────────────────────
// 端到端：pass 路径（验收①②）
// ────────────────────────────────────────────────────────────────────

func TestDPipelineRunPass(t *testing.T) {
	sink := &memSink{certID: "cert-1"}
	dp, _ := newTestPipeline(t, dLineDraftGenerator(t), sink)

	res, err := dp.Run(DPipelineRequest{
		BlueprintID: "bp-d-line-composition-test",
		Params:      map[string]any{"topic": "春天"},
	})
	if err != nil {
		t.Fatalf("流水线失败: %v", err)
	}
	// 验收①：item_id 与证书 id.
	if !res.Published || res.ItemID == "" || res.CertificateID != "cert-1" {
		t.Errorf("pass 路径应入库并持证：%+v", res)
	}
	if res.FinalVerdict != VerdictPass {
		t.Errorf("final_verdict 应 pass，实际 %s", res.FinalVerdict)
	}
	if len(sink.published) != 1 {
		t.Fatalf("应恰好入库一次")
	}
	// 验收②：入库题目 scoring_ref 指向 ai_rubric，量规模板嵌入题目元数据.
	published := sink.published[0]
	if published.ScoringRef["scorer_id"] != DLineScorerID {
		t.Errorf("scoring_ref.scorer_id 应为 ai_rubric：%v", published.ScoringRef["scorer_id"])
	}
	scorerParams := published.ScoringRef["scorer_params"].(map[string]any)
	rubricPayload, ok := scorerParams["rubric"].(map[string]any)
	if !ok {
		t.Fatalf("量规应嵌入 scorer_params.rubric")
	}
	parsed, err := scoring.ParseRubric(rubricPayload)
	if err != nil || len(parsed.Dimensions) != 4 {
		t.Errorf("嵌入量规应可被评分器解析（4 维度）：%v", err)
	}
	// 阶段留痕：generate → validate → assemble 全 ok.
	if len(res.StageTraces) != 3 {
		t.Fatalf("应留痕 3 阶段：%v", res.StageTraces)
	}
	for i, want := range []string{StageGenerate, StageValidate, StageAssemble} {
		if res.StageTraces[i].Stage != want || res.StageTraces[i].Status != "ok" {
			t.Errorf("阶段 %d 应为 %s/ok，实际 %+v", i, want, res.StageTraces[i])
		}
	}
}

// ────────────────────────────────────────────────────────────────────
// 端到端：门失败路径（不入库、不签证书）
// ────────────────────────────────────────────────────────────────────

func TestDPipelineGateFail(t *testing.T) {
	sink := &memSink{certID: "cert-1"}
	failer := &capturingValidator{id: "_always_fail", verdict: VerdictFail, blocking: true}
	dp, _ := newTestPipeline(t, dLineDraftGenerator(t), sink, failer)

	res, err := dp.Run(DPipelineRequest{
		BlueprintID: "bp-d-line-composition-test",
		Params:      map[string]any{"topic": "春天"},
	})
	if err != nil {
		t.Fatalf("门失败不是异常，是判定: %v", err)
	}
	if res.Published || res.ItemID != "" || res.CertificateID != "" {
		t.Errorf("门失败不应入库：%+v", res)
	}
	if res.FinalVerdict != VerdictFail {
		t.Errorf("final_verdict 应 fail，实际 %s", res.FinalVerdict)
	}
	if len(sink.published) != 0 {
		t.Errorf("门失败不得调用入库端口")
	}
	if res.ItemVersionID == "" {
		t.Errorf("门失败仍应回 item_version_id 便于诊断")
	}
	last := res.StageTraces[len(res.StageTraces)-1]
	if last.Stage != StageAssemble || last.Status != "skipped" {
		t.Errorf("装配阶段应 skipped：%+v", last)
	}
}

func TestDPipelineNonBlockingFailIsReview(t *testing.T) {
	sink := &memSink{certID: "cert-1"}
	reviewer := &capturingValidator{id: "_nonblocking", verdict: VerdictFail, blocking: false}
	dp, _ := newTestPipeline(t, dLineDraftGenerator(t), sink, reviewer)

	res, err := dp.Run(DPipelineRequest{
		BlueprintID: "bp-d-line-composition-test",
		Params:      map[string]any{"topic": "春天"},
	})
	if err != nil {
		t.Fatalf("流水线失败: %v", err)
	}
	if res.FinalVerdict != VerdictReview || res.Published {
		t.Errorf("非阻断失败应为 review 且不入库：%+v", res)
	}
}

// ────────────────────────────────────────────────────────────────────
// 阶段显式传递与 fail-loud
// ────────────────────────────────────────────────────────────────────

func TestDPipelineExplicitArtifactPassing(t *testing.T) {
	var genSeen *ItemVersion
	gen := GeneratorFunc(func(req GenerateRequest) (*ItemVersion, error) {
		iv, err := dLineDraftGenerator(t)(req)
		if err != nil {
			return nil, err
		}
		genSeen = iv
		return iv, nil
	})
	validator := &capturingValidator{id: "capturing"}
	var sinkSeen *ItemVersion
	sink := ItemSinkFunc(func(iv *ItemVersion) (*Publication, error) {
		sinkSeen = iv
		return &Publication{ItemID: "item-x", ItemVersionID: iv.ItemVersionID, CertificateID: "cert-x"}, nil
	})
	dp, _ := newTestPipeline(t, gen, sink, validator)

	res, err := dp.Run(DPipelineRequest{
		BlueprintID: "bp-d-line-composition-test",
		Params:      map[string]any{"topic": "春天"},
		Locale:      "zh-CN",
	})
	if err != nil {
		t.Fatalf("流水线失败: %v", err)
	}
	// 显式传递：校验阶段与装配阶段看到的是生成阶段产出的同一对象，
	// 流水线不隐式重建.
	if validator.seen == nil || validator.seen.ItemVersion != genSeen {
		t.Errorf("校验阶段应看到生成阶段的同一 *ItemVersion（显式传递）")
	}
	if sinkSeen != genSeen {
		t.Errorf("装配阶段应收到同一 *ItemVersion（显式传递）")
	}
	if res.FinalArtifact != validator.seen {
		t.Errorf("结果应携带同一 artifact（显式传递可追溯）")
	}
	// 生成请求参数与校验可见参数一致（学段/参数显式流经）.
	if len(genSeen.Lineage) == 0 {
		t.Errorf("产物应携带谱系")
	}
	// Locale 显式传递（缺省 zh-CN 注入生成请求）.
	if len(validator.seen.InstantiateParams) == 0 {
		t.Errorf("实例化参数应显式流经 artifact")
	}
}

func TestDPipelineFailLoud(t *testing.T) {
	t.Run("蓝图未注册", func(t *testing.T) {
		dp, _ := newTestPipeline(t, dLineDraftGenerator(t), &memSink{})
		_, err := dp.Run(DPipelineRequest{BlueprintID: "nope", Params: map[string]any{"topic": "x"}})
		if !errors.Is(err, ErrBlueprintNotRegistered) {
			t.Errorf("期望 ErrBlueprintNotRegistered，实际 %v", err)
		}
	})

	t.Run("生成阶段失败上抛并留痕", func(t *testing.T) {
		gen := &memGenerator{err: context.DeadlineExceeded}
		sink := &memSink{}
		dp, _ := newTestPipeline(t, gen, sink)
		_, err := dp.Run(DPipelineRequest{
			BlueprintID: "bp-d-line-composition-test",
			Params:      map[string]any{"topic": "x"},
		})
		if err == nil || !errors.Is(err, context.DeadlineExceeded) {
			t.Fatalf("生成阶段错误应上抛，实际 %v", err)
		}
		if !strings.Contains(err.Error(), StageGenerate) {
			t.Errorf("错误应指认 generate 阶段：%v", err)
		}
		if len(sink.published) != 0 {
			t.Errorf("生成失败不得入库")
		}
	})

	t.Run("入库阶段失败上抛", func(t *testing.T) {
		sink := &memSink{err: errors.New("db down")}
		dp, _ := newTestPipeline(t, dLineDraftGenerator(t), sink)
		_, err := dp.Run(DPipelineRequest{
			BlueprintID: "bp-d-line-composition-test",
			Params:      map[string]any{"topic": "x"},
		})
		if err == nil || !strings.Contains(err.Error(), StageAssemble) {
			t.Fatalf("入库错误应上抛并指认 assemble 阶段，实际 %v", err)
		}
	})

	t.Run("composition 缺 topic", func(t *testing.T) {
		dp, _ := newTestPipeline(t, dLineDraftGenerator(t), &memSink{})
		_, err := dp.Run(DPipelineRequest{BlueprintID: "bp-d-line-composition-test", Params: map[string]any{}})
		if !errors.Is(err, ErrInvalidPipelineParams) {
			t.Errorf("期望 ErrInvalidPipelineParams，实际 %v", err)
		}
	})

	t.Run("nil 端口构造拒绝", func(t *testing.T) {
		if _, err := NewDPipeline(nil, dLineDraftGenerator(t), &memSink{}); !errors.Is(err, ErrNilPipelinePort) {
			t.Errorf("nil registry 应拒绝")
		}
		reg := NewBlueprintRegistry()
		if _, err := NewDPipeline(reg, nil, &memSink{}); !errors.Is(err, ErrNilPipelinePort) {
			t.Errorf("nil generator 应拒绝")
		}
		if _, err := NewDPipeline(reg, dLineDraftGenerator(t), nil); !errors.Is(err, ErrNilPipelinePort) {
			t.Errorf("nil sink 应拒绝")
		}
	})
}

func TestDPipelineGradeBandAndPictureWriting(t *testing.T) {
	reg := NewBlueprintRegistry()
	bp := mustPipelineBlueprint(t, WritingPicture)
	bp.BlueprintID = "bp-d-line-picture-test"
	if err := reg.Register(bp.BlueprintID, BlueprintEntry{
		Blueprint: bp, Rubric: mustPipelineRubric(t),
		TemplateVersion: map[string]any{}, PackDigest: "sha256:pack",
	}); err != nil {
		t.Fatalf("注册失败: %v", err)
	}
	var gotSpec GradeBandSpec
	gen := GeneratorFunc(func(req GenerateRequest) (*ItemVersion, error) {
		gotSpec = req.Spec
		if req.InstantiateParams["picture_ref"] != "minio:pictures/1" || req.InstantiateParams["prompt"] != "看图写话" {
			t.Errorf("picture_writing 实例化参数不符：%v", req.InstantiateParams)
		}
		return dLineDraftGenerator(t)(req)
	})
	dp, err := NewDPipeline(reg, gen, ItemSinkFunc(func(iv *ItemVersion) (*Publication, error) {
		return &Publication{ItemID: "item-p", ItemVersionID: iv.ItemVersionID, CertificateID: "cert-p"}, nil
	}))
	if err != nil {
		t.Fatalf("构造失败: %v", err)
	}
	// params 内 grade_band 覆盖缺省 M → H 段字数区间.
	res, err := dp.Run(DPipelineRequest{
		BlueprintID: "bp-d-line-picture-test",
		Params:      map[string]any{"picture_ref": "minio:pictures/1", "prompt": "看图写话", "grade_band": GradebandH},
	})
	if err != nil {
		t.Fatalf("流水线失败: %v", err)
	}
	if !res.Published {
		t.Errorf("应入库")
	}
	if gotSpec.GradeBand != GradebandH || gotSpec.WordCountMin != 300 || gotSpec.WordCountMax != 400 {
		t.Errorf("应按 params.grade_band=H 取字数区间，实际 %+v", gotSpec)
	}
}
