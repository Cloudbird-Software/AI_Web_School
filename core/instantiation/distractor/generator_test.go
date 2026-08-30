// generator_test.go 干扰项生成器的验收测试（T-W2-003）。
//
// 覆盖（对齐验收 §1/§2/§3）：
//  1. deterministic 规则：安全表达式求值 → 选项绑定 error_type_id；
//  2. corpus_sample 规则：corpus_ref 占位（value=nil）；
//  3. 碰撞检查：与正解同值默认拒绝（DistractorCollisionError），
//     allowCollision=true 时标记 collision；跨规则去重审计 DistinctValues；
//  4. fail-closed：缺 expression / 缺 corpus_ref / 表达式求值失败 / 空列表。
package distractor

import (
	"errors"
	"testing"

	"github.com/Cloudbird-Software/AI_Web_School/core/instantiation/dsl"
	"github.com/Cloudbird-Software/AI_Web_School/core/instantiation/expr"
)

func detRule(exprStr string) dsl.DistractorRule {
	e := exprStr
	return dsl.DistractorRule{
		RuleType:    "deterministic",
		ErrorTypeID: "err.calc.off-by-one",
		Expression:  &e,
	}
}

func corpusRule(ref string) dsl.DistractorRule {
	r := ref
	return dsl.DistractorRule{
		RuleType:    "corpus_sample",
		ErrorTypeID: "err.reading.confusion",
		CorpusRef:   &r,
	}
}

func TestGenerateDeterministic(t *testing.T) {
	res, err := Generate(detRule("a + b + 1"), map[string]any{"a": 3, "b": 4}, expr.IntValue(7), false)
	if err != nil {
		t.Fatalf("生成失败: %v", err)
	}
	if len(res.Options) != 1 {
		t.Fatalf("单标量结果应展开为 1 个 option，实际 %d", len(res.Options))
	}
	opt := res.Options[0]
	if opt.Value != expr.IntValue(8) {
		t.Errorf("option 值 = %v, 期望 8", opt.Value)
	}
	if opt.ErrorBinding != "err.calc.off-by-one" {
		t.Errorf("error_binding = %q", opt.ErrorBinding)
	}
	if opt.Collision {
		t.Errorf("不应对正解碰撞")
	}
}

func TestGenerateDeterministicListExpansion(t *testing.T) {
	// env 注入 list → 表达式返回 list → 展开为多个 option
	res, err := Generate(detRule("candidates"),
		map[string]any{"a": 3, "b": 4, "candidates": []any{5, 6}},
		expr.IntValue(7), false)
	if err != nil {
		t.Fatalf("生成失败: %v", err)
	}
	if len(res.Options) != 2 {
		t.Fatalf("list 结果应展开为 2 个 option，实际 %d", len(res.Options))
	}
	if res.Options[0].Value != expr.IntValue(5) || res.Options[1].Value != expr.IntValue(6) {
		t.Errorf("展开值不符: %v / %v", res.Options[0].Value, res.Options[1].Value)
	}
}

func TestGenerateCorpusSample(t *testing.T) {
	res, err := Generate(corpusRule("corpus:reading-001"), map[string]any{}, expr.IntValue(7), false)
	if err != nil {
		t.Fatalf("生成失败: %v", err)
	}
	opt := res.Options[0]
	if opt.Value != nil {
		t.Errorf("corpus_sample 占位 value 应为 nil，实际 %v", opt.Value)
	}
	if opt.CorpusRef != "corpus:reading-001" {
		t.Errorf("corpus_ref = %q", opt.CorpusRef)
	}
	if opt.Label != "corpus:reading-001" {
		t.Errorf("label 缺省应回填 corpus_ref，实际 %q", opt.Label)
	}
}

func TestGenerateCollision(t *testing.T) {
	// a + b 与正解 7 相等 → 默认拒绝
	_, err := Generate(detRule("a + b"), map[string]any{"a": 3, "b": 4}, expr.IntValue(7), false)
	var coll *CollisionError
	if !errors.As(err, &coll) {
		t.Fatalf("期望 CollisionError，实际 %T: %v", err, err)
	}
	// allowCollision=true → 标记而非拒绝
	res, err := Generate(detRule("a + b"), map[string]any{"a": 3, "b": 4}, expr.IntValue(7), true)
	if err != nil {
		t.Fatalf("容差模式不应拒绝: %v", err)
	}
	if !res.Options[0].Collision {
		t.Errorf("容差模式应标记 collision=true")
	}
	// 无正解（nil）→ 不做碰撞检查
	res, err = Generate(detRule("a + b"), map[string]any{"a": 3, "b": 4}, nil, false)
	if err != nil {
		t.Fatalf("answer=nil 时不应碰撞: %v", err)
	}
	if res.Options[0].Collision {
		t.Errorf("answer=nil 时不应标记碰撞")
	}
	// 跨类型数值相等也算碰撞（1 == 1.0）
	if _, err := Generate(detRule("a + 1"), map[string]any{"a": 6}, expr.FloatValue(7.0), false); err == nil {
		t.Errorf("int 值与 float 正解同值应判碰撞")
	}
	// 字符串不冒充数值：值不等不碰撞
	if _, err := Generate(detRule("'7'"), map[string]any{}, expr.IntValue(7), false); err != nil {
		t.Errorf("字符串 '7' 与 int 7 不应判碰撞: %v", err)
	}
}

func TestGenerateDistinctValues(t *testing.T) {
	res, err := Generate(detRule("candidates"),
		map[string]any{"candidates": []any{5, 5, 6}}, expr.IntValue(7), false)
	if err != nil {
		t.Fatalf("生成失败: %v", err)
	}
	// 去重审计：重复值折叠为唯一取值清单
	got := DistinctValues(res.Options)
	if len(got) != 2 || got[0] != "5" || got[1] != "6" {
		t.Errorf("DistinctValues = %v, 期望 [5 6]", got)
	}
}

func TestGenerateFailClosed(t *testing.T) {
	cases := []struct {
		name string
		rule dsl.DistractorRule
		env  map[string]any
	}{
		{"deterministic 缺 expression", dsl.DistractorRule{RuleType: "deterministic", ErrorTypeID: "e"}, map[string]any{}},
		{"deterministic 空 expression", detRule(""), map[string]any{}},
		{"corpus_sample 缺 corpus_ref", dsl.DistractorRule{RuleType: "corpus_sample", ErrorTypeID: "e"}, map[string]any{}},
		{"表达式求值失败", detRule("unknown_slot + 1"), map[string]any{}},
		{"表达式类型错", detRule(`"a" + 1`), map[string]any{}},
		{"空 list 结果", detRule("empty"), map[string]any{"empty": []any{}}},
		{"未知 rule_type", dsl.DistractorRule{RuleType: "magic", ErrorTypeID: "e"}, map[string]any{}},
	}
	for _, tc := range cases {
		if _, err := Generate(tc.rule, tc.env, expr.IntValue(7), false); err == nil {
			t.Errorf("case %q: 期望 fail-closed，实际成功", tc.name)
		}
	}
}
