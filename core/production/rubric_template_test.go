// rubric_template_test.go 量规模板 Go 移植的验收测试（对照冻结
// tests/unit/test_blueprint_schema.py 的 TestRubricTemplate /
// TestRubricSerialization；验收②③：维度齐全/描述非空/分值合计正确/
// 可序列化为评分器输入）。
package production

import (
	"encoding/json"
	"errors"
	"reflect"
	"testing"
)

// mustRubric 构造一份四维量规（内容/结构/语言/书写，各 5 分满分，3 档；
// 与冻结测试 _make_rubric 同构）.
func mustRubric(t *testing.T, gradeBand string) *RubricTemplate {
	t.Helper()
	rubric, err := NewRubricTemplate(RubricTemplateInput{
		RubricID:  "sha256:test-rubric-composition-" + gradeBand + "-v1",
		Name:      "作文量规-" + gradeBand + "段",
		GradeBand: gradeBand,
		Dimensions: []RubricDimension{
			{
				ID: "content", Name: "内容", MaxScore: 5,
				Levels: []RubricLevel{
					{Level: 1, Label: "优秀", Description: "主题明确，内容充实具体", Score: 5},
					{Level: 2, Label: "合格", Description: "主题基本明确，内容较具体", Score: 3},
					{Level: 3, Label: "待改进", Description: "主题模糊或内容空泛", Score: 1},
				},
			},
			{
				ID: "structure", Name: "结构", MaxScore: 5,
				Levels: []RubricLevel{
					{Level: 1, Label: "优秀", Description: "段落清晰，过渡自然", Score: 5},
					{Level: 2, Label: "合格", Description: "段落较清晰，过渡略显生硬", Score: 3},
					{Level: 3, Label: "待改进", Description: "段落混乱，无过渡", Score: 1},
				},
			},
			{
				ID: "language", Name: "语言", MaxScore: 5,
				Levels: []RubricLevel{
					{Level: 1, Label: "优秀", Description: "语句通顺，用词准确丰富", Score: 5},
					{Level: 2, Label: "合格", Description: "语句基本通顺，用词一般", Score: 3},
					{Level: 3, Label: "待改进", Description: "语句不通，用词不当", Score: 1},
				},
			},
			{
				ID: "handwriting", Name: "书写", MaxScore: 5,
				Levels: []RubricLevel{
					{Level: 1, Label: "优秀", Description: "字迹工整，无错别字", Score: 5},
					{Level: 2, Label: "合格", Description: "字迹较工整，偶有错别字", Score: 3},
					{Level: 3, Label: "待改进", Description: "字迹潦草，错别字多", Score: 1},
				},
			},
		},
		TotalMaxScore: 20,
		Version:       "1.0.0",
	})
	if err != nil {
		t.Fatalf("NewRubricTemplate 失败: %v", err)
	}
	return rubric
}

func TestRubricTemplateValid(t *testing.T) {
	for _, band := range []string{GradebandL, GradebandM, GradebandH} {
		rubric := mustRubric(t, band)
		if rubric.GradeBand != band {
			t.Errorf("学段覆盖标记应为 %s", band)
		}
	}
	rubric := mustRubric(t, GradebandM)
	if len(rubric.Dimensions) != 4 {
		t.Fatalf("应含 4 维度")
	}
	for _, dim := range rubric.Dimensions {
		if dim.Name == "" || len(dim.Levels) < 2 {
			t.Fatalf("维度 %s 应含名称与 ≥2 档", dim.ID)
		}
		for _, lvl := range dim.Levels {
			if lvl.Description == "" {
				t.Errorf("维度 %s 等级 %d 描述应非空（验收②）", dim.ID, lvl.Level)
			}
		}
	}
}

func TestRubricTemplateInvalid(t *testing.T) {
	dim := func(mutate func(*RubricDimension)) RubricDimension {
		d := RubricDimension{
			ID: "x", Name: "X", MaxScore: 5,
			Levels: []RubricLevel{
				{Level: 1, Label: "a", Description: "d1", Score: 5},
				{Level: 2, Label: "b", Description: "d2", Score: 3},
			},
		}
		mutate(&d)
		return d
	}
	cases := []struct {
		name   string
		input  RubricTemplateInput
		mutate func(*RubricTemplateInput)
	}{
		{"分值合计不匹配（验收③）", RubricTemplateInput{
			RubricID: "r1", Name: "bad", GradeBand: GradebandM,
			Dimensions: []RubricDimension{dim(func(*RubricDimension) {})},
		}, func(in *RubricTemplateInput) { in.TotalMaxScore = 99; in.Version = "1" }},
		{"维度 id 重复", RubricTemplateInput{
			RubricID: "r1", Name: "dup", GradeBand: GradebandM, TotalMaxScore: 10, Version: "1",
			Dimensions: []RubricDimension{
				dim(func(*RubricDimension) {}),
				dim(func(d *RubricDimension) { d.Name = "Y" }),
			},
		}, nil},
		{"维度 max_score 不等于最高档", RubricTemplateInput{
			RubricID: "r1", Name: "bad", GradeBand: GradebandM, TotalMaxScore: 10, Version: "1",
			Dimensions: []RubricDimension{dim(func(d *RubricDimension) { d.MaxScore = 10 })},
		}, nil},
		{"等级数 <2", RubricTemplateInput{
			RubricID: "r1", Name: "bad", GradeBand: GradebandM, TotalMaxScore: 5, Version: "1",
			Dimensions: []RubricDimension{dim(func(d *RubricDimension) {
				d.Levels = d.Levels[:1]
			})},
		}, nil},
		{"等级 level 重复", RubricTemplateInput{
			RubricID: "r1", Name: "bad", GradeBand: GradebandM, TotalMaxScore: 5, Version: "1",
			Dimensions: []RubricDimension{dim(func(d *RubricDimension) { d.Levels[1].Level = 1 })},
		}, nil},
		{"等级描述为空（验收②）", RubricTemplateInput{
			RubricID: "r1", Name: "bad", GradeBand: GradebandM, TotalMaxScore: 5, Version: "1",
			Dimensions: []RubricDimension{dim(func(d *RubricDimension) { d.Levels[1].Description = "" })},
		}, nil},
		{"学段越域", RubricTemplateInput{
			RubricID: "r1", Name: "bad", TotalMaxScore: 5, Version: "1",
			GradeBand:  "X",
			Dimensions: []RubricDimension{dim(func(*RubricDimension) {})},
		}, nil},
		{"维度为空", RubricTemplateInput{
			RubricID: "r1", Name: "bad", GradeBand: GradebandM, TotalMaxScore: 0, Version: "1",
		}, nil},
	}
	for _, tc := range cases {
		in := tc.input
		if tc.mutate != nil {
			tc.mutate(&in)
		}
		if _, err := NewRubricTemplate(in); !errors.Is(err, ErrInvalidRubric) {
			t.Errorf("%s：期望 ErrInvalidRubric，实际 %v", tc.name, err)
		}
	}
}

func TestToScorerParams(t *testing.T) {
	rubric := mustRubric(t, GradebandM)
	params := rubric.ToScorerParams()

	dims, ok := params["dimensions"].([]any)
	if !ok || len(dims) != 4 {
		t.Fatalf("dimensions 应为 4 元素数组")
	}
	var contentDim map[string]any
	for _, raw := range dims {
		d := raw.(map[string]any)
		for _, key := range []string{"id", "name", "anchors", "score_bands", "error_type_rules"} {
			if _, ok := d[key]; !ok {
				t.Errorf("维度缺契约字段 %s（scorer.yaml ai_rubric.params_schema.rubric）", key)
			}
		}
		if d["id"] == "content" {
			contentDim = d
		}
	}
	if contentDim == nil {
		t.Fatalf("缺 content 维度")
	}

	// anchors ← levels[].description（按 level 升序）.
	wantAnchors := []any{"主题明确，内容充实具体", "主题基本明确，内容较具体", "主题模糊或内容空泛"}
	if !reflect.DeepEqual(contentDim["anchors"], wantAnchors) {
		t.Errorf("anchors 升序不符：%v", contentDim["anchors"])
	}

	// score_bands 含 level/label/score（评分器落档用）.
	bands := contentDim["score_bands"].([]any)
	if len(bands) != 3 {
		t.Fatalf("content score_bands 应 3 档")
	}
	top := bands[0].(map[string]any)
	if top["level"] != 1 || top["label"] != "优秀" || top["score"] != float64(5) {
		t.Errorf("最高档 band 不符：%v", top)
	}
	if params["total_max_score"] != float64(20) {
		t.Errorf("total_max_score 应为 20：%v", params["total_max_score"])
	}
}

func TestToScorerParamsJSONRoundtrip(t *testing.T) {
	rubric := mustRubric(t, GradebandM)
	params := rubric.ToScorerParams()
	// 可 JSON 序列化且往返稳定（验收③：可序列化为 JSON 被评分器解析）.
	first, err := json.Marshal(params)
	if err != nil {
		t.Fatalf("序列化失败: %v", err)
	}
	var restored any
	if err := json.Unmarshal(first, &restored); err != nil {
		t.Fatalf("反序列化失败: %v", err)
	}
	second, err := json.Marshal(restored)
	if err != nil {
		t.Fatalf("再序列化失败: %v", err)
	}
	if string(first) != string(second) {
		t.Errorf("JSON 往返不稳定")
	}
}

func TestRubricLevelsUnorderedStillSorted(t *testing.T) {
	// levels 传入乱序：构造期不强制排序，ToScorerParams 按 level 升序输出.
	rubric, err := NewRubricTemplate(RubricTemplateInput{
		RubricID: "r", Name: "n", GradeBand: GradebandL, TotalMaxScore: 5, Version: "1",
		Dimensions: []RubricDimension{{
			ID: "x", Name: "X", MaxScore: 5,
			Levels: []RubricLevel{
				{Level: 2, Label: "合格", Description: "d2", Score: 3},
				{Level: 1, Label: "优秀", Description: "d1", Score: 5},
			},
		}},
	})
	if err != nil {
		t.Fatalf("乱序 levels 应可构造: %v", err)
	}
	params := rubric.ToScorerParams()
	dim := params["dimensions"].([]any)[0].(map[string]any)
	if !reflect.DeepEqual(dim["anchors"], []any{"d1", "d2"}) {
		t.Errorf("anchors 应按 level 升序：%v", dim["anchors"])
	}
}
