// engine_test.go 确定性实例化引擎的验收测试。
//
// 测试策略（对齐 tests/unit/test_instantiation_engine.py + 黄金样例）：
//  1. 跨语言地面真值：3 个黄金样例（tests/golden/instantiation/sample_*.yaml
//     同构内联）的 expected_item_version_id 由冻结 Python 实现产出并钉死在
//     YAML（禁改路径），Go 端 Instantiate 必须逐字节复现（公式一 + 规范化
//     参数 + ENGINE_DIGEST 的全链互验）。
//  2. 确定性：同输入跨次/并发调用必得同 id（D3）。
//  3. 结构：六大块齐全、item_id 自引用、status=draft、lineage tier=A。
//  4. fail-closed：spec 不合规、未知槽、表达式求值失败、干扰项碰撞。
package instantiation

import (
	"sync"
	"testing"

	"github.com/Cloudbird-Software/AI_Web_School/core/instantiation/dsl"
)

// goldenTemplate 构造黄金样例模板（与 sample_single_choice.yaml 同构）。
func goldenTemplate() map[string]any {
	return map[string]any{
		"template_version_id": "sha256:fixture-template-single-choice-add",
		"template_id":         "tpl-001-single-choice-add",
		"dsl_version":         "1",
		"spec": map[string]any{
			"objective": map[string]any{
				"kp_set":          []any{map[string]any{"dimension": "kp", "code": "math.nal.int.add"}},
				"kp_set_mode":     "single",
				"cognitive_level": "apply",
				"gradeband":       "L",
				"graph_release":   "2026.1",
			},
			"slots": map[string]any{
				"a": map[string]any{"type": "int", "difficulty_relevant": true},
				"b": map[string]any{"type": "int", "difficulty_relevant": true},
			},
			"variation_axes": map[string]any{"axes": []any{}},
			"presentation": map[string]any{
				"blocks": []any{map[string]any{"kind": "text", "template": "{a} + {b} = ?"}},
			},
			"answer_program": map[string]any{"expression": "a + b", "returns": "number"},
			"distractor_rules": map[string]any{
				"rules": []any{
					map[string]any{
						"rule_type":     "deterministic",
						"error_type_id": "err.calc.add.off-by-one",
						"expression":    "a + b + 1",
						"label":         "多 1",
					},
					map[string]any{
						"rule_type":     "deterministic",
						"error_type_id": "err.calc.add.minus-one",
						"expression":    "a + b - 1",
						"label":         "少 1",
					},
				},
			},
		},
	}
}

func goldenOpts() InstantiateOptions {
	return InstantiateOptions{
		PackDigest:    "sha256:pack-math-fixture",
		InteractionID: "single_choice",
		ScorerID:      "exact_match",
		ScorerParams:  map[string]any{"answer": 7},
		Locale:        "zh-CN",
		CorpusDigests: []string{},
		Seed:          0,
		SignedAt:      "2026-07-27T00:00:00+00:00",
	}
}

// 黄金地面真值：冻结 Python instantiate 对 sample_single_choice.yaml 的
// expected_item_version_id（禁改路径钉死，Go 端必须逐字节复现）。
const goldenSingleChoiceID = "sha256:8bbe4cb1acbbe9d7e7b00e5dbe9f794142a00d738e53b5db74454b1144ff2c80"

func TestInstantiateGoldenAgainstFrozenPython(t *testing.T) {
	iv, err := Instantiate(goldenTemplate(), map[string]any{"a": 3, "b": 4}, goldenOpts())
	if err != nil {
		t.Fatalf("黄金样例实例化失败: %v", err)
	}
	if iv.ItemVersionID != goldenSingleChoiceID {
		t.Fatalf("item_version_id 与冻结实现不一致:\n got  %s\n want %s", iv.ItemVersionID, goldenSingleChoiceID)
	}
	if iv.ItemID != iv.ItemVersionID {
		t.Errorf("A/B 级 item_id 必须自引用 item_version_id")
	}
	if iv.Status != "draft" {
		t.Errorf("status 默认 draft，实际 %q", iv.Status)
	}
	// content 插值："{a} + {b} = ?" → "3 + 4 = ?"
	blocks, ok := iv.Content["blocks"].([]any)
	if !ok || len(blocks) != 1 {
		t.Fatalf("content.blocks 结构异常: %v", iv.Content)
	}
	b0 := blocks[0].(map[string]any)
	if b0["rendered"] != "3 + 4 = ?" {
		t.Errorf("插值结果 = %v, 期望 %q", b0["rendered"], "3 + 4 = ?")
	}
	// error_bindings：2 条规则 → 2 个绑定（7+1 / 7-1）
	if len(iv.ErrorBindings) != 2 {
		t.Fatalf("error_bindings 数量 = %d, 期望 2", len(iv.ErrorBindings))
	}
	eb := iv.ErrorBindings[0]
	if eb["error_type_id"] != "err.calc.add.off-by-one" {
		t.Errorf("error_type_id = %v", eb["error_type_id"])
	}
	// lineage
	if iv.Lineage["tier"] != "A" {
		t.Errorf("lineage.tier = %v, 期望 A", iv.Lineage["tier"])
	}
	if iv.Lineage["template_version_id"] != "sha256:fixture-template-single-choice-add" {
		t.Errorf("lineage.template_version_id = %v", iv.Lineage["template_version_id"])
	}
	if iv.Lineage["signed_at"] != "2026-07-27T00:00:00+00:00" {
		t.Errorf("lineage.signed_at = %v", iv.Lineage["signed_at"])
	}
	// objective 携带自母题
	if iv.Objective["cognitive_level"] != "apply" {
		t.Errorf("objective.cognitive_level = %v", iv.Objective["cognitive_level"])
	}
}

// 数值填空黄金样例（sample_numeric_blank.yaml 同构）：sqrt 表达式。
func TestInstantiateGoldenNumericBlank(t *testing.T) {
	tpl := goldenTemplate()
	spec := tpl["spec"].(map[string]any)
	spec["objective"] = map[string]any{
		"kp_set":          []any{map[string]any{"dimension": "kp", "code": "math.gm.pythagorean"}},
		"kp_set_mode":     "single",
		"cognitive_level": "apply",
		"gradeband":       "M",
		"graph_release":   "2026.1",
	}
	spec["presentation"] = map[string]any{
		"blocks": []any{map[string]any{"kind": "text", "template": "直角三角形两直角边为 {a} 和 {b}，求斜边长。"}},
	}
	spec["answer_program"] = map[string]any{"expression": "sqrt(a * a + b * b)", "returns": "number"}
	spec["distractor_rules"] = map[string]any{
		"rules": []any{
			map[string]any{"rule_type": "deterministic", "error_type_id": "err.pythagorean.sum", "expression": "a + b", "label": "和而非斜边"},
			map[string]any{"rule_type": "deterministic", "error_type_id": "err.pythagorean.diff", "expression": "a * a + b * b", "label": "未开方"},
		},
	}
	tpl["template_version_id"] = "sha256:fixture-template-numeric-blank-pythagorean"
	tpl["template_id"] = "tpl-002-numeric-blank-pythagorean"
	opts := goldenOpts()
	opts.InteractionID = "numeric_blank"
	opts.ScorerID = "math_equivalence"
	opts.ScorerParams = map[string]any{"answer_expr": "5"}
	iv, err := Instantiate(tpl, map[string]any{"a": 3, "b": 4}, opts)
	if err != nil {
		t.Fatalf("数值填空黄金样例实例化失败: %v", err)
	}
	want := "sha256:a99651a61918333fea02be6b9b7bcd69fb981d5f3a39368ccf8a4022621c6128"
	if iv.ItemVersionID != want {
		t.Fatalf("item_version_id 与冻结实现不一致:\n got  %s\n want %s", iv.ItemVersionID, want)
	}
}

// 匹配题黄金样例（sample_matching.yaml 同构）：string 槽 + 无干扰项。
func TestInstantiateGoldenMatching(t *testing.T) {
	tpl := goldenTemplate()
	spec := tpl["spec"].(map[string]any)
	spec["objective"] = map[string]any{
		"kp_set":          []any{map[string]any{"dimension": "kp", "code": "math.nal.int.recognize"}},
		"kp_set_mode":     "single",
		"cognitive_level": "remember",
		"gradeband":       "L",
		"graph_release":   "2026.1",
	}
	spec["slots"] = map[string]any{
		"first_num":   map[string]any{"type": "int", "difficulty_relevant": false},
		"first_text":  map[string]any{"type": "string", "difficulty_relevant": false},
		"second_num":  map[string]any{"type": "int", "difficulty_relevant": false},
		"second_text": map[string]any{"type": "string", "difficulty_relevant": false},
	}
	spec["presentation"] = map[string]any{
		"blocks": []any{map[string]any{"kind": "text", "template": "把数字与对应的中文连线：{first_num}—?, {second_num}—?"}},
	}
	spec["answer_program"] = map[string]any{"expression": "first_num", "returns": "number"}
	spec["distractor_rules"] = map[string]any{"rules": []any{}}
	tpl["template_version_id"] = "sha256:fixture-template-matching-number-hanzi"
	tpl["template_id"] = "tpl-003-matching-number-hanzi"
	opts := goldenOpts()
	opts.InteractionID = "matching"
	opts.ScorerParams = map[string]any{"answer": map[string]any{"pairs": []any{
		map[string]any{"1": "一"}, map[string]any{"2": "二"},
	}}}
	params := map[string]any{
		"first_num": 1, "first_text": "一",
		"second_num": 2, "second_text": "二",
	}
	iv, err := Instantiate(tpl, params, opts)
	if err != nil {
		t.Fatalf("匹配题黄金样例实例化失败: %v", err)
	}
	want := "sha256:f1d45df31f7f4281f995c7f253b15c9e8086ab22485e223328508febbe27c71d"
	if iv.ItemVersionID != want {
		t.Fatalf("item_version_id 与冻结实现不一致:\n got  %s\n want %s", iv.ItemVersionID, want)
	}
	if len(iv.ErrorBindings) != 0 {
		t.Errorf("无规则时 error_bindings 应为空，实际 %d", len(iv.ErrorBindings))
	}
}

func TestInstantiateDeterministicConcurrent(t *testing.T) {
	const want = goldenSingleChoiceID
	var wg sync.WaitGroup
	ids := make([]string, 24)
	errs := make([]error, 24)
	for i := range ids {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			iv, err := Instantiate(goldenTemplate(), map[string]any{"a": 3, "b": 4}, goldenOpts())
			if err != nil {
				errs[i] = err
				return
			}
			ids[i] = iv.ItemVersionID
		}(i)
	}
	wg.Wait()
	for i := range ids {
		if errs[i] != nil {
			t.Fatalf("并发实例化 [%d] 失败: %v", i, errs[i])
		}
		if ids[i] != want {
			t.Fatalf("并发实例化 [%d] id 漂移: %s", i, ids[i])
		}
	}
}

func TestInstantiateFailClosed(t *testing.T) {
	cases := []struct {
		name  string
		tpl   map[string]any
		param map[string]any
	}{
		{"缺 template_version_id", func() map[string]any {
			tpl := goldenTemplate()
			delete(tpl, "template_version_id")
			return tpl
		}(), map[string]any{"a": 3, "b": 4}},
		{"缺 template_id", func() map[string]any {
			tpl := goldenTemplate()
			delete(tpl, "template_id")
			return tpl
		}(), map[string]any{"a": 3, "b": 4}},
		{"缺 spec", func() map[string]any {
			tpl := goldenTemplate()
			delete(tpl, "spec")
			return tpl
		}(), map[string]any{"a": 3, "b": 4}},
		{"spec extra 字段", func() map[string]any {
			tpl := goldenTemplate()
			tpl["spec"].(map[string]any)["typo_block"] = map[string]any{}
			return tpl
		}(), map[string]any{"a": 3, "b": 4}},
		{"slot 类型非法", func() map[string]any {
			tpl := goldenTemplate()
			tpl["spec"].(map[string]any)["slots"].(map[string]any)["a"] =
				map[string]any{"type": "float", "difficulty_relevant": true}
			return tpl
		}(), map[string]any{"a": 3, "b": 4}},
		{"未知槽参数", goldenTemplate(), map[string]any{"a": 3, "b": 4, "c": 5}},
		{"缺槽参数", goldenTemplate(), map[string]any{"a": 3}},
		{"表达式未知变量", func() map[string]any {
			tpl := goldenTemplate()
			tpl["spec"].(map[string]any)["answer_program"] =
				map[string]any{"expression": "zzz + 1", "returns": "number"}
			return tpl
		}(), map[string]any{"a": 3, "b": 4}},
		{"表达式除零", func() map[string]any {
			tpl := goldenTemplate()
			tpl["spec"].(map[string]any)["answer_program"] =
				map[string]any{"expression": "a / (b - b)", "returns": "number"}
			return tpl
		}(), map[string]any{"a": 3, "b": 4}},
		{"干扰项与正解碰撞", func() map[string]any {
			tpl := goldenTemplate()
			tpl["spec"].(map[string]any)["distractor_rules"] = map[string]any{
				"rules": []any{map[string]any{
					"rule_type": "deterministic", "error_type_id": "err.same", "expression": "a + b",
				}},
			}
			return tpl
		}(), map[string]any{"a": 3, "b": 4}},
		{"模板引用未提供槽", func() map[string]any {
			tpl := goldenTemplate()
			tpl["spec"].(map[string]any)["presentation"] = map[string]any{
				"blocks": []any{map[string]any{"kind": "text", "template": "{missing}?"}},
			}
			return tpl
		}(), map[string]any{"a": 3, "b": 4}},
	}
	for _, tc := range cases {
		_, err := Instantiate(tc.tpl, tc.param, goldenOpts())
		if err == nil {
			t.Errorf("case %q: 期望 fail-closed，实际成功", tc.name)
		}
	}
}

func TestNormalizeParamsDecimalFraction(t *testing.T) {
	spec, err := dsl.ParseSpec(map[string]any{
		"objective": map[string]any{
			"kp_set":          []any{map[string]any{"dimension": "kp", "code": "x"}},
			"kp_set_mode":     "single",
			"cognitive_level": "apply",
			"gradeband":       "L",
			"graph_release":   "r",
		},
		"slots": map[string]any{
			"d": map[string]any{"type": "decimal", "difficulty_relevant": false},
			"f": map[string]any{"type": "fraction", "difficulty_relevant": false},
			"s": map[string]any{"type": "string", "difficulty_relevant": false},
		},
		"variation_axes":   map[string]any{"axes": []any{}},
		"presentation":     map[string]any{"blocks": []any{}},
		"answer_program":   map[string]any{"expression": "1", "returns": "number"},
		"distractor_rules": map[string]any{"rules": []any{}},
	})
	if err != nil {
		t.Fatalf("ParseSpec 失败: %v", err)
	}
	got, err := NormalizeParams(map[string]any{
		"d": "3.10",
		"f": "0.75",
		"s": "一",
	}, spec.Slots)
	if err != nil {
		t.Fatalf("NormalizeParams 失败: %v", err)
	}
	// '3.10' 与 '3.1' 规范化后必相同（去尾零，无 E 记号）
	got2, _ := NormalizeParams(map[string]any{"d": "3.1", "f": "3/4", "s": "一"}, spec.Slots)
	if got["d"] != "3.1" || got2["d"] != "3.1" {
		t.Errorf("decimal 规范化: %v / %v, 期望 3.1", got["d"], got2["d"])
	}
	if got["f"] != "3/4" {
		t.Errorf("fraction 规范化: %v, 期望 3/4", got["f"])
	}
}
