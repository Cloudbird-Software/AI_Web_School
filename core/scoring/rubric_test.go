package scoring

import (
	"strings"
	"testing"
)

// 量规解析套件（T-W4-019 语义同构验证）：
// - 结构校验全分支（缺 dimensions/缺 id/name/空 anchors/空 bands/超上限/非数值）；
// - prompt 构建含量规全要素与学段上下文；
// - AI 响应解析容错（围栏/漏评/clamp/理由缺失降置信/解析失败零分复核）。

func sampleRubric() map[string]any {
	return map[string]any{
		"rubric_id": "rub-test-1",
		"dimensions": []any{
			map[string]any{
				"id": "content", "name": "内容",
				"anchors":     []any{"中心明确", "偏离题意"},
				"score_bands": []any{map[string]any{"level": 1.0, "label": "优秀", "score": 5.0}, map[string]any{"level": 2.0, "label": "待提高", "score": 1.0}},
			},
			map[string]any{
				"id": "language", "name": "语言",
				"anchors":     []any{"语句通顺"},
				"score_bands": []any{map[string]any{"level": 1.0, "label": "良好", "score": 3.0}},
			},
		},
	}
}

// TestParseRubric 结构校验与派生值.
func TestParseRubric(t *testing.T) {
	parsed, err := ParseRubric(sampleRubric())
	if err != nil {
		t.Fatal(err)
	}
	if len(parsed.Dimensions) != 2 {
		t.Fatalf("维度数=%d", len(parsed.Dimensions))
	}
	if parsed.Dimensions[0].MaxScore != 5 {
		t.Fatalf("max_score 应取分值带上界: %v", parsed.Dimensions[0].MaxScore)
	}
	if parsed.TotalMaxScore != 8 {
		t.Fatalf("total_max_score 应为维度合计 8: %v", parsed.TotalMaxScore)
	}
	if parsed.Dimensions[0].Anchors[1] != "偏离题意" {
		t.Fatalf("anchors 投影失真: %v", parsed.Dimensions[0].Anchors)
	}

	// 显式 total_max_score 覆盖维度合计.
	rubric := sampleRubric()
	rubric["total_max_score"] = 10.0
	parsed2, err := ParseRubric(rubric)
	if err != nil || parsed2.TotalMaxScore != 10 {
		t.Fatalf("total_max_score 覆盖失败: %v %v", parsed2, err)
	}
}

// TestParseRubricErrors 结构非法全分支 fail-loud.
func TestParseRubricErrors(t *testing.T) {
	cases := []struct {
		name   string
		mutate func(map[string]any)
	}{
		{"缺 dimensions", func(r map[string]any) { delete(r, "dimensions") }},
		{"dimensions 为空数组", func(r map[string]any) { r["dimensions"] = []any{} }},
		{"维度数超上限", func(r map[string]any) {
			dims := make([]any, MaxRubricDimensions+1)
			for i := range dims {
				dims[i] = map[string]any{"id": "d", "name": "n", "anchors": []any{"a"}, "score_bands": []any{map[string]any{"score": 1.0}}}
			}
			r["dimensions"] = dims
		}},
		{"维度非 object", func(r map[string]any) { r["dimensions"] = []any{"x"} }},
		{"缺 id", func(r map[string]any) { delete(r["dimensions"].([]any)[0].(map[string]any), "id") }},
		{"缺 name", func(r map[string]any) { delete(r["dimensions"].([]any)[0].(map[string]any), "name") }},
		{"anchors 为空", func(r map[string]any) { r["dimensions"].([]any)[0].(map[string]any)["anchors"] = []any{} }},
		{"score_bands 为空", func(r map[string]any) { r["dimensions"].([]any)[0].(map[string]any)["score_bands"] = []any{} }},
		{"band score 非数值", func(r map[string]any) {
			r["dimensions"].([]any)[0].(map[string]any)["score_bands"] = []any{map[string]any{"score": "高分"}}
		}},
		{"total_max_score 非数值", func(r map[string]any) { r["total_max_score"] = "十分" }},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			rubric := sampleRubric()
			tc.mutate(rubric)
			if _, err := ParseRubric(rubric); err == nil {
				t.Fatal("结构非法必须报错")
			}
		})
	}
}

// TestBuildScoringPrompt prompt 全要素（量规/学段/作答/输出约束）.
func TestBuildScoringPrompt(t *testing.T) {
	parsed, err := ParseRubric(sampleRubric())
	if err != nil {
		t.Fatal(err)
	}
	prompt := BuildScoringPrompt("春天来了，花开了。", parsed, "L")
	for _, want := range []string{
		"你是小学语文作文/看图写话评分器",
		"【学段】低段（小学 1-2 年级）",
		"维度「内容」（id=content，满分 5）",
		"等级1（优秀，5分）：中心明确",
		"维度「语言」（id=language，满分 3）",
		"【学生作答】",
		"春天来了，花开了。",
		`{"id": "<维度id>", "score": <分数>, "rationale": "<理由>", "confidence": <0-1>}`,
	} {
		if !strings.Contains(prompt, want) {
			t.Fatalf("prompt 缺要素 %q:\n%s", want, prompt)
		}
	}
	// 未知学段原样透传（Python str(band) 兜底同构）.
	if !strings.Contains(BuildScoringPrompt("x", parsed, "P"), "【学段】P") {
		t.Fatal("未知学段应透传")
	}
}

// TestParseAIResponse 容错解析全路径.
func TestParseAIResponse(t *testing.T) {
	parsed, err := ParseRubric(sampleRubric())
	if err != nil {
		t.Fatal(err)
	}

	t.Run("干净 JSON 全维命中", func(t *testing.T) {
		score := ParseAIResponse(`{"dimensions":[{"id":"content","score":4,"rationale":"扣题","confidence":0.9},{"id":"language","score":3,"rationale":"通顺","confidence":0.8}]}`, parsed)
		if score.TotalScore != 7 || score.OverallConfidence != 0.8 || score.NeedsHumanReview {
			t.Fatalf("score=%+v", score)
		}
		if score.Dimensions[0].Name != "内容" || score.Dimensions[0].Max != 5 {
			t.Fatalf("维度投影失真: %+v", score.Dimensions[0])
		}
	})
	t.Run("markdown 围栏容错", func(t *testing.T) {
		content := "```json\n{\"dimensions\":[{\"id\":\"content\",\"score\":5,\"rationale\":\"好\",\"confidence\":1.0},{\"id\":\"language\",\"score\":3,\"rationale\":\"佳\",\"confidence\":0.9}]}\n```"
		score := ParseAIResponse(content, parsed)
		if score.TotalScore != 8 || score.NeedsHumanReview {
			t.Fatalf("score=%+v", score)
		}
	})
	t.Run("漏评维度零分零置信并转复核", func(t *testing.T) {
		score := ParseAIResponse(`{"dimensions":[{"id":"content","score":5,"rationale":"好","confidence":1.0}]}`, parsed)
		if score.Dimensions[1].Score != 0 || score.Dimensions[1].Confidence != 0 {
			t.Fatalf("漏评维度应零分零置信: %+v", score.Dimensions[1])
		}
		if score.OverallConfidence != 0 || !score.NeedsHumanReview {
			t.Fatalf("score=%+v", score)
		}
	})
	t.Run("score 越带 clamp", func(t *testing.T) {
		score := ParseAIResponse(`{"dimensions":[{"id":"language","score":99,"rationale":"r","confidence":0.9}]}`, parsed)
		if score.Dimensions[1].Score != 3 {
			t.Fatalf("应 clamp 到分值带上界 3: %+v", score.Dimensions[1])
		}
	})
	t.Run("空理由占位并降置信", func(t *testing.T) {
		score := ParseAIResponse(`{"dimensions":[{"id":"content","score":5,"rationale":"  ","confidence":0.9}]}`, parsed)
		d := score.Dimensions[0]
		if d.Rationale != "AI 未给出理由（rationale 为空）" || d.Confidence != 0.3 {
			t.Fatalf("d=%+v", d)
		}
	})
	t.Run("confidence 越界 clamp", func(t *testing.T) {
		score := ParseAIResponse(`{"dimensions":[{"id":"content","score":5,"rationale":"r","confidence":7}]}`, parsed)
		if score.Dimensions[0].Confidence != 1.0 {
			t.Fatalf("confidence=%v", score.Dimensions[0].Confidence)
		}
	})
	t.Run("完全解析失败零分转复核", func(t *testing.T) {
		score := ParseAIResponse("我不会输出 JSON", parsed)
		if score.TotalScore != 0 || score.OverallConfidence != 0 || !score.NeedsHumanReview {
			t.Fatalf("score=%+v", score)
		}
		if len(score.Dimensions) != 2 {
			t.Fatalf("零分结果应逐维铺开: %d", len(score.Dimensions))
		}
	})
	t.Run("顶层非 object 不重试提取（Python 同构）", func(t *testing.T) {
		score := ParseAIResponse(`[{"dimensions":[]}]`, parsed)
		if !score.NeedsHumanReview || score.TotalScore != 0 {
			t.Fatalf("score=%+v", score)
		}
	})
}

// TestRubricID 影子账量规 id 缺省.
func TestRubricID(t *testing.T) {
	if got := RubricID(sampleRubric()); got != "rub-test-1" {
		t.Fatalf("rubric_id=%q", got)
	}
	if got := RubricID(map[string]any{}); got != "ad-hoc-rubric" {
		t.Fatalf("缺省 rubric_id=%q", got)
	}
}
