package scoring

import (
	"context"
	"errors"
	"math/big"
	"strings"
	"testing"
)

// math_equivalence 套件：
// - 解析与比较两套代码各自独立验证（双实现独立重算纪律）；
// - 等值形态（0.5 vs 1/2 vs 2/4、容差、单位换算、多解形态）；
// - 正/负例 + 配置违例 fail-loud + 错误类型推断随 evidence。

// TestParseMathExpr 解析侧地面真值（第一套代码）.
func TestParseMathExpr(t *testing.T) {
	cases := []struct {
		in   string
		want string // big.Rat 规范化形态
	}{
		{"42", "42"},
		{"0.5", "1/2"},
		{"1/2", "1/2"},
		{"2/4", "1/2"},
		{"1+2", "3"},
		{"2*3+4", "10"},
		{"(1+2)/4", "3/4"},
		{"-3", "-3"},
		{"1.25*4", "5"},
		{" 1 / 2 ", "1/2"},
		{".5", "1/2"},
		{"3-5", "-2"},
	}
	for _, tc := range cases {
		v, err := parseMathExpr(tc.in)
		if err != nil {
			t.Errorf("parse(%q) 意外失败: %v", tc.in, err)
			continue
		}
		if got := v.RatString(); got != tc.want {
			t.Errorf("parse(%q) = %s, want %s", tc.in, got, tc.want)
		}
	}
	for _, bad := range []string{"", "abc", "1+", "1/0", "((1)", "1 2", "1..2", "*3", "1e3"} {
		if _, err := parseMathExpr(bad); !errors.Is(err, ErrInvalidInput) {
			t.Errorf("parse(%q) 应 ErrInvalidInput，得到 %v", bad, err)
		}
	}
}

// TestEquivalentValues 比较侧地面真值（第二套代码）.
func TestEquivalentValues(t *testing.T) {
	cases := []struct {
		name       string
		a, b       string
		tol        string // "" = 精确比较
		wantOK     bool
		wantMethod string
	}{
		{"精确相等", "1/2", "1/2", "", true, "exact"},
		{"小数与分数等值（多解形态）", "0.5", "1/2", "", true, "exact"},
		{"化简等值", "2/4", "1/2", "", true, "exact"},
		{"容差命中", "1.0001", "1.0000", "0.001", true, "tolerance"},
		{"容差带内分数", "1/3", "0.3333", "0.001", true, "tolerance"},
		{"容差越界", "1/2", "2/5", "0.001", false, "mismatch"},
		{"无容差不精确", "1/3", "0.3333", "", false, "mismatch"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			var tol *big.Rat
			if tc.tol != "" {
				tol = mustParse(t, tc.tol)
			}
			ok, method := equivalentValues(mustParse(t, tc.a), mustParse(t, tc.b), tol)
			if ok != tc.wantOK || method != tc.wantMethod {
				t.Fatalf("equivalent(%s, %s) = %v/%s, want %v/%s", tc.a, tc.b, ok, method, tc.wantOK, tc.wantMethod)
			}
		})
	}
}

func mustParse(t *testing.T, s string) *big.Rat {
	t.Helper()
	v, err := parseMathExpr(s)
	if err != nil {
		t.Fatalf("parse(%q): %v", s, err)
	}
	return v
}

// TestMathEquivalenceScorer 评分器表驱动：正/负例 + 等值形态 + 错误推断.
func TestMathEquivalenceScorer(t *testing.T) {
	s := NewMathEquivalenceScorer()
	ctx := context.Background()

	cases := []struct {
		name        string
		answer      string
		params      map[string]any
		wantScore   float64
		wantCorrect bool
		checkEV     func(t *testing.T, ev map[string]any)
	}{
		{
			name:   "0.5 vs 1/2 等值（小数/分数多解形态）",
			answer: "0.5", params: map[string]any{"answer_expr": "1/2"},
			wantScore: 1, wantCorrect: true,
			checkEV: func(t *testing.T, ev map[string]any) {
				if ev["method"] != "exact" {
					t.Fatalf("method=%v", ev["method"])
				}
			},
		},
		{
			name: "2/4 化简等值", answer: "2/4", params: map[string]any{"answer_expr": "1/2"},
			wantScore: 1, wantCorrect: true,
		},
		{
			name: "算术表达式归约", answer: "1+2", params: map[string]any{"answer_expr": "3"},
			wantScore: 1, wantCorrect: true,
		},
		{
			name: "数值容差命中", answer: "1.0001",
			params:    map[string]any{"answer_expr": "1.0000", "tolerance": "0.001"},
			wantScore: 1, wantCorrect: true,
			checkEV: func(t *testing.T, ev map[string]any) {
				if ev["method"] != "tolerance" {
					t.Fatalf("method=%v", ev["method"])
				}
			},
		},
		{
			name: "容差越界判错（value_mismatch）", answer: "1.5",
			params:    map[string]any{"answer_expr": "1.3", "tolerance": "0.001"},
			wantScore: 0, wantCorrect: false,
			checkEV: func(t *testing.T, ev map[string]any) {
				if got := firstInferenceType(t, ev); got != "value_mismatch" {
					t.Fatalf("error_type_id=%v", got)
				}
			},
		},
		{
			name: "容差等于阈值命中（≤）", answer: "1.5",
			params:    map[string]any{"answer_expr": "1", "tolerance": "0.5"},
			wantScore: 1, wantCorrect: true,
		},
		{
			name: "规则禁用容差", answer: "1.5",
			params: map[string]any{"answer_expr": "1", "tolerance": "0.5",
				"equivalence_rules": []any{"fraction_reduce"}},
			wantScore: 0, wantCorrect: false,
		},
		{
			name: "单位换算 100cm=1m", answer: "100cm",
			params:    map[string]any{"answer_expr": "1m"},
			wantScore: 1, wantCorrect: true,
			checkEV: func(t *testing.T, ev map[string]any) {
				if ev["used_unit_conversion"] != true {
					t.Fatalf("used_unit_conversion=%v", ev["used_unit_conversion"])
				}
			},
		},
		{
			name: "单位换算 1m=100cm（反向）", answer: "1m",
			params:    map[string]any{"answer_expr": "100cm"},
			wantScore: 1, wantCorrect: true,
		},
		{
			name: "单位显式分离形态", answer: `{"value":"100","unit":"cm"}`,
			params:    map[string]any{"answer_expr": "1m"},
			wantScore: 1, wantCorrect: true,
		},
		{
			name: "单位不同不可换算判错（wrong_unit）", answer: "1kg",
			params:    map[string]any{"answer_expr": "1g"},
			wantScore: 0, wantCorrect: false,
			checkEV: func(t *testing.T, ev map[string]any) {
				if got := firstInferenceType(t, ev); got != "wrong_unit" {
					t.Fatalf("error_type_id=%v", got)
				}
			},
		},
		{
			name: "禁用 unit_convert 单位不同判错", answer: "1m",
			params:    map[string]any{"answer_expr": "1cm", "equivalence_rules": []any{"fraction_reduce"}},
			wantScore: 0, wantCorrect: false,
		},
		{
			name: "仅一侧带单位按数值比较", answer: "100",
			params:    map[string]any{"answer_expr": "1m"},
			wantScore: 0, wantCorrect: false,
		},
		{
			name: "差 1 推断 off_by_one", answer: "6",
			params:    map[string]any{"answer_expr": "5"},
			wantScore: 0, wantCorrect: false,
			checkEV: func(t *testing.T, ev map[string]any) {
				if got := firstInferenceType(t, ev); got != "off_by_one" {
					t.Fatalf("error_type_id=%v", got)
				}
			},
		},
		{
			name: "单空 blanks 形态", answer: `{"blanks":{"b1":"0.5"}}`,
			params:    map[string]any{"answer_expr": "1/2"},
			wantScore: 1, wantCorrect: true,
		},
		{
			name: "作答为空判错不降置信", answer: "",
			params:    map[string]any{"answer_expr": "1"},
			wantScore: 0, wantCorrect: false,
			checkEV: func(t *testing.T, ev map[string]any) {
				if got := firstInferenceType(t, ev); got != "empty_response" {
					t.Fatalf("error_type_id=%v", got)
				}
			},
		},
		{
			name: "作答无法解析判错不降置信", answer: "abc",
			params:    map[string]any{"answer_expr": "1"},
			wantScore: 0, wantCorrect: false,
			checkEV: func(t *testing.T, ev map[string]any) {
				if got := firstInferenceType(t, ev); got != "invalid_response" {
					t.Fatalf("error_type_id=%v", got)
				}
			},
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			res, err := s.Score(ctx, tc.answer, tc.params)
			if err != nil {
				t.Fatalf("意外失败: %v", err)
			}
			if res.Score != tc.wantScore || res.Correct != tc.wantCorrect {
				t.Fatalf("res=%+v", res)
			}
			if res.Confidence != 1.0 {
				t.Fatalf("确定性评分器置信度应为 1: %v", res.Confidence)
			}
			if tc.checkEV != nil {
				tc.checkEV(t, evidenceMap(t, res))
			}
		})
	}
}

// TestMathEquivalenceConfigErrors 配置违例 fail-loud（Python 置信度 0 结果
// 在 Go 面收紧为错误）.
func TestMathEquivalenceConfigErrors(t *testing.T) {
	s := NewMathEquivalenceScorer()
	ctx := context.Background()
	cases := []struct {
		name   string
		params map[string]any
	}{
		{"缺 answer_expr", map[string]any{}},
		{"answer_expr 空串", map[string]any{"answer_expr": "  "}},
		{"tolerance 非法", map[string]any{"answer_expr": "1", "tolerance": "xyz"}},
		{"多空作答", map[string]any{"answer_expr": "1"}},
	}
	for i, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			answer := "1"
			if i == 3 {
				answer = `{"blanks":{"a":"1","b":"2"}}`
			}
			if _, err := s.Score(ctx, answer, tc.params); !errors.Is(err, ErrInvalidInput) {
				t.Fatalf("err = %v, want ErrInvalidInput", err)
			}
		})
	}
	// 标准答案不可解析同样是配置错误.
	if _, err := s.Score(ctx, "1", map[string]any{"answer_expr": "y=x"}); !errors.Is(err, ErrInvalidInput) {
		t.Fatalf("err = %v, want ErrInvalidInput", err)
	}
}

// TestMathEquivalenceEvidenceDeterministic 同输入同证据（可回放断言）.
func TestMathEquivalenceEvidenceDeterministic(t *testing.T) {
	s := NewMathEquivalenceScorer()
	params := map[string]any{"answer_expr": "1/2", "tolerance": "0"}
	r1, err1 := s.Score(context.Background(), "0.5", params)
	r2, err2 := s.Score(context.Background(), "0.5", params)
	if err1 != nil || err2 != nil {
		t.Fatalf("err1=%v err2=%v", err1, err2)
	}
	if r1.EvidenceJSON != r2.EvidenceJSON {
		t.Fatalf("同输入证据必须逐字节一致:\n%s\n%s", r1.EvidenceJSON, r2.EvidenceJSON)
	}
	if !strings.Contains(r1.EvidenceJSON, "rules_applied") {
		t.Fatalf("证据应含规则面: %s", r1.EvidenceJSON)
	}
}

// firstInferenceType 取 evidence.error_inferences[0].error_type_id.
func firstInferenceType(t *testing.T, ev map[string]any) string {
	t.Helper()
	infs, ok := ev["error_inferences"].([]any)
	if !ok || len(infs) == 0 {
		t.Fatalf("证据缺 error_inferences: %v", ev)
	}
	first, _ := infs[0].(map[string]any)
	typ, _ := first["error_type_id"].(string)
	return typ
}
