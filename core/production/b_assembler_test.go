// b_assembler_test.go B 线装配方 Go 移植的验收测试（对照冻结
// tests/unit/test_b_assembler.py + tests/golden/b_line/unit_conversion.yaml）。
//
// 测试策略：纯函数内核，无 DB。item_version_id 为与 Python 冻结实现交叉
// 验证的地面真值——运行冻结实现（pydantic 2.13.4 环境，yaml 黄金样例 +
// 固定 signed_at）对相同 fixture 采样后固化于此。
package production

import (
	"errors"
	"reflect"
	"strings"
	"testing"
)

// ────────────────────────────────────────────────────────────────────
// 黄金样例（tests/golden/b_line/unit_conversion.yaml 同构内联）
// ────────────────────────────────────────────────────────────────────

func goldenTemplateMap() map[string]any {
	return map[string]any{
		"template_id":      "tpl-unit-conversion-v1",
		"template_version": "1.0.0",
		"pack_id":          "subject-math",
		"description":      "B 线单位换算题型框架模板（T-W2-017 实证）",
		"slots": []any{
			map[string]any{"name": "value", "type": "number", "required": true, "description": "待换算数值"},
			map[string]any{"name": "from_unit", "type": "string", "required": true, "description": "源单位（如 km / hour / g）"},
			map[string]any{"name": "to_unit", "type": "string", "required": true, "description": "目标单位（如 m / minute / kg）"},
			map[string]any{"name": "answer", "type": "number", "required": true, "description": "正确答案（数值）"},
		},
		"presentation": []any{
			map[string]any{"type": "text", "template": "把 {value} {from_unit} 换算成 {to_unit}（结果保留两位小数）。"},
		},
		"objective": map[string]any{
			"kp_set":          []any{map[string]any{"dimension": "kp", "code": "math.nal.unit_conversion"}},
			"kp_set_mode":     "single",
			"cognitive_level": "apply",
			"gradeband":       "M",
			"graph_release":   "2026.1",
		},
		"interaction_ref": map[string]any{
			"interaction_id":     "numeric_blank",
			"interaction_params": map[string]any{"placeholder": "数值", "precision": 2},
		},
		"scoring_ref": map[string]any{
			"scorer_id":     "exact_match",
			"scorer_params": map[string]any{"answer_key": "{answer}", "tolerance": 0.01},
		},
		"error_bindings": []any{
			map[string]any{"error_type": "math.unit.scale_wrong", "description": "量级错误（如 km→m 时除以 1000 而非乘 1000）"},
			map[string]any{"error_type": "math.unit.cross_kind", "description": "跨类换算（如 km→kg）"},
		},
	}
}

func mustGoldenTemplate(t *testing.T) *FrameworkTemplate {
	t.Helper()
	tpl, err := NewFrameworkTemplate(goldenTemplateMap())
	if err != nil {
		t.Fatalf("NewFrameworkTemplate 失败: %v", err)
	}
	return tpl
}

func goldenCorpusRefs() []CorpusRef {
	return []CorpusRef{{CorpusVersionID: "sha256:math-functions-v1", Digest: "sha256:math-functions-v1"}}
}

// goldenCases 黄金样例 3 组 (params, expected_value)；数值字面量保持 yaml
// 装载类型（int/float64 分明——公式二规范化对 int 与 float 渲染不同）.
func goldenCases() []struct {
	caseID       string
	params       map[string]any
	expectedText string
} {
	return []struct {
		caseID       string
		params       map[string]any
		expectedText string
	}{
		{"unit_km_to_m", map[string]any{"value": 1.5, "from_unit": "km", "to_unit": "m", "answer": 1500},
			"把 1.5 km 换算成 m（结果保留两位小数）。"},
		{"unit_hour_to_min", map[string]any{"value": 2, "from_unit": "hour", "to_unit": "minute", "answer": 120},
			"把 2 hour 换算成 minute（结果保留两位小数）。"},
		{"unit_g_to_kg", map[string]any{"value": 500, "from_unit": "g", "to_unit": "kg", "answer": 0.5},
			"把 500 g 换算成 kg（结果保留两位小数）。"},
	}
}

// ────────────────────────────────────────────────────────────────────
// 与冻结实现逐字节互验的地面真值（Python assemble 同参采样）
// ────────────────────────────────────────────────────────────────────

var goldenItemVersionIDs = map[string]string{
	"unit_km_to_m":     "sha256:a0f53cb983052e9e22440cb668d64da1d2740e859ae24debeb03df4c574cfc26",
	"unit_hour_to_min": "sha256:1bf4c8308ce602ff11746f4f902ca710a59cd62fd217d628ca578921ed24ce96",
	"unit_g_to_kg":     "sha256:9bdba73d9bcc65a720cc349057b80f521e69bb1db38beb3d1ec6f556b628647f",
}

func TestAssembleGoldenCrossVerification(t *testing.T) {
	tpl := mustGoldenTemplate(t)
	for _, tc := range goldenCases() {
		iv, err := Assemble(tpl, goldenCorpusRefs(), tc.params, AssembleOptions{SignedAt: "2026-07-27T00:00:00+00:00"})
		if err != nil {
			t.Fatalf("案例 %s 装配失败: %v", tc.caseID, err)
		}
		want, ok := goldenItemVersionIDs[tc.caseID]
		if !ok {
			t.Fatalf("缺案例 %s 的地面真值", tc.caseID)
		}
		if iv.ItemVersionID != want {
			t.Errorf("案例 %s item_version_id 与冻结实现不一致：got %s want %s", tc.caseID, iv.ItemVersionID, want)
		}
	}
}

func TestAssembleReturnsItemVersion(t *testing.T) {
	tpl := mustGoldenTemplate(t)
	iv, err := Assemble(tpl, goldenCorpusRefs(), goldenCases()[0].params,
		AssembleOptions{SignedAt: "2026-07-27T00:00:00+00:00"})
	if err != nil {
		t.Fatalf("装配失败: %v", err)
	}
	// 六大块齐全 + 状态.
	for _, field := range []string{"objective", "interaction_ref", "content", "scoring_ref", "error_bindings", "lineage"} {
		if _, ok := iv.ToMap()[field]; !ok {
			t.Errorf("ItemVersion 缺 %s 块", field)
		}
	}
	if iv.Status != "draft" {
		t.Errorf("status 应为 draft，实际 %q", iv.Status)
	}
	// B 级自引用：item_id = item_version_id.
	if iv.ItemID != iv.ItemVersionID {
		t.Errorf("B 级 item_id 应自引用 item_version_id：%q != %q", iv.ItemID, iv.ItemVersionID)
	}
	if !strings.HasPrefix(iv.ItemVersionID, "sha256:") {
		t.Errorf("item_version_id 应带 sha256: 前缀，实际 %q", iv.ItemVersionID)
	}
}

// ────────────────────────────────────────────────────────────────────
// D3 确定性：同蓝图同参数同输出
// ────────────────────────────────────────────────────────────────────

func TestAssembleDeterministic(t *testing.T) {
	tpl := mustGoldenTemplate(t)
	params := goldenCases()[0].params
	opts := AssembleOptions{SignedAt: "2026-07-27T00:00:00+00:00"}
	iv1, err := Assemble(tpl, goldenCorpusRefs(), params, opts)
	if err != nil {
		t.Fatalf("第一次装配失败: %v", err)
	}
	iv2, err := Assemble(tpl, goldenCorpusRefs(), params, opts)
	if err != nil {
		t.Fatalf("第二次装配失败: %v", err)
	}
	if !reflect.DeepEqual(iv1, iv2) {
		t.Errorf("同输入两次装配输出应全等（D3）")
	}
	if !reflect.DeepEqual(iv1.ToMap(), iv2.ToMap()) {
		t.Errorf("ToMap 输出应全等（D3）")
	}
}

func TestAssembleSignedAtNotInContentAddress(t *testing.T) {
	tpl := mustGoldenTemplate(t)
	params := goldenCases()[0].params
	iv1, err := Assemble(tpl, goldenCorpusRefs(), params, AssembleOptions{SignedAt: "2026-07-27T00:00:00+00:00"})
	if err != nil {
		t.Fatalf("装配失败: %v", err)
	}
	iv2, err := Assemble(tpl, goldenCorpusRefs(), params, AssembleOptions{SignedAt: "2026-07-28T00:00:00+00:00"})
	if err != nil {
		t.Fatalf("装配失败: %v", err)
	}
	// 公式二不含 lineage → signed_at 变化不影响 item_version_id.
	if iv1.ItemVersionID != iv2.ItemVersionID {
		t.Errorf("不同 signed_at 不应改变内容 id（D3）：%q != %q", iv1.ItemVersionID, iv2.ItemVersionID)
	}
	if iv1.Lineage["signed_at"] != "2026-07-27T00:00:00+00:00" || iv2.Lineage["signed_at"] != "2026-07-28T00:00:00+00:00" {
		t.Errorf("lineage.signed_at 应随输入变化：%v / %v", iv1.Lineage["signed_at"], iv2.Lineage["signed_at"])
	}
}

func TestAssembleDifferentParamsDifferentIDs(t *testing.T) {
	tpl := mustGoldenTemplate(t)
	seen := map[string]bool{}
	for _, tc := range goldenCases() {
		iv, err := Assemble(tpl, goldenCorpusRefs(), tc.params, AssembleOptions{SignedAt: "2026-07-27T00:00:00+00:00"})
		if err != nil {
			t.Fatalf("案例 %s 装配失败: %v", tc.caseID, err)
		}
		if seen[iv.ItemVersionID] {
			t.Errorf("案例 %s 与前例 id 重复（不同内容应不同 id）", tc.caseID)
		}
		seen[iv.ItemVersionID] = true
	}
}

// ────────────────────────────────────────────────────────────────────
// 产物正确性
// ────────────────────────────────────────────────────────────────────

func TestAssembleRendersBlocks(t *testing.T) {
	tpl := mustGoldenTemplate(t)
	for _, tc := range goldenCases() {
		iv, err := Assemble(tpl, goldenCorpusRefs(), tc.params, AssembleOptions{SignedAt: "2026-07-27T00:00:00+00:00"})
		if err != nil {
			t.Fatalf("案例 %s 装配失败: %v", tc.caseID, err)
		}
		blocks, ok := iv.Content["blocks"].([]any)
		if !ok || len(blocks) != 1 {
			t.Fatalf("案例 %s content.blocks 应为单元素数组", tc.caseID)
		}
		blk := blocks[0].(map[string]any)
		if blk["value"] != tc.expectedText {
			t.Errorf("案例 %s 渲染值不匹配：got %q want %q", tc.caseID, blk["value"], tc.expectedText)
		}
		// template 原文保留（谱系追溯）.
		if blk["template"] != "把 {value} {from_unit} 换算成 {to_unit}（结果保留两位小数）。" {
			t.Errorf("案例 %s template 应原样保留", tc.caseID)
		}
	}
}

func TestAssembleLineage(t *testing.T) {
	tpl := mustGoldenTemplate(t)
	params := goldenCases()[0].params
	iv, err := Assemble(tpl, goldenCorpusRefs(), params, AssembleOptions{SignedAt: "2026-07-27T00:00:00+00:00"})
	if err != nil {
		t.Fatalf("装配失败: %v", err)
	}
	if iv.Lineage["tier"] != "B" {
		t.Errorf("lineage.tier 应为 B，实际 %v", iv.Lineage["tier"])
	}
	refs, ok := iv.Lineage["corpus_refs"].([]any)
	if !ok || len(refs) != 1 {
		t.Fatalf("lineage.corpus_refs 必须非空且与输入等长")
	}
	ref := refs[0].(map[string]any)
	if ref["corpus_version_id"] != "sha256:math-functions-v1" || ref["digest"] != "sha256:math-functions-v1" {
		t.Errorf("corpus_ref 字段应与输入对应：%v", ref)
	}
	if iv.Lineage["template_version_id"] != "tpl-unit-conversion-v1" {
		t.Errorf("lineage.template_version_id 应保留模板 id，实际 %v", iv.Lineage["template_version_id"])
	}
	if !reflect.DeepEqual(iv.Lineage["params"], params) {
		t.Errorf("lineage.params 应保留装配参数（B 线核心谱系）")
	}
	pipeline := iv.Lineage["pipeline"].(map[string]any)
	if pipeline["id"] != "subject-math.b_assembler" || pipeline["version"] != "1.0.0" {
		t.Errorf("lineage.pipeline 不符预期：%v", pipeline)
	}
	if iv.Lineage["signed_by"] != "b_assembler" {
		t.Errorf("lineage.signed_by 应为 b_assembler，实际 %v", iv.Lineage["signed_by"])
	}
	// objective 从模板继承且深相等（不污染模板原数据）.
	if !reflect.DeepEqual(iv.Objective, tpl.Objective) {
		t.Errorf("objective 应从模板继承")
	}
	// scoring_ref 原样保留（answer_key 含 {answer} 占位，由评分器运行时解析）.
	scorerParams := iv.ScoringRef["scorer_params"].(map[string]any)
	if scorerParams["answer_key"] != "{answer}" {
		t.Errorf("scoring_ref.scorer_params.answer_key 应原样保留占位，实际 %v", scorerParams["answer_key"])
	}
}

// ────────────────────────────────────────────────────────────────────
// 装配错误处理（异常层级 + params 校验正负例）
// ────────────────────────────────────────────────────────────────────

func TestAssembleMissingCorpus(t *testing.T) {
	tpl := mustGoldenTemplate(t)
	_, err := Assemble(tpl, nil, goldenCases()[0].params, AssembleOptions{})
	var missing *MissingCorpusError
	if !errors.As(err, &missing) {
		t.Fatalf("期望 *MissingCorpusError，实际 %T: %v", err, err)
	}
	// 异常层级：MissingCorpusError → BAssemblerError.
	var base *BAssemblerError
	if !errors.As(err, &base) {
		t.Errorf("MissingCorpusError 应可 errors.As 到 *BAssemblerError（层级）")
	}
}

func TestAssembleParamValidation(t *testing.T) {
	tpl := mustGoldenTemplate(t)
	refs := goldenCorpusRefs()

	t.Run("必填槽缺失", func(t *testing.T) {
		params := map[string]any{"value": 1.5, "from_unit": "km", "to_unit": "m"}
		_, err := Assemble(tpl, refs, params, AssembleOptions{})
		var slotErr *SlotValidationError
		if !errors.As(err, &slotErr) {
			t.Fatalf("期望 *SlotValidationError，实际 %T: %v", err, err)
		}
		if !strings.Contains(err.Error(), "answer") {
			t.Errorf("错误应指认缺失槽 answer：%v", err)
		}
	})

	t.Run("未知槽", func(t *testing.T) {
		params := map[string]any{"value": 1.5, "from_unit": "km", "to_unit": "m", "answer": 1500, "unknown_slot": "spam"}
		_, err := Assemble(tpl, refs, params, AssembleOptions{})
		var slotErr *SlotValidationError
		if !errors.As(err, &slotErr) {
			t.Fatalf("期望 *SlotValidationError，实际 %T: %v", err, err)
		}
		if !strings.Contains(err.Error(), "unknown_slot") {
			t.Errorf("错误应指认未知槽 unknown_slot：%v", err)
		}
	})

	t.Run("bool 不能充当 number", func(t *testing.T) {
		params := map[string]any{"value": true, "from_unit": "km", "to_unit": "m", "answer": 1500}
		_, err := Assemble(tpl, refs, params, AssembleOptions{})
		var slotErr *SlotValidationError
		if !errors.As(err, &slotErr) {
			t.Fatalf("期望 *SlotValidationError，实际 %T: %v", err, err)
		}
		if !strings.Contains(err.Error(), "value") {
			t.Errorf("错误应指认槽 value：%v", err)
		}
	})

	t.Run("字符串不能充当 number", func(t *testing.T) {
		params := map[string]any{"value": "1.5", "from_unit": "km", "to_unit": "m", "answer": 1500}
		_, err := Assemble(tpl, refs, params, AssembleOptions{})
		var slotErr *SlotValidationError
		if !errors.As(err, &slotErr) {
			t.Fatalf("期望 *SlotValidationError，实际 %T: %v", err, err)
		}
	})

	t.Run("模板引用未知槽", func(t *testing.T) {
		// params 通过校验（槽都声明了）但模板引用了未提供的可选槽.
		tpl2 := &FrameworkTemplate{
			TemplateID: "tpl-x", TemplateVersion: "1.0", PackID: "p",
			Slots:        []SlotSpec{{Name: "a", Type: SlotTypeString}, {Name: "b", Type: SlotTypeString, Required: boolPtr(false)}},
			Presentation: []BlockSpec{{Type: "text", Template: strPtr("{a}{b}")}},
			Objective:    map[string]any{}, InteractionRef: map[string]any{}, ScoringRef: map[string]any{},
		}
		_, err := Assemble(tpl2, refs, map[string]any{"a": "x"}, AssembleOptions{})
		var slotErr *SlotValidationError
		if !errors.As(err, &slotErr) {
			t.Fatalf("期望 *SlotValidationError（模板引用未提供的槽），实际 %T: %v", err, err)
		}
	})
}

func boolPtr(b bool) *bool { return &b }

func strPtr(s string) *string { return &s }

// ────────────────────────────────────────────────────────────────────
// schema 校验（Pydantic 正负例）
// ────────────────────────────────────────────────────────────────────

func TestFrameworkTemplateSchema(t *testing.T) {
	t.Run("最小可用模板", func(t *testing.T) {
		tpl, err := NewFrameworkTemplate(map[string]any{
			"template_id":      "tpl-test",
			"template_version": "1.0",
			"pack_id":          "subject-math",
			"slots":            []any{map[string]any{"name": "x", "type": "integer"}},
			"presentation":     []any{map[string]any{"type": "text", "template": "{x}"}},
			"objective":        map[string]any{},
			"interaction_ref":  map[string]any{},
			"scoring_ref":      map[string]any{},
		})
		if err != nil {
			t.Fatalf("最小模板应合法: %v", err)
		}
		if tpl.Slots[0].Name != "x" {
			t.Errorf("slots 未正确装载")
		}
		// required 缺省 true（Python default）.
		if !tpl.Slots[0].IsRequired() {
			t.Errorf("required 缺省应为 true")
		}
	})

	t.Run("非 semver 版本拒绝", func(t *testing.T) {
		m := goldenTemplateMap()
		m["template_version"] = "not-semver"
		if _, err := NewFrameworkTemplate(m); !errors.Is(err, ErrInvalidTemplate) {
			t.Errorf("期望 ErrInvalidTemplate，实际 %v", err)
		}
	})

	t.Run("slots 缺失拒绝", func(t *testing.T) {
		m := goldenTemplateMap()
		delete(m, "slots")
		if _, err := NewFrameworkTemplate(m); !errors.Is(err, ErrInvalidTemplate) {
			t.Errorf("期望 ErrInvalidTemplate，实际 %v", err)
		}
	})

	t.Run("slot 类型越域拒绝", func(t *testing.T) {
		m := goldenTemplateMap()
		m["slots"] = []any{map[string]any{"name": "x", "type": "array"}}
		if _, err := NewFrameworkTemplate(m); !errors.Is(err, ErrInvalidTemplate) {
			t.Errorf("期望 ErrInvalidTemplate，实际 %v", err)
		}
	})

	t.Run("CorpusRef 必须两字段齐全", func(t *testing.T) {
		if _, err := NewCorpusRef(map[string]any{"corpus_version_id": "x"}); !errors.Is(err, ErrInvalidTemplate) {
			t.Errorf("缺 digest 应拒绝，实际 %v", err)
		}
		if _, err := NewCorpusRef(map[string]any{"digest": "x"}); !errors.Is(err, ErrInvalidTemplate) {
			t.Errorf("缺 corpus_version_id 应拒绝，实际 %v", err)
		}
		if _, err := NewCorpusRef(map[string]any{"corpus_version_id": "x", "digest": "y"}); err != nil {
			t.Errorf("双字段齐全应通过，实际 %v", err)
		}
	})

	t.Run("nil 模板拒绝", func(t *testing.T) {
		if _, err := Assemble(nil, goldenCorpusRefs(), map[string]any{}, AssembleOptions{}); !errors.Is(err, ErrInvalidTemplate) {
			t.Errorf("期望 ErrInvalidTemplate，实际 %v", err)
		}
	})
}

func TestAssembleDefaultLocale(t *testing.T) {
	tpl := mustGoldenTemplate(t)
	iv, err := Assemble(tpl, goldenCorpusRefs(), goldenCases()[0].params,
		AssembleOptions{SignedAt: "2026-07-27T00:00:00+00:00", Locale: DefaultLocale})
	if err != nil {
		t.Fatalf("装配失败: %v", err)
	}
	// 与默认 locale 装配同 id（缺省 zh-CN 生效）.
	ivDefault, err := Assemble(tpl, goldenCorpusRefs(), goldenCases()[0].params,
		AssembleOptions{SignedAt: "2026-07-27T00:00:00+00:00"})
	if err != nil {
		t.Fatalf("装配失败: %v", err)
	}
	if iv.ItemVersionID != ivDefault.ItemVersionID {
		t.Errorf("缺省 locale 应等价 zh-CN：%q != %q", iv.ItemVersionID, ivDefault.ItemVersionID)
	}
}
