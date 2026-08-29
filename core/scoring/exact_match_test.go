package scoring

import (
	"context"
	"encoding/json"
	"errors"
	"strings"
	"testing"

	"github.com/Cloudbird-Software/AI_Web_School/registry"
)

// exact_match 套件（T-W5-016 验收 #3 PyR 落地面）：
// - 全角/半角归一表逐对断言（Python 全量映射 + 运算符/标点/空白补全）；
// - 规范化三开关与次序；
// - 标量/序列/映射三形态判定 + partial_credit + 边界（全角、空白、多解形态）。

// TestFullwidthTablePairwise 逐对断言归一表（地面真值字面量，防实现自证）。
func TestFullwidthTablePairwise(t *testing.T) {
	norm := textNormalization{FullwidthToHalf: true} // strip 关闭——隔离表本身
	pairs := []struct{ in, want string }{
		// 数字（Python 全量映射）
		{"０", "0"}, {"１", "1"}, {"２", "2"}, {"３", "3"}, {"４", "4"},
		{"５", "5"}, {"６", "6"}, {"７", "7"}, {"８", "8"}, {"９", "9"},
		// 大写字母
		{"Ａ", "A"}, {"Ｂ", "B"}, {"Ｃ", "C"}, {"Ｄ", "D"}, {"Ｅ", "E"},
		{"Ｆ", "F"}, {"Ｇ", "G"}, {"Ｈ", "H"}, {"Ｉ", "I"}, {"Ｊ", "J"},
		{"Ｋ", "K"}, {"Ｌ", "L"}, {"Ｍ", "M"}, {"Ｎ", "N"}, {"Ｏ", "O"},
		{"Ｐ", "P"}, {"Ｑ", "Q"}, {"Ｒ", "R"}, {"Ｓ", "S"}, {"Ｔ", "T"},
		{"Ｕ", "U"}, {"Ｖ", "V"}, {"Ｗ", "W"}, {"Ｘ", "X"}, {"Ｙ", "Y"}, {"Ｚ", "Z"},
		// 小写字母
		{"ａ", "a"}, {"ｂ", "b"}, {"ｃ", "c"}, {"ｄ", "d"}, {"ｅ", "e"},
		{"ｆ", "f"}, {"ｇ", "g"}, {"ｈ", "h"}, {"ｉ", "i"}, {"ｊ", "j"},
		{"ｋ", "k"}, {"ｌ", "l"}, {"ｍ", "m"}, {"ｎ", "n"}, {"ｏ", "o"},
		{"ｐ", "p"}, {"ｑ", "q"}, {"ｒ", "r"}, {"ｓ", "s"}, {"ｔ", "t"},
		{"ｕ", "u"}, {"ｖ", "v"}, {"ｗ", "w"}, {"ｘ", "x"}, {"ｙ", "y"}, {"ｚ", "z"},
		// 运算符与常用标点（T-W5-016 补全面）
		{"！", "!"}, {"＂", "\""}, {"＃", "#"}, {"＄", "$"}, {"％", "%"},
		{"＆", "&"}, {"＇", "'"}, {"（", "("}, {"）", ")"}, {"＊", "*"},
		{"＋", "+"}, {"，", ","}, {"－", "-"}, {"．", "."}, {"／", "/"},
		{"：", ":"}, {"；", ";"}, {"＜", "<"}, {"＝", "="}, {"＞", ">"},
		{"？", "?"}, {"＠", "@"}, {"［", "["}, {"＼", "\\"}, {"］", "]"},
		{"＾", "^"}, {"＿", "_"}, {"｀", "`"}, {"｛", "{"}, {"｜", "|"},
		{"｝", "}"}, {"～", "~"},
		// 空白（strip 关闭时也归一）
		{"　", " "}, {" ", " "},
	}
	for _, p := range pairs {
		if got := NormalizeText(p.in, norm); got != p.want {
			t.Errorf("全角 %q → %q, want %q", p.in, got, p.want)
		}
	}
	if len(fullwidthToHalf) != 96 {
		t.Fatalf("归一表应为 96 项（FF01–FF5E 块 94 + 空白 2）: %d", len(fullwidthToHalf))
	}
}

// TestNormalizeTextModes 三开关行为与次序（全角 → 折叠空白 → 折叠大小写）.
func TestNormalizeTextModes(t *testing.T) {
	cases := []struct {
		name string
		in   string
		norm textNormalization
		want string
	}{
		{"缺省仅 strip（评分器经 parseNormalization 的缺省面）", "  a   b\t\nc  ", textNormalization{Strip: true}, "a b c"},
		{"strip 关闭保留原文", "  a   b  ", textNormalization{Strip: false}, "  a   b  "},
		{"大小写折叠", "AbC dEf", textNormalization{Strip: true, Casefold: true}, "abc def"},
		{"全角归一", "Ａ１ｂ", textNormalization{FullwidthToHalf: true, Strip: false}, "A1b"},
		{"全角+折叠+大小写", " Ｂ　Ｃ ", textNormalization{FullwidthToHalf: true, Strip: true, Casefold: true}, "b c"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := NormalizeText(tc.in, tc.norm); got != tc.want {
				t.Fatalf("NormalizeText(%q) = %q, want %q", tc.in, got, tc.want)
			}
		})
	}
}

// evidenceMap 解码 EvidenceJSON（证据面断言共用）.
func evidenceMap(t *testing.T, res registry.ScoreResult) map[string]any {
	t.Helper()
	var ev map[string]any
	if err := json.Unmarshal([]byte(res.EvidenceJSON), &ev); err != nil {
		t.Fatalf("EvidenceJSON 解码失败: %v", err)
	}
	return ev
}

// TestExactMatchScalar 标量答案（single_choice / 单值作答）正负例与边界.
func TestExactMatchScalar(t *testing.T) {
	s := NewExactMatchScorer()
	ctx := context.Background()

	t.Run("selected 命中", func(t *testing.T) {
		res, err := s.Score(ctx, `{"selected":"B"}`, map[string]any{"answer": "B"})
		if err != nil || !res.Correct || res.Score != 1 {
			t.Fatalf("res=%+v err=%v", res, err)
		}
	})
	t.Run("answer 键回退", func(t *testing.T) {
		res, err := s.Score(ctx, `{"answer":"C"}`, map[string]any{"answer": "C"})
		if err != nil || !res.Correct {
			t.Fatalf("res=%+v err=%v", res, err)
		}
	})
	t.Run("value 键回退", func(t *testing.T) {
		res, err := s.Score(ctx, `{"value":"7"}`, map[string]any{"answer": "7"})
		if err != nil || !res.Correct {
			t.Fatalf("res=%+v err=%v", res, err)
		}
	})
	t.Run("裸字符串作答", func(t *testing.T) {
		res, err := s.Score(ctx, "B", map[string]any{"answer": "B"})
		if err != nil || !res.Correct {
			t.Fatalf("res=%+v err=%v", res, err)
		}
	})
	t.Run("数字答案与字符串作答同投影", func(t *testing.T) {
		res, err := s.Score(ctx, `{"answer":"42"}`, map[string]any{"answer": 42})
		if err != nil || !res.Correct {
			t.Fatalf("res=%+v err=%v", res, err)
		}
	})
	t.Run("答错", func(t *testing.T) {
		res, err := s.Score(ctx, `{"selected":"C"}`, map[string]any{"answer": "B"})
		if err != nil || res.Correct || res.Score != 0 || res.Confidence != 1 {
			t.Fatalf("res=%+v err=%v", res, err)
		}
	})
	t.Run("缺作答键记错不误判", func(t *testing.T) {
		res, err := s.Score(ctx, `{}`, map[string]any{"answer": "B"})
		if err != nil || res.Correct || res.Score != 0 {
			t.Fatalf("res=%+v err=%v", res, err)
		}
	})
	t.Run("全角作答经归一命中", func(t *testing.T) {
		res, err := s.Score(ctx, `{"selected":"Ｂ"}`, map[string]any{
			"answer":        "B",
			"normalization": map[string]any{"fullwidth_to_half": true},
		})
		if err != nil || !res.Correct {
			t.Fatalf("res=%+v err=%v", res, err)
		}
	})
	t.Run("首尾空白折叠命中", func(t *testing.T) {
		res, err := s.Score(ctx, `{"answer":"  光合作用 "}`, map[string]any{"answer": "光合作用"})
		if err != nil || !res.Correct {
			t.Fatalf("res=%+v err=%v", res, err)
		}
	})
	t.Run("内部连续空白折叠为单空格后命中", func(t *testing.T) {
		res, err := s.Score(ctx, `{"answer":" 光合  作用 "}`, map[string]any{"answer": "光合 作用"})
		if err != nil || !res.Correct {
			t.Fatalf("res=%+v err=%v", res, err)
		}
	})
	t.Run("缺 answer 配置显式失败", func(t *testing.T) {
		if _, err := s.Score(ctx, "B", map[string]any{}); !errors.Is(err, ErrInvalidInput) {
			t.Fatalf("err = %v, want ErrInvalidInput", err)
		}
	})
}

// TestExactMatchSequence 数组答案（multi_choice 集合 / ordering 序列）.
func TestExactMatchSequence(t *testing.T) {
	s := NewExactMatchScorer()
	ctx := context.Background()

	t.Run("无序集合全中", func(t *testing.T) {
		res, err := s.Score(ctx, `{"selected":["C","A"]}`, map[string]any{"answer": []any{"A", "C"}})
		if err != nil || !res.Correct || res.Score != 1 {
			t.Fatalf("res=%+v err=%v", res, err)
		}
	})
	t.Run("无序集合含多选错误项全对判错", func(t *testing.T) {
		res, err := s.Score(ctx, `{"selected":["A","C","B"]}`, map[string]any{"answer": []any{"A", "C"}})
		if err != nil || res.Correct || res.Score != 0 {
			t.Fatalf("res=%+v err=%v", res, err)
		}
		ev := evidenceMap(t, res)
		js := ev["judgements"].([]any)
		if len(js) != 3 { // 期望两项 + 一个 extra
			t.Fatalf("judgements 应为 3 条: %v", ev)
		}
	})
	t.Run("per_item 按命中比例给分", func(t *testing.T) {
		res, err := s.Score(ctx, `{"selected":["A"]}`, map[string]any{
			"answer":         []any{"A", "C"},
			"partial_credit": map[string]any{"per_item": true},
		})
		if err != nil || res.Correct {
			t.Fatalf("res=%+v err=%v", res, err)
		}
		if res.Score != 0.5 {
			t.Fatalf("per_item 命中比例应为 0.5: %v", res.Score)
		}
	})
	t.Run("ordering 有序序列错位", func(t *testing.T) {
		res, err := s.Score(ctx, `{"sequence":["A","B"]}`, map[string]any{
			"answer":  []any{"B", "A"},
			"ordered": true,
		})
		if err != nil || res.Correct || res.Score != 0 {
			t.Fatalf("res=%+v err=%v", res, err)
		}
		ev := evidenceMap(t, res)
		js := ev["judgements"].([]any)
		first := js[0].(map[string]any)
		if first["part"] != "pos1" || first["ok"] != false {
			t.Fatalf("首位判定应记错: %v", first)
		}
	})
	t.Run("ordering 多出元素记 extra", func(t *testing.T) {
		res, err := s.Score(ctx, `{"sequence":["B","A","C","D"]}`, map[string]any{
			"answer":  []any{"B", "A"},
			"ordered": true,
		})
		if err != nil {
			t.Fatal(err)
		}
		ev := evidenceMap(t, res)
		js := ev["judgements"].([]any)
		last := js[len(js)-1].(map[string]any)
		if last["part"] != "extra" || last["ok"] != false {
			t.Fatalf("多出元素应记 extra: %v", last)
		}
	})
	t.Run("ordering 顺序全对", func(t *testing.T) {
		res, err := s.Score(ctx, `{"sequence":["B","A"]}`, map[string]any{
			"answer":  []any{"B", "A"},
			"ordered": true,
		})
		if err != nil || !res.Correct || res.Score != 1 {
			t.Fatalf("res=%+v err=%v", res, err)
		}
	})
	t.Run("无序缺答记错", func(t *testing.T) {
		res, err := s.Score(ctx, `{}`, map[string]any{"answer": []any{"A"}})
		if err != nil || res.Correct || res.Score != 0 {
			t.Fatalf("res=%+v err=%v", res, err)
		}
	})
}

// TestExactMatchMapping 对象答案（blanks / matching / drawing_operation）.
func TestExactMatchMapping(t *testing.T) {
	s := NewExactMatchScorer()
	ctx := context.Background()

	t.Run("逐空全对（含 {value,unit} 解包）", func(t *testing.T) {
		res, err := s.Score(ctx,
			`{"blanks":{"b1":"3","b2":{"value":"5","unit":"cm"}}}`,
			map[string]any{"answer": map[string]any{"b1": "3", "b2": "5"}})
		if err != nil || !res.Correct || res.Score != 1 {
			t.Fatalf("res=%+v err=%v", res, err)
		}
	})
	t.Run("逐空部分错全对判错", func(t *testing.T) {
		res, err := s.Score(ctx, `{"blanks":{"b1":"3","b2":"4"}}`,
			map[string]any{"answer": map[string]any{"b1": "3", "b2": "5"}})
		if err != nil || res.Correct || res.Score != 0 {
			t.Fatalf("res=%+v err=%v", res, err)
		}
	})
	t.Run("逐空 per_item 比例", func(t *testing.T) {
		res, err := s.Score(ctx, `{"blanks":{"b1":"3","b2":"4","b3":"5"}}`,
			map[string]any{
				"answer":         map[string]any{"b1": "3", "b2": "5", "b3": "5"},
				"partial_credit": map[string]any{"per_item": true},
			})
		if err != nil {
			t.Fatal(err)
		}
		if res.Score != 2.0/3.0 {
			t.Fatalf("比例给分应为 2/3: %v", res.Score)
		}
	})
	t.Run("matching pairs", func(t *testing.T) {
		res, err := s.Score(ctx,
			`{"pairs":[{"left_id":"l1","right_id":"r2"},{"left_id":"l2","right_id":"r1"}]}`,
			map[string]any{"answer": map[string]any{"l1": "r2", "l2": "r1"}})
		if err != nil || !res.Correct || res.Score != 1 {
			t.Fatalf("res=%+v err=%v", res, err)
		}
	})
	t.Run("drawing_operation elements", func(t *testing.T) {
		res, err := s.Score(ctx,
			`{"elements":[{"element_id":"e1","state":"drawn"}]}`,
			map[string]any{"answer": map[string]any{"e1": "drawn"}})
		if err != nil || !res.Correct || res.Score != 1 {
			t.Fatalf("res=%+v err=%v", res, err)
		}
	})
	t.Run("全角数字作答经归一命中", func(t *testing.T) {
		res, err := s.Score(ctx, `{"blanks":{"b1":"１２３"}}`, map[string]any{
			"answer":        map[string]any{"b1": "123"},
			"normalization": map[string]any{"fullwidth_to_half": true},
		})
		if err != nil || !res.Correct {
			t.Fatalf("res=%+v err=%v", res, err)
		}
	})
	t.Run("判定明细确定性（同输入同证据）", func(t *testing.T) {
		params := map[string]any{"answer": map[string]any{"b1": "3", "b2": "5"}}
		r1, err1 := s.Score(ctx, `{"blanks":{"b1":"3","b2":"4"}}`, params)
		r2, err2 := s.Score(ctx, `{"blanks":{"b1":"3","b2":"4"}}`, params)
		if err1 != nil || err2 != nil {
			t.Fatalf("err1=%v err2=%v", err1, err2)
		}
		if r1.EvidenceJSON != r2.EvidenceJSON {
			t.Fatalf("同输入证据必须逐字节一致（可回放）:\n%s\n%s", r1.EvidenceJSON, r2.EvidenceJSON)
		}
	})
}

// TestExactMatchThroughRunner 注册表面端到端：KindAny 声明面接受三种 answer
// 形态；Runner 契约校验 + trace 装配 + 证据随行.
func TestExactMatchThroughRunner(t *testing.T) {
	tb := registry.NewScorerTable()
	if err := tb.Register("exact_match", NewExactMatchScorer()); err != nil {
		t.Fatal(err)
	}
	r, err := NewRunner(tb)
	if err != nil {
		t.Fatal(err)
	}

	cases := []struct {
		name    string
		answer  string
		params  map[string]any
		correct bool
	}{
		{"标量 answer", `{"selected":"B"}`, map[string]any{"answer": "B"}, true},
		{"数组 answer", `{"selected":["A"]}`, map[string]any{"answer": []any{"A"}}, true},
		{"对象 answer", `{"blanks":{"b1":"x"}}`, map[string]any{"answer": map[string]any{"b1": "x"}}, true},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			run, err := r.Run(context.Background(), RunInput{ScorerID: "exact_match", Answer: tc.answer, Params: tc.params})
			if err != nil {
				t.Fatal(err)
			}
			if run.ScorerVersion != versionExactMatch {
				t.Fatalf("版本锚不符: %s", run.ScorerVersion)
			}
			if run.Result.Correct != tc.correct {
				t.Fatalf("correct=%v", run.Result.Correct)
			}
			if _, ok := run.Trace["evidence"]; !ok {
				t.Fatalf("评分证据应随 trace 落账: %v", run.Trace)
			}
		})
	}

	// 缺 answer 必备键在 Runner 门被拦（评分器不被触达）.
	if _, err := r.Run(context.Background(), RunInput{ScorerID: "exact_match", Answer: "B", Params: map[string]any{}}); !errors.Is(err, ErrInvalidInput) || !strings.Contains(err.Error(), "缺必备键") {
		t.Fatalf("err = %v, want 缺必备键", err)
	}
}
