// blueprint_test.go 命题蓝图 schema Go 移植的验收测试（对照冻结
// tests/unit/test_blueprint_schema.py 的 TestBlueprint / TestRubricTemplate）。
package production

import (
	"errors"
	"reflect"
	"testing"
)

func mustBlueprint(t *testing.T, writingType string) *Blueprint {
	t.Helper()
	bp, err := MakeBlueprint(
		"sha256:test-blueprint-composition-v1",
		writingType,
		"subject-chinese",
		"sha256:tpl-chinese-composition-v1",
		"sha256:test-rubric-composition-M-v1",
		[]string{"春天", "我的好朋友", "一次难忘的旅行"},
		30,
		"",
	)
	if err != nil {
		t.Fatalf("MakeBlueprint 失败: %v", err)
	}
	return bp
}

func TestMakeBlueprintDefaults(t *testing.T) {
	bp := mustBlueprint(t, WritingComposition)
	// 验收①：写作类型/学段/主题池/字数区间/时间限制/量规模板引用齐全.
	if bp.WritingType != WritingComposition || bp.PackID != "subject-chinese" ||
		bp.RubricTemplateID == "" || bp.TemplateVersionID == "" || bp.TimeLimitMinutes < 1 {
		t.Fatalf("蓝图必填字段不齐全: %+v", bp)
	}
	if len(bp.GradeBandSpecs) != 3 {
		t.Fatalf("应含三学段 specs，实际 %d", len(bp.GradeBandSpecs))
	}
	if bp.Version != "1" {
		t.Errorf("version 空串应缺省 \"1\"，实际 %q", bp.Version)
	}
	// 验收③：低段50-100/中段150-250/高段300-400.
	byBand := map[string]GradeBandSpec{}
	for _, spec := range bp.GradeBandSpecs {
		byBand[spec.GradeBand] = spec
	}
	want := map[string][4]any{
		GradebandL: {50, 100, 20, 0.8},
		GradebandM: {150, 250, 30, 0.6},
		GradebandH: {300, 400, 40, 0.5},
	}
	for band, w := range want {
		spec, ok := byBand[band]
		if !ok {
			t.Fatalf("缺学段 %s spec", band)
		}
		if spec.WordCountMin != w[0] || spec.WordCountMax != w[1] || spec.TimeLimitMinutes != w[2] {
			t.Errorf("学段 %s 默认参数不符: %+v", band, spec)
		}
		if spec.RubricLeniency != w[3] {
			t.Errorf("学段 %s 默认宽松度不符: %v", band, spec.RubricLeniency)
		}
	}
}

func TestNewBlueprintValidation(t *testing.T) {
	validSpecs := []GradeBandSpec{
		{GradeBand: GradebandL, WordCountMin: 50, WordCountMax: 100, TimeLimitMinutes: 20, RubricLeniency: 0.8},
		{GradeBand: GradebandM, WordCountMin: 150, WordCountMax: 250, TimeLimitMinutes: 30, RubricLeniency: 0.6},
		{GradeBand: GradebandH, WordCountMin: 300, WordCountMax: 400, TimeLimitMinutes: 40, RubricLeniency: 0.5},
	}
	base := BlueprintInput{
		BlueprintID: "b1", WritingType: WritingComposition, PackID: "p",
		TemplateVersionID: "t", RubricTemplateID: "r",
		GradeBandSpecs: validSpecs, TopicPool: []string{"t1"},
		TimeLimitMinutes: 30, Version: "1",
	}

	cases := []struct {
		name   string
		mutate func(in BlueprintInput) BlueprintInput
	}{
		{"学段 specs 未覆盖 L/M/H", func(in BlueprintInput) BlueprintInput {
			in.GradeBandSpecs = validSpecs[:1]
			return in
		}},
		{"学段 specs 重复", func(in BlueprintInput) BlueprintInput {
			in.GradeBandSpecs = append(append([]GradeBandSpec(nil), validSpecs...), validSpecs[0])
			return in
		}},
		{"字数下限不严格小于上限", func(in BlueprintInput) BlueprintInput {
			specs := append([]GradeBandSpec(nil), validSpecs...)
			specs[0].WordCountMin = 100
			specs[0].WordCountMax = 100
			in.GradeBandSpecs = specs
			return in
		}},
		{"写作类型越域", func(in BlueprintInput) BlueprintInput {
			in.WritingType = "invalid"
			return in
		}},
		{"主题池为空", func(in BlueprintInput) BlueprintInput {
			in.TopicPool = nil
			return in
		}},
		{"时长越域", func(in BlueprintInput) BlueprintInput {
			in.TimeLimitMinutes = 0
			return in
		}},
		{"宽松度越域", func(in BlueprintInput) BlueprintInput {
			specs := append([]GradeBandSpec(nil), validSpecs...)
			specs[0].RubricLeniency = 1.5
			in.GradeBandSpecs = specs
			return in
		}},
		{"标识缺失", func(in BlueprintInput) BlueprintInput {
			in.RubricTemplateID = ""
			return in
		}},
		{"版本缺失", func(in BlueprintInput) BlueprintInput {
			in.Version = ""
			return in
		}},
	}
	for _, tc := range cases {
		_, err := NewBlueprint(tc.mutate(base))
		if !errors.Is(err, ErrInvalidBlueprint) {
			t.Errorf("%s：期望 ErrInvalidBlueprint，实际 %v", tc.name, err)
		}
	}

	// 正例：合法输入通过.
	bp, err := NewBlueprint(base)
	if err != nil {
		t.Fatalf("合法蓝图应通过: %v", err)
	}
	if !reflect.DeepEqual(bp.GradeBandSpecs, validSpecs) {
		t.Errorf("specs 应保序保值")
	}
}

func TestBlueprintSpecFor(t *testing.T) {
	bp := mustBlueprint(t, WritingComposition)
	spec, err := bp.SpecFor(GradebandH)
	if err != nil {
		t.Fatalf("取 H 段 spec 失败: %v", err)
	}
	if spec.WordCountMin != 300 || spec.WordCountMax != 400 {
		t.Errorf("H 段字数区间不符: %+v", spec)
	}
	if _, err := bp.SpecFor("X"); !errors.Is(err, ErrInvalidBlueprint) {
		t.Errorf("缺学段 spec 应报 ErrInvalidBlueprint，实际 %v", err)
	}
}

func TestBlueprintWritingTypeDomain(t *testing.T) {
	if _, err := MakeBlueprint("b", "picture_writing", "p", "t", "r", []string{"x"}, 30, ""); err != nil {
		t.Errorf("picture_writing 应合法: %v", err)
	}
	if _, err := MakeBlueprint("b", "diary", "p", "t", "r", []string{"x"}, 30, ""); !errors.Is(err, ErrInvalidBlueprint) {
		t.Errorf("越域 writing_type 应拒绝，实际 %v", err)
	}
}
