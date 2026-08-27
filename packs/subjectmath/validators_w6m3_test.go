package subjectmath

import (
	"errors"
	"strings"
	"testing"
)

// validators_w6m3_test.go —— 第三阶 3 母题（⑧乘除混合⑨时间单位换算
// ⑩长方形/正方形周长面积）的验证器防共谋与负例组（沿第二阶口径）：
//   - 地面真值：手写题干/答案直接喂验证器，不经过生成器——生成器与验证器
//     同向同错会在这组立刻暴露（历史 bug：×60 写成 ×10、周长忘 ×2、
//     ÷60 误当 ÷10、面积误用周长公式全部在此钉死）；
//   - 负例：每个新母题 ≥15 条故意坏实例，且必须落在预期的哨兵类别上。
//
// 手工实例走 handBuiltNB（绕开生成器装配）。

// ────────────────────────────────────────────────────────────────
// 母题⑧ 乘除混合：地面真值 + 负例
// ────────────────────────────────────────────────────────────────

func TestValidateIntMulDivGroundTruth(t *testing.T) {
	truths := []struct{ stem, ans, expl string }{
		// 半单位经典：先乘后除，中间积整除
		{"6 × 7 ÷ 3 = （  ）", "14",
			"同级运算从左往右：先算 6 × 7=42，再算 42 ÷ 3=14。"},
		// 先除后乘：56 ÷ 8 = 7，7 × 5 = 35
		{"56 ÷ 8 × 5 = （  ）", "35",
			"同级运算从左往右：先算 56 ÷ 8=7，再算 7 × 5=35。"},
		// 连续表内：8 × 9 ÷ 6 = 12
		{"8 × 9 ÷ 6 = （  ）", "12",
			"同级运算从左往右：先算 8 × 9=72，再算 72 ÷ 6=12。"},
		// 情境前缀变体（不含数字的前缀——数字 token 数口径的边界）
		{"按要求从左往右依次计算：18 ÷ 6 × 4 = （  ）", "12",
			"同级运算从左往右：先算 18 ÷ 6=3，再算 3 × 4=12。"},
		// 商恰为 2 的下边界
		{"算一算：14 ÷ 7 × 9 = （  ）", "18",
			"同级运算从左往右：先算 14 ÷ 7=2，再算 2 × 9=18。"},
	}
	for _, tc := range truths {
		inst := handBuiltNB(idIntMulDiv, tc.stem, tc.ans, tc.expl)
		if err := validateIntMulDiv(inst); err != nil {
			t.Errorf("地面真值 [%s=%s] 被验证器误杀: %v", tc.stem, tc.ans, err)
		}
	}

	// 经典错答形态：口算滑步、次序颠倒
	wrongs := []struct{ stem, wrongAns string }{
		{"56 ÷ 8 × 5 = （  ）", "40"}, // 56÷8 滑成 8
		{"8 × 9 ÷ 6 = （  ）", "13"},  // 72÷6 滑成 13
		{"45 ÷ 9 × 7 = （  ）", "42"}, // 45÷9 滑成 6
	}
	for _, tc := range wrongs {
		inst := handBuiltNB(idIntMulDiv, tc.stem, tc.wrongAns,
			"口算得到 "+tc.wrongAns+"。")
		err := validateIntMulDiv(inst)
		wantClass(t, err, ErrAnswerMismatch, "muldiv-wrong["+tc.stem+"="+tc.wrongAns+"]")
	}
}

func TestValidateIntMulDivRejectsMutants(t *testing.T) {
	base := firstValid(t, idIntMulDiv, 7)
	n := 0

	cp := cloneOf(t, base)
	dig(t, cp.Content, "answer")["value"] = "0"
	mutClass(t, &n, Validate(cp), ErrAnswerMismatch, "①answer-value")

	cp = cloneOf(t, base)
	dig(t, cp.ScoringRef, "scorer_params")["answer"] = "0"
	mutClass(t, &n, Validate(cp), ErrAnswerMismatch, "②scorer-answer")

	cp = cloneOf(t, base)
	put2(cp.Content["blocks"].([]any)[0].(map[string]any), "rendered", "6 × 7 ＋ 3 = （  ）")
	mutClass(t, &n, Validate(cp), ErrShapeInvalid, "③no-div-operator")

	cp = cloneOf(t, base)
	put2(cp.Content["blocks"].([]any)[0].(map[string]any), "rendered", "6 × 7 × 3 = （  ）")
	mutClass(t, &n, Validate(cp), ErrShapeInvalid, "④two-mul")

	cp = cloneOf(t, base)
	put2(cp.Content["blocks"].([]any)[0].(map[string]any), "rendered", "123 × 85 ÷ 7 = （  ）")
	mutClass(t, &n, Validate(cp), ErrFormatViolation, "⑤md-a-above-domain")

	cp = cloneOf(t, base)
	put2(cp.Content["blocks"].([]any)[0].(map[string]any), "rendered", "6 × 77 ÷ 3 = （  ）")
	mutClass(t, &n, Validate(cp), ErrFormatViolation, "⑥md-b-above-domain")

	// MD 不整除（42 ÷ 4）：性质断言命中
	cp = cloneOf(t, base)
	put2(cp.Content["blocks"].([]any)[0].(map[string]any), "rendered", "6 × 7 ÷ 4 = （  ）")
	mutClass(t, &n, Validate(cp), ErrConsistencyBroken, "⑦md-not-divisible")

	// DM 不整除（57 ÷ 4）
	cp = cloneOf(t, base)
	put2(cp.Content["blocks"].([]any)[0].(map[string]any), "rendered", "57 ÷ 4 × 3 = （  ）")
	mutClass(t, &n, Validate(cp), ErrConsistencyBroken, "⑧dm-not-divisible")

	// DM 商为 1 的退化题（4 ÷ 4）
	cp = cloneOf(t, base)
	put2(cp.Content["blocks"].([]any)[0].(map[string]any), "rendered", "4 ÷ 4 × 3 = （  ）")
	mutClass(t, &n, Validate(cp), ErrConsistencyBroken, "⑨dm-quotient-one")

	// 三操作数变两个（算符数先于数字数判——缺 ÷ 即句式破损）
	cp = cloneOf(t, base)
	put2(cp.Content["blocks"].([]any)[0].(map[string]any), "rendered", "6 × 7 = （  ）")
	mutClass(t, &n, Validate(cp), ErrShapeInvalid, "⑩two-operands")

	// 混入第四个数字
	cp = cloneOf(t, base)
	put2(cp.Content["blocks"].([]any)[0].(map[string]any), "rendered", "第 1 题：6 × 7 ÷ 3 = （  ）")
	mutClass(t, &n, Validate(cp), ErrConsistencyBroken, "⑪four-tokens")

	// 操作数退化值 1
	cp = cloneOf(t, base)
	put2(cp.Content["blocks"].([]any)[0].(map[string]any), "rendered", "6 × 1 ÷ 3 = （  ）")
	mutClass(t, &n, Validate(cp), ErrFormatViolation, "⑫degenerate-operand")

	cp = cloneOf(t, base)
	put2(cp.Content, "explanation", "口算可得。")
	mutClass(t, &n, Validate(cp), ErrConsistencyBroken, "⑬explanation-thin")

	cp = cloneOf(t, base)
	delete(dig(t, cp.Content, "answer"), "blank_id")
	mutClass(t, &n, Validate(cp), ErrShapeInvalid, "⑭blank-id")

	cp = cloneOf(t, base)
	cp.ErrorBindings[0]["subject"] = "option:A"
	mutClass(t, &n, Validate(cp), ErrConsistencyBroken, "⑮binding-subject")

	cp = cloneOf(t, base)
	cp.ErrorBindings = nil
	mutClass(t, &n, Validate(cp), ErrShapeInvalid, "⑯bindings-empty")

	cp = cloneOf(t, base)
	cp.InteractionRef = map[string]any{"interaction_id": "single_choice"}
	mutClass(t, &n, Validate(cp), ErrShapeInvalid, "⑰interaction")

	cp = cloneOf(t, base)
	cp.Locale = "en-US"
	mutClass(t, &n, Validate(cp), ErrShapeInvalid, "⑱locale")

	cp = cloneOf(t, base)
	cp.Lineage["tier"] = "B"
	mutClass(t, &n, Validate(cp), ErrShapeInvalid, "⑲tier")

	cp = cloneOf(t, base)
	stemStr := stemRendered(t, cp)
	put2(cp.Content["blocks"].([]any)[0].(map[string]any),
		"rendered", strings.Replace(stemStr, "（  ）", "____", 1))
	mutClass(t, &n, Validate(cp), ErrShapeInvalid, "⑳no-blank")

	if n < 15 {
		t.Fatalf("负例数 %d < 15", n)
	}
}

// ────────────────────────────────────────────────────────────────
// 母题⑨ 时间单位换算：地面真值 + 负例
// ────────────────────────────────────────────────────────────────

// handBuiltNBUnit 在 handBuiltNB 基础上补齐带单位的答案位
// （时间/几何模板的已发布形态含 answer.unit 与 scoring_ref.unit）。
func handBuiltNBUnit(tplID, stem, ans, unit, expl string) *Instance {
	inst := handBuiltNB(tplID, stem, ans, expl)
	inst.Content["answer"].(map[string]any)["unit"] = unit
	inst.ScoringRef["scorer_params"].(map[string]any)["unit"] = unit
	return inst
}

func TestValidateTimeConvGroundTruth(t *testing.T) {
	truths := []struct{ stem, ans, unit, expl string }{
		{"5 分 = （  ）秒", "300", "秒",
			"因为 1分=60秒，所以 5 × 60=300。单位是秒。"},
		// 历史分歧点钉死：×60 不是 ×10（90 秒 ≠ 9 分）
		{"90 秒 = （  ）分", "1.5", "分",
			"因为 1分=60秒，所以 90 ÷ 60=1.5。单位是分。"},
		{"3 时 = （  ）分", "180", "分",
			"因为 1时=60分，所以 3 × 60=180。单位是分。"},
		{"240 分 = （  ）时", "4", "时",
			"因为 1时=60分，所以 240 ÷ 60=4。单位是时。"},
		// 小数值放大：0.5 分 = 30 秒
		{"0.5 分 = （  ）秒", "30", "秒",
			"因为 1分=60秒，所以 0.5 × 60=30。单位是秒。"},
		// 两位小数缩小：3 秒 = 0.05 分（长除逐位路径的钉死点）
		{"3 秒 = （  ）分", "0.05", "分",
			"因为 1分=60秒，所以 3 ÷ 60=0.05。单位是分。"},
		{"1 时 = （  ）分", "60", "分",
			"因为 1时=60分，所以 1 × 60=60。单位是分。"},
	}
	for _, tc := range truths {
		inst := handBuiltNBUnit(idTimeConv, tc.stem, tc.ans, tc.unit, tc.expl)
		if err := validateTimeConv(inst); err != nil {
			t.Errorf("地面真值 [%s=%s] 被验证器误杀: %v", tc.stem, tc.ans, err)
		}
	}

	// 十进制误当六十进制（历史 bug 形态：÷10 / ×10 / ÷10 的商）
	wrongs := []struct{ stem, wrongAns, wrongUnit string }{
		{"90 秒 = （  ）分", "9", "分"},   // ÷10 误当 ÷60
		{"5 分 = （  ）秒", "50", "秒"},   // ×10 误当 ×60
		{"240 分 = （  ）时", "24", "时"}, // ÷10 误当 ÷60
		{"3 时 = （  ）分", "300", "分"},  // ×100 误当 ×60
	}
	for _, tc := range wrongs {
		inst := handBuiltNBUnit(idTimeConv, tc.stem, tc.wrongAns, tc.wrongUnit,
			"换算得到 "+tc.wrongAns+"。")
		err := validateTimeConv(inst)
		wantClass(t, err, ErrAnswerMismatch, "time-wrong["+tc.stem+"="+tc.wrongAns+"]")
	}
}

func TestValidateTimeConvRejectsMutants(t *testing.T) {
	base := firstValid(t, idTimeConv, 7)
	n := 0

	cp := cloneOf(t, base)
	dig(t, cp.Content, "answer")["value"] = "0"
	mutClass(t, &n, Validate(cp), ErrAnswerMismatch, "①answer-value")

	cp = cloneOf(t, base)
	dig(t, cp.ScoringRef, "scorer_params")["answer"] = "0"
	mutClass(t, &n, Validate(cp), ErrAnswerMismatch, "②scorer-answer")

	cp = cloneOf(t, base)
	put2(cp.Content["blocks"].([]any)[0].(map[string]any), "rendered", "5 分 ＋（  ）秒")
	mutClass(t, &n, Validate(cp), ErrShapeInvalid, "③no-conv-pattern")

	cp = cloneOf(t, base)
	put2(cp.Content["blocks"].([]any)[0].(map[string]any), "rendered", "5 分 = （  ）分")
	mutClass(t, &n, Validate(cp), ErrConsistencyBroken, "④same-unit")

	// 跨两级（时→秒，×3600 超纲）
	cp = cloneOf(t, base)
	put2(cp.Content["blocks"].([]any)[0].(map[string]any), "rendered", "3 时 = （  ）秒")
	mutClass(t, &n, Validate(cp), ErrFormatViolation, "⑤cross-two-ladders")

	// 缩小方向非有限小数（100 秒 = 5/3 分）
	cp = cloneOf(t, base)
	put2(cp.Content["blocks"].([]any)[0].(map[string]any), "rendered", "100 秒 = （  ）分")
	mutClass(t, &n, Validate(cp), ErrFormatViolation, "⑥non-terminating")

	// 两位小数值超口径
	cp = cloneOf(t, base)
	put2(cp.Content["blocks"].([]any)[0].(map[string]any), "rendered", "1.25 分 = （  ）秒")
	mutClass(t, &n, Validate(cp), ErrFormatViolation, "⑦two-decimal-value")

	// 尾零非规范
	cp = cloneOf(t, base)
	put2(cp.Content["blocks"].([]any)[0].(map[string]any), "rendered", "5.0 分 = （  ）秒")
	mutClass(t, &n, Validate(cp), ErrFormatViolation, "⑧trailing-zero")

	// 值域上限外
	cp = cloneOf(t, base)
	put2(cp.Content["blocks"].([]any)[0].(map[string]any), "rendered", "1000 分 = （  ）秒")
	mutClass(t, &n, Validate(cp), ErrFormatViolation, "⑨above-domain")

	// 单位不在量纲表（米）
	cp = cloneOf(t, base)
	put2(cp.Content["blocks"].([]any)[0].(map[string]any), "rendered", "5 米 = （  ）秒")
	mutClass(t, &n, Validate(cp), ErrShapeInvalid, "⑩unit-not-in-table")

	cp = cloneOf(t, base)
	put2(cp.Content, "explanation", "换算一下就好。")
	mutClass(t, &n, Validate(cp), ErrConsistencyBroken, "⑪explanation-thin")

	cp = cloneOf(t, base)
	put2(cp.Content, "explanation", "因为 1分=60秒，所以等于 60。单位是秒。")
	mutClass(t, &n, Validate(cp), ErrConsistencyBroken, "⑫explanation-wrong-value")

	cp = cloneOf(t, base)
	dig(t, cp.Content, "answer")["unit"] = "时"
	mutClass(t, &n, Validate(cp), ErrAnswerMismatch, "⑬answer-unit")

	cp = cloneOf(t, base)
	delete(dig(t, cp.Content, "answer"), "blank_id")
	mutClass(t, &n, Validate(cp), ErrShapeInvalid, "⑭blank-id")

	cp = cloneOf(t, base)
	cp.ErrorBindings[0]["subject"] = "blank:b2"
	mutClass(t, &n, Validate(cp), ErrConsistencyBroken, "⑮binding-subject")

	cp = cloneOf(t, base)
	cp.InteractionRef = map[string]any{"interaction_id": "single_choice"}
	mutClass(t, &n, Validate(cp), ErrShapeInvalid, "⑯interaction")

	cp = cloneOf(t, base)
	cp.Lineage["tier"] = "C"
	mutClass(t, &n, Validate(cp), ErrShapeInvalid, "⑰tier")

	if n < 15 {
		t.Fatalf("负例数 %d < 15", n)
	}
}

// ────────────────────────────────────────────────────────────────
// 母题⑩ 长方形/正方形周长面积：地面真值 + 负例
// ────────────────────────────────────────────────────────────────

func TestValidateGeoRectGroundTruth(t *testing.T) {
	truths := []struct{ stem, ans, unit, expl string }{
		{"一个长方形，长 12 厘米，宽 5 厘米，它的周长是（  ）厘米。", "34", "厘米",
			"长方形的周长 =（长＋宽）×2 =（12＋5）×2 = 17×2 = 34（厘米）。"},
		{"一个长方形，长 12 厘米，宽 5 厘米，它的面积是（  ）平方厘米。", "60", "平方厘米",
			"长方形的面积 = 长×宽 = 12×5 = 60（平方厘米）。"},
		{"一个正方形，边长 7 厘米，它的周长是（  ）厘米。", "28", "厘米",
			"正方形的周长 = 边长×4 = 7×4 = 28（厘米）。"},
		{"一个正方形，边长 7 厘米，它的面积是（  ）平方厘米。", "49", "平方厘米",
			"正方形的面积 = 边长×边长 = 7×7 = 49（平方厘米）。"},
		// 前缀变体（句首无数字——token 提取口径的边界）
		{"算一算：一个长方形，长 9 厘米，宽 4 厘米，它的周长是（  ）厘米。", "26", "厘米",
			"长方形的周长 =（长＋宽）×2 =（9＋4）×2 = 13×2 = 26（厘米）。"},
	}
	for _, tc := range truths {
		inst := handBuiltNBUnit(idGeoRect, tc.stem, tc.ans, tc.unit, tc.expl)
		if err := validateGeoRect(inst); err != nil {
			t.Errorf("地面真值 [%s=%s] 被验证器误杀: %v", tc.stem, tc.ans, err)
		}
	}

	// 经典错答形态：周长忘 ×2、面积误用周长公式
	wrongs := []struct{ stem, wrongAns, wrongUnit string }{
		{"一个长方形，长 12 厘米，宽 5 厘米，它的周长是（  ）厘米。", "17", "厘米"},     // 长+宽 忘 ×2
		{"一个长方形，长 12 厘米，宽 5 厘米，它的面积是（  ）平方厘米。", "34", "平方厘米"}, // 面积误用周长公式
		{"一个正方形，边长 7 厘米，它的面积是（  ）平方厘米。", "28", "平方厘米"},        // 面积误用周长公式
	}
	for _, tc := range wrongs {
		inst := handBuiltNBUnit(idGeoRect, tc.stem, tc.wrongAns, tc.wrongUnit,
			"计算得到 "+tc.wrongAns+"。")
		err := validateGeoRect(inst)
		wantClass(t, err, ErrAnswerMismatch, "geo-wrong["+tc.stem+"="+tc.wrongAns+"]")
	}
}

func TestValidateGeoRectRejectsMutants(t *testing.T) {
	base := firstValid(t, idGeoRect, 7)
	n := 0

	cp := cloneOf(t, base)
	dig(t, cp.Content, "answer")["value"] = "0"
	mutClass(t, &n, Validate(cp), ErrAnswerMismatch, "①answer-value")

	cp = cloneOf(t, base)
	dig(t, cp.ScoringRef, "scorer_params")["answer"] = "0"
	mutClass(t, &n, Validate(cp), ErrAnswerMismatch, "②scorer-answer")

	// 句式破坏（去掉量词）
	cp = cloneOf(t, base)
	put2(cp.Content["blocks"].([]any)[0].(map[string]any),
		"rendered", "一个长方形，长 12 厘米，宽 5 厘米，它是（  ）厘米。")
	mutClass(t, &n, Validate(cp), ErrShapeInvalid, "③no-quantity-word")

	// 长 ≤ 宽（空间约束；相等归 square 形态）
	cp = cloneOf(t, base)
	put2(cp.Content["blocks"].([]any)[0].(map[string]any),
		"rendered", "一个长方形，长 5 厘米，宽 5 厘米，它的周长是（  ）厘米。")
	mutClass(t, &n, Validate(cp), ErrConsistencyBroken, "④length-le-width")

	// 长超域
	cp = cloneOf(t, base)
	put2(cp.Content["blocks"].([]any)[0].(map[string]any),
		"rendered", "一个长方形，长 31 厘米，宽 5 厘米，它的周长是（  ）厘米。")
	mutClass(t, &n, Validate(cp), ErrFormatViolation, "⑤length-above-domain")

	// 边长超域
	cp = cloneOf(t, base)
	put2(cp.Content["blocks"].([]any)[0].(map[string]any),
		"rendered", "一个正方形，边长 1 厘米，它的周长是（  ）厘米。")
	mutClass(t, &n, Validate(cp), ErrFormatViolation, "⑥side-below-domain")

	// 周长配面积单位（量词/单位交叉错配）
	cp = cloneOf(t, base)
	put2(cp.Content["blocks"].([]any)[0].(map[string]any),
		"rendered", "一个长方形，长 12 厘米，宽 5 厘米，它的周长是（  ）平方厘米。")
	mutClass(t, &n, Validate(cp), ErrConsistencyBroken, "⑦perim-with-area-unit")

	// 面积配周长单位
	cp = cloneOf(t, base)
	put2(cp.Content["blocks"].([]any)[0].(map[string]any),
		"rendered", "一个正方形，边长 7 厘米，它的面积是（  ）厘米。")
	mutClass(t, &n, Validate(cp), ErrConsistencyBroken, "⑧area-with-perim-unit")

	// answer.unit 篡改
	cp = cloneOf(t, base)
	dig(t, cp.Content, "answer")["unit"] = "分米"
	mutClass(t, &n, Validate(cp), ErrAnswerMismatch, "⑨answer-unit")

	// scoring_ref.unit 篡改
	cp = cloneOf(t, base)
	dig(t, cp.ScoringRef, "scorer_params")["unit"] = "分米"
	mutClass(t, &n, Validate(cp), ErrAnswerMismatch, "⑩scorer-unit")

	cp = cloneOf(t, base)
	put2(cp.Content, "explanation", "公式算一下。")
	mutClass(t, &n, Validate(cp), ErrConsistencyBroken, "⑪explanation-thin")

	// 解析缺公式字样
	cp = cloneOf(t, base)
	put2(cp.Content, "explanation", "周长是 34，单位是厘米。")
	mutClass(t, &n, Validate(cp), ErrConsistencyBroken, "⑫explanation-no-formula")

	cp = cloneOf(t, base)
	delete(dig(t, cp.Content, "answer"), "blank_id")
	mutClass(t, &n, Validate(cp), ErrShapeInvalid, "⑬blank-id")

	cp = cloneOf(t, base)
	cp.ErrorBindings[0]["subject"] = "blank:b2"
	mutClass(t, &n, Validate(cp), ErrConsistencyBroken, "⑭binding-subject")

	cp = cloneOf(t, base)
	cp.ErrorBindings = nil
	mutClass(t, &n, Validate(cp), ErrShapeInvalid, "⑮bindings-empty")

	cp = cloneOf(t, base)
	cp.InteractionRef = map[string]any{"interaction_id": "single_choice"}
	mutClass(t, &n, Validate(cp), ErrShapeInvalid, "⑯interaction")

	cp = cloneOf(t, base)
	cp.Locale = "en-US"
	mutClass(t, &n, Validate(cp), ErrShapeInvalid, "⑰locale")

	cp = cloneOf(t, base)
	cp.Lineage["tier"] = "B"
	mutClass(t, &n, Validate(cp), ErrShapeInvalid, "⑱tier")

	if n < 15 {
		t.Fatalf("负例数 %d < 15", n)
	}
}

// TestW6M3ValidatorsNoCrossTemplateLeak 新验证器对异母题实例必须拒绝，
// 异母题实例喂新验证器也必须拒绝（分发正确性双向断言）。
func TestW6M3ValidatorsNoCrossTemplateLeak(t *testing.T) {
	muldiv := firstValid(t, idIntMulDiv, 3)
	if err := validateTimeConv(muldiv); err == nil {
		t.Fatal("muldiv 实例喂 time 验证器必须被拒")
	}
	if err := validateGeoRect(muldiv); err == nil {
		t.Fatal("muldiv 实例喂 geo 验证器必须被拒")
	}
	timeInst := firstValid(t, idTimeConv, 3)
	if err := validateIntMulDiv(timeInst); err == nil {
		t.Fatal("time 实例喂 muldiv 验证器必须被拒")
	}
	if err := validateGeoRect(timeInst); err == nil {
		t.Fatal("time 实例喂 geo 验证器必须被拒")
	}
	geo := firstValid(t, idGeoRect, 3)
	if err := validateIntMulDiv(geo); err == nil {
		t.Fatal("geo 实例喂 muldiv 验证器必须被拒")
	}
	if err := validateTimeConv(geo); err == nil {
		t.Fatal("geo 实例喂 time 验证器必须被拒")
	}
	// 旧母题实例喂新验证器
	if err := validateIntMulDiv(firstValid(t, idUnitConv, 3)); err == nil {
		t.Fatal("conv 实例喂 muldiv 验证器必须被拒")
	}
	if err := validateTimeConv(firstValid(t, idIntRound, 3)); err == nil {
		t.Fatal("round 实例喂 time 验证器必须被拒")
	}
	if err := validateGeoRect(firstValid(t, idIntAddSub, 3)); err == nil {
		t.Fatal("addsub 实例喂 geo 验证器必须被拒")
	}
	// 新验证器进总分发口
	if err := Validate(muldiv); err != nil {
		t.Fatalf("分发口对新母题实例误杀: %v", err)
	}
}

// TestW6M3SentinelCoverage 确认三个新验证器的拒绝都落在四类哨兵上
// （抽样断言：每类哨兵至少被一个新验证器产出）。
func TestW6M3SentinelCoverage(t *testing.T) {
	seen := map[error]bool{}
	note := func(err error) {
		if err == nil {
			return
		}
		seen[ErrShapeInvalid] = seen[ErrShapeInvalid] || errors.Is(err, ErrShapeInvalid)
		seen[ErrAnswerMismatch] = seen[ErrAnswerMismatch] || errors.Is(err, ErrAnswerMismatch)
		seen[ErrFormatViolation] = seen[ErrFormatViolation] || errors.Is(err, ErrFormatViolation)
		seen[ErrConsistencyBroken] = seen[ErrConsistencyBroken] || errors.Is(err, ErrConsistencyBroken)
	}
	// muldiv：shape / format / consistency / answer
	note(validateIntMulDiv(handBuiltNB(idIntMulDiv, "6 × 7 ＋ 3 = （  ）", "14", "略。")))
	note(validateIntMulDiv(handBuiltNB(idIntMulDiv, "60 × 7 ÷ 3 = （  ）", "140", "略。")))
	note(validateIntMulDiv(handBuiltNB(idIntMulDiv, "6 × 7 ÷ 4 = （  ）", "10", "略。")))
	note(validateIntMulDiv(handBuiltNB(idIntMulDiv, "6 × 7 ÷ 3 = （  ）", "99", "略。")))
	// time：format / consistency / answer
	note(validateTimeConv(handBuiltNB(idTimeConv, "100 秒 = （  ）分", "1.5", "略。")))
	note(validateTimeConv(handBuiltNB(idTimeConv, "5 分 = （  ）分", "5", "略。")))
	note(validateTimeConv(handBuiltNB(idTimeConv, "5 分 = （  ）秒", "50", "略。")))
	// geo：shape / format / consistency / answer
	note(validateGeoRect(handBuiltNB(idGeoRect, "一个图形，边 3 厘米，它的周长是（  ）厘米。", "12", "略。")))
	note(validateGeoRect(handBuiltNB(idGeoRect, "一个长方形，长 99 厘米，宽 5 厘米，它的周长是（  ）厘米。", "208", "略。")))
	note(validateGeoRect(handBuiltNB(idGeoRect, "一个长方形，长 5 厘米，宽 5 厘米，它的周长是（  ）厘米。", "20", "略。")))
	note(validateGeoRect(handBuiltNB(idGeoRect, "一个长方形，长 12 厘米，宽 5 厘米，它的周长是（  ）厘米。", "17", "略。")))
	for e := range map[error]struct{}{
		ErrShapeInvalid: {}, ErrAnswerMismatch: {}, ErrFormatViolation: {}, ErrConsistencyBroken: {},
	} {
		if !seen[e] {
			t.Fatalf("哨兵 %v 未被第三阶验证器覆盖", e)
		}
	}
}

// TestStructuralDiversityW6M3 三新母题的结构轴覆盖（确定性探针：空间按形态
// 分段连续构造，首尾下标的形态即全谱代表；批次抽样另验形态可见性）。
func TestStructuralDiversityW6M3(t *testing.T) {
	// ⑧ 乘除混合：空间前段 MD、尾段 DM（构造序）
	md, _ := Get(idIntMulDiv)
	head, err := md.Instance(0)
	if err != nil {
		t.Fatalf("muldiv head: %v", err)
	}
	tail, err := md.Instance(md.Size() - 1)
	if err != nil {
		t.Fatalf("muldiv tail: %v", err)
	}
	if head.Lineage["params"].(map[string]any)["normalized"].(map[string]any)["form"] != "M" {
		t.Fatal("muldiv 空间首参数点应为 MD 形态")
	}
	if tail.Lineage["params"].(map[string]any)["normalized"].(map[string]any)["form"] != "D" {
		t.Fatal("muldiv 空间尾参数点应为 DM 形态")
	}

	// ⑨ 时间换算：空间构造序为 from 单位升级序——首点 秒→分（缩小），
	// 尾点 时→分（放大），两端即双向代表
	tg, _ := Get(idTimeConv)
	headT, _ := tg.Instance(0)
	tailT, _ := tg.Instance(tg.Size() - 1)
	hn := headT.Lineage["params"].(map[string]any)["normalized"].(map[string]any)
	tn := tailT.Lineage["params"].(map[string]any)["normalized"].(map[string]any)
	if hn["from_unit"] != "秒" || hn["to_unit"] != "分" {
		t.Fatalf("time 首参数点应为 秒→分（缩小方向），得 %v→%v", hn["from_unit"], hn["to_unit"])
	}
	if tn["from_unit"] != "时" || tn["to_unit"] != "分" {
		t.Fatalf("time 尾参数点应为 时→分（放大方向），得 %v→%v", tn["from_unit"], tn["to_unit"])
	}

	// ⑩ 周长面积：四形态 KP 各就各位（rect 对交错、square 对殿后）
	gg, _ := Get(idGeoRect)
	wantKP := []string{
		"math.geo.rect.perimeter", "math.geo.rect.area",
		"math.geo.square.perimeter", "math.geo.square.area",
	}
	probes := []int{0, 1, gg.Size() - 2, gg.Size() - 1}
	for i, idx := range probes {
		inst, err := gg.Instance(idx)
		if err != nil {
			t.Fatalf("geo idx=%d: %v", idx, err)
		}
		kp := inst.Objective["kp_set"].([]any)[0].(map[string]any)["code"].(string)
		if kp != wantKP[i] {
			t.Fatalf("geo idx=%d kp=%q，期望 %q", idx, kp, wantKP[i])
		}
	}

	// 批次内形态可见性（固定种子，装配层抽样视角）
	recs, _, err := Run(Options{TemplateID: idIntMulDiv, N: 40, Seed: 5})
	if err != nil {
		t.Fatalf("muldiv 批次失败: %v", err)
	}
	forms := map[string]bool{}
	for _, r := range recs {
		forms[r.Lineage["params"].(map[string]any)["normalized"].(map[string]any)["form"].(string)] = true
	}
	if !forms["M"] || !forms["D"] {
		t.Fatalf("乘除混合双向未齐备：%v", forms)
	}
}
