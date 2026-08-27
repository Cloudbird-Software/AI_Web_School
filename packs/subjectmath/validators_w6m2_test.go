package subjectmath

import (
	"errors"
	"strings"
	"testing"
)

// validators_w6m2_test.go —— 第二阶 4 母题（④近似数⑤同分母分数加减⑥小数比较
// ⑦整数进退位加减）的验证器防共谋与负例组：
//   - 地面真值：手写题干/答案直接喂验证器，不经过生成器——生成器与验证器
//     同向同错会在这组立刻暴露（历史 bug：half-down 舍入、尾数前导零漏补、
//     忘进位/忘退位、位数陷阱 1.3>1.28 全部在此钉死）；
//   - 负例：每个新母题 ≥15 条故意坏实例，且必须落在预期的哨兵类别上。
//
// 手工实例走 handBuiltNB / handBuiltChoice（绕开生成器装配）。

// wantClass 断言 err 非空且属于预期哨兵类别。
func wantClass(t *testing.T, err error, want error, name string) {
	t.Helper()
	if err == nil {
		t.Errorf("[%s] 坏实例未被拒", name)
		return
	}
	if !errors.Is(err, want) {
		t.Errorf("[%s] 错误类别不符: got %v want %v", name, err, want)
	}
}

// mutClass 在 wantClass 基础上累计负例计数（每母题 ≥15 条的断言口径）。
func mutClass(t *testing.T, n *int, err error, want error, name string) {
	t.Helper()
	*n++
	wantClass(t, err, want, name)
}

// handBuiltNB 手工拼装数值填空实例（绕开生成器）。
func handBuiltNB(tplID, stem, ans, expl string) *Instance {
	return &Instance{
		TemplateID:        tplID,
		TemplateVersionID: "sha256:handmade-for-test",
		Locale:            "zh-CN",
		Objective:         objective("math.nal.integer.round", "apply", "M"),
		InteractionRef: map[string]any{
			"interaction_id":     "numeric_blank",
			"interaction_params": map[string]any{"blank_ids": []any{"b1"}},
		},
		Content: map[string]any{
			"blocks":      []any{textBlock(stem, stem)},
			"answer":      map[string]any{"blank_id": "b1", "value": ans},
			"explanation": expl,
		},
		ScoringRef: map[string]any{
			"scorer_id":     "exact_match",
			"scorer_params": map[string]any{"answer": ans, "blank_id": "b1"},
		},
		ErrorBindings: []map[string]any{{
			"subject":         "blank:b1",
			"error_type_id":   "err.round.nearest.mismatch",
			"confidence_rule": "answer-value-neq-implies-error",
		}},
		Lineage: lineage("sha256:handmade-for-test", map[string]any{}),
	}
}

// handBuiltChoice 手工拼装单选实例（正解恒居 A 位，便于手写解析句）。
func handBuiltChoice(tplID, stem, expl string, labels []string, ansLabel string) *Instance {
	blocks := make([]any, 0, len(labels)+1)
	blocks = append(blocks, textBlock(stem, stem))
	optionParams := make([]any, 0, len(labels))
	errBinds := make([]map[string]any, 0, len(labels)-1)
	ansLet := "A"
	for i, lab := range labels {
		let := string(rune('A' + i))
		blocks = append(blocks, textBlock(let+". {"+let+"}", let+". "+lab))
		optionParams = append(optionParams, map[string]any{"id": let, "label": lab})
		if lab == ansLabel {
			ansLet = let
		} else {
			errBinds = append(errBinds, map[string]any{
				"subject":         "option:" + let,
				"error_type_id":   "err.test.handmade",
				"confidence_rule": "selected-option-equals-subject",
			})
		}
	}
	return &Instance{
		TemplateID:        tplID,
		TemplateVersionID: "sha256:handmade-for-test",
		Locale:            "zh-CN",
		Objective:         objective("math.nal.integer.mul", "apply", "M"),
		InteractionRef: map[string]any{
			"interaction_id":     "single_choice",
			"interaction_params": map[string]any{"options": optionParams},
		},
		Content: map[string]any{
			"blocks":      blocks,
			"answer":      map[string]any{"letter": ansLet, "value": ansLabel},
			"explanation": expl,
		},
		ScoringRef: map[string]any{
			"scorer_id":     "exact_match",
			"scorer_params": map[string]any{"answer": ansLabel},
		},
		ErrorBindings: errBinds,
		Lineage:       lineage("sha256:handmade-for-test", map[string]any{}),
	}
}

// ────────────────────────────────────────────────────────────────
// 母题④ 四舍五入近似数：地面真值 + 17 条负例
// ────────────────────────────────────────────────────────────────

func TestValidateIntRoundGroundTruth(t *testing.T) {
	truths := []struct{ stem, ans, expl string }{
		{"1299 ≈ （  ）（四舍五入到百位）", "1300",
			"1299 四舍五入到百位：省略的尾数是 99，尾数首位 9，满 5，向前一位进 1，得 1300。"},
		// half-up 半单位（银行家舍入会给 1000——正是要钉死的分歧点）
		{"1050 ≈ （  ）（四舍五入到百位）", "1100",
			"1050 四舍五入到百位：省略的尾数是 50，尾数首位 5，满 5，向前一位进 1，得 1100。"},
		// 连续进位越到五位数
		{"9999 ≈ （  ）（四舍五入到千位）", "10000",
			"9999 四舍五入到千位：省略的尾数是 999，尾数首位 9，满 5，向前一位进 1，得 10000。"},
		// 尾数前导零补齐后判首位（105→百位应舍去得 100，而非误进到 200）
		{"105 ≈ （  ）（四舍五入到百位）", "100",
			"105 四舍五入到百位：省略的尾数是 5，视作 05 后首位是 0，不满 5，直接舍去，得 100。"},
		{"2985 ≈ （  ）（四舍五入到十位）", "2990",
			"2985 四舍五入到十位：省略的尾数是 5，尾数首位 5，满 5，向前一位进 1，得 2990。"},
		// 尾数全零：四舍五入后等于原数
		{"1200 ≈ （  ）（四舍五入到百位）", "1200",
			"1200 四舍五入到百位：省略的尾数是 0，首位 0，不满 5，直接舍去，得 1200。"},
	}
	for _, tc := range truths {
		inst := handBuiltNB(idIntRound, tc.stem, tc.ans, tc.expl)
		if err := validateIntRound(inst); err != nil {
			t.Errorf("地面真值 [%s=%s] 被验证器误杀: %v", tc.stem, tc.ans, err)
		}
	}

	// 反向/边界错答（历史 bug 形态：方向反了、half-down 银行家舍入）
	wrongs := []struct{ stem, wrongAns string }{
		{"1949 ≈ （  ）（四舍五入到百位）", "2000"}, // 49 < 50 应舍去，误进位
		{"2985 ≈ （  ）（四舍五入到十位）", "2980"}, // 半单位应进一，half-down 错答
		{"1050 ≈ （  ）（四舍五入到百位）", "1000"}, // 半单位应进一，half-down 错答
		{"105 ≈ （  ）（四舍五入到百位）", "200"},   // 尾数未补前导零误判首位
	}
	for _, tc := range wrongs {
		inst := handBuiltNB(idIntRound, tc.stem, tc.wrongAns,
			tc.stem+" 的近似数是 "+tc.wrongAns+"。")
		err := validateIntRound(inst)
		wantClass(t, err, ErrAnswerMismatch, "round-wrong["+tc.stem+"="+tc.wrongAns+"]")
	}
}

func TestValidateIntRoundRejectsMutants(t *testing.T) {
	base := firstValid(t, idIntRound, 7)
	n := 0

	cp := cloneOf(t, base)
	dig(t, cp.Content, "answer")["value"] = "0"
	mutClass(t, &n, Validate(cp), ErrAnswerMismatch, "①answer-value")

	cp = cloneOf(t, base)
	dig(t, cp.ScoringRef, "scorer_params")["answer"] = "0"
	mutClass(t, &n, Validate(cp), ErrAnswerMismatch, "②scorer-answer")

	cp = cloneOf(t, base)
	put2(cp.Content["blocks"].([]any)[0].(map[string]any), "rendered", "1299 = （  ）（四舍五入到百位）")
	mutClass(t, &n, Validate(cp), ErrShapeInvalid, "③no-approx-symbol")

	cp = cloneOf(t, base)
	put2(cp.Content["blocks"].([]any)[0].(map[string]any), "rendered", "1299 ≈ （  ）（用四舍五入法）")
	mutClass(t, &n, Validate(cp), ErrShapeInvalid, "④no-place-word")

	cp = cloneOf(t, base)
	put2(cp.Content["blocks"].([]any)[0].(map[string]any), "rendered", "99 ≈ （  ）（四舍五入到十位）")
	mutClass(t, &n, Validate(cp), ErrFormatViolation, "⑤value-below-domain")

	cp = cloneOf(t, base)
	put2(cp.Content["blocks"].([]any)[0].(map[string]any), "rendered", "12345 ≈ （  ）（四舍五入到百位）")
	mutClass(t, &n, Validate(cp), ErrFormatViolation, "⑥value-above-domain")

	cp = cloneOf(t, base)
	put2(cp.Content["blocks"].([]any)[0].(map[string]any), "rendered", "999 ≈ （  ）（四舍五入到千位）")
	mutClass(t, &n, Validate(cp), ErrFormatViolation, "⑦thousand-on-3digit")

	cp = cloneOf(t, base)
	delete(dig(t, cp.Content, "answer"), "blank_id")
	mutClass(t, &n, Validate(cp), ErrShapeInvalid, "⑧blank-id")

	cp = cloneOf(t, base)
	put2(cp.Content, "explanation", "约等于某个数。")
	mutClass(t, &n, Validate(cp), ErrConsistencyBroken, "⑨explanation-thin")

	cp = cloneOf(t, base)
	put2(cp.Content, "explanation", "取近似数即可。")
	mutClass(t, &n, Validate(cp), ErrConsistencyBroken, "⑩explanation-no-place")

	cp = cloneOf(t, base)
	cp.ErrorBindings[0]["subject"] = "blank:b2"
	mutClass(t, &n, Validate(cp), ErrConsistencyBroken, "⑪binding-subject")

	cp = cloneOf(t, base)
	cp.ErrorBindings = nil
	mutClass(t, &n, Validate(cp), ErrShapeInvalid, "⑫bindings-empty")

	cp = cloneOf(t, base)
	cp.InteractionRef = map[string]any{"interaction_id": "single_choice"}
	mutClass(t, &n, Validate(cp), ErrShapeInvalid, "⑬interaction")

	cp = cloneOf(t, base)
	cp.Locale = "en-US"
	mutClass(t, &n, Validate(cp), ErrShapeInvalid, "⑭locale")

	cp = cloneOf(t, base)
	cp.Lineage["tier"] = "B"
	mutClass(t, &n, Validate(cp), ErrShapeInvalid, "⑮tier")

	cp = cloneOf(t, base)
	dig(t, cp.ScoringRef, "scorer_params")["answer"] = "1299四舍五入" // 非规范数字串
	mutClass(t, &n, Validate(cp), ErrAnswerMismatch, "⑯non-numeric-answer")

	cp = cloneOf(t, base)
	put2(cp.Content["blocks"].([]any)[0].(map[string]any), "rendered", "1299 ≈ （  ）（四舍五入到万位）")
	mutClass(t, &n, Validate(cp), ErrShapeInvalid, "⑰unknown-place")

	if n < 15 {
		t.Fatalf("负例数 %d < 15", n)
	}
}

// ────────────────────────────────────────────────────────────────
// 母题⑤ 同分母分数加减：地面真值 + 负例
// ────────────────────────────────────────────────────────────────

func TestValidateFracAddSubGroundTruth(t *testing.T) {
	truths := []struct {
		stem, expl string
		labels     []string
		ans        string
	}{
		{"计算：3/8 ＋ 2/8 = ？",
			"同分母分数相加，分母不变，分子相加：3/8 ＋ 2/8 = 5/8，所以选 A。",
			[]string{"5/8", "3/8", "1/2", "7/8"}, "5/8"},
		// 结果需化简：2/4 → 1/2（答案必须是最简形式）
		{"计算：1/4 ＋ 1/4 = ？",
			"同分母分数相加，分母不变，分子相加：1/4 ＋ 1/4 = 2/4 = 1/2，所以选 A。",
			[]string{"1/2", "1/4", "3/4", "1/8"}, "1/2"},
		{"计算：7/12 － 5/12 = ？",
			"同分母分数相减，分母不变，分子相减：7/12 － 5/12 = 2/12 = 1/6，所以选 A。",
			[]string{"1/6", "1/2", "5/12", "11/12"}, "1/6"},
		{"计算：5/9 － 2/9 = ？",
			"同分母分数相减，分母不变，分子相减：5/9 － 2/9 = 3/9 = 1/3，所以选 A。",
			[]string{"1/3", "2/9", "1/9", "2/3"}, "1/3"},
		{"口算：3/8 ＋ 1/8 = ？",
			"同分母分数相加，分母不变，分子相加：3/8 ＋ 1/8 = 4/8 = 1/2，所以选 A。",
			[]string{"1/2", "1/8", "3/8", "5/8"}, "1/2"},
	}
	for _, tc := range truths {
		inst := handBuiltChoice(idFracAddSub, tc.stem, tc.expl, tc.labels, tc.ans)
		if err := validateFracAddSub(inst); err != nil {
			t.Errorf("地面真值 [%s=%s] 被验证器误杀: %v", tc.stem, tc.ans, err)
		}
	}

	// 经典错答必须被拒：未化简答案、分母也相加
	wrongs := []struct {
		name, stem, ans string
	}{
		{"unreduced-answer", "计算：1/4 ＋ 1/4 = ？", "2/4"},
		{"denominator-added", "计算：1/8 ＋ 3/8 = ？", "4/16"},
		{"sign-slip", "计算：7/12 － 5/12 = ？", "1/2"},
	}
	for _, tc := range wrongs {
		inst := handBuiltChoice(idFracAddSub, tc.stem, "硬算得到 "+tc.ans+"，选 A。",
			[]string{tc.ans, "1/2", "3/4", "1/8"}, tc.ans)
		err := validateFracAddSub(inst)
		wantClass(t, err, ErrAnswerMismatch, "fracadd-wrong-"+tc.name)
	}
}

func TestValidateFracAddSubRejectsMutants(t *testing.T) {
	base := firstValid(t, idFracAddSub, 5)
	n := 0

	cp := cloneOf(t, base)
	ansVal := dig(t, cp.Content, "answer")["value"].(string)
	distractorLet := ""
	for _, b := range cp.Content["blocks"].([]any)[1:] {
		rendered := b.(map[string]any)["rendered"].(string)
		parts := strings.SplitN(rendered, ". ", 2)
		if len(parts) == 2 && parts[1] != ansVal {
			distractorLet = parts[0]
			break
		}
	}
	dig(t, cp.Content, "answer")["letter"] = distractorLet
	mutClass(t, &n, Validate(cp), ErrAnswerMismatch, "①letter-flip")

	cp = cloneOf(t, base)
	dig(t, cp.ScoringRef, "scorer_params")["answer"] = "1/9"
	mutClass(t, &n, Validate(cp), ErrAnswerMismatch, "②scorer-answer")

	cp = cloneOf(t, base)
	blocks := cp.Content["blocks"].([]any)
	prevLabel := strings.SplitN(blocks[2].(map[string]any)["rendered"].(string), ". ", 2)[1]
	blocks[3].(map[string]any)["rendered"] = "C. " + prevLabel
	mutClass(t, &n, Validate(cp), ErrConsistencyBroken, "③dup-options")

	cp = cloneOf(t, base)
	cp.Content["blocks"].([]any)[2].(map[string]any)["rendered"] = "B. 2/4"
	mutClass(t, &n, Validate(cp), ErrFormatViolation, "④unreduced-option")

	cp = cloneOf(t, base)
	cp.Content["blocks"].([]any)[2].(map[string]any)["rendered"] = "B. 0.5"
	mutClass(t, &n, Validate(cp), ErrFormatViolation, "⑤non-fraction-option")

	cp = cloneOf(t, base)
	put2(cp.Content["blocks"].([]any)[0].(map[string]any), "rendered", "计算：1/8 ＋ 1/4 = ？")
	mutClass(t, &n, Validate(cp), ErrConsistencyBroken, "⑥diff-denominator")

	cp = cloneOf(t, base)
	put2(cp.Content["blocks"].([]any)[0].(map[string]any), "rendered", "计算：5/8 ＋ 4/8 = ？")
	mutClass(t, &n, Validate(cp), ErrConsistencyBroken, "⑦sum-exceeds-unit")

	cp = cloneOf(t, base)
	put2(cp.Content["blocks"].([]any)[0].(map[string]any), "rendered", "计算：2/8 － 5/8 = ？")
	mutClass(t, &n, Validate(cp), ErrConsistencyBroken, "⑧negative-diff")

	cp = cloneOf(t, base)
	put2(cp.Content["blocks"].([]any)[0].(map[string]any), "rendered", "计算：1/8 和 3/8 = ？")
	mutClass(t, &n, Validate(cp), ErrShapeInvalid, "⑨no-operator")

	cp = cloneOf(t, base)
	put2(cp.Content["blocks"].([]any)[0].(map[string]any), "rendered", "计算：1/8 ＋ 3/8 － 2/8 = ？")
	mutClass(t, &n, Validate(cp), ErrShapeInvalid, "⑩both-operators")

	cp = cloneOf(t, base)
	put2(cp.Content["blocks"].([]any)[0].(map[string]any), "rendered", "计算：1/8 ＋ 3/8 ＋ 2/8 = ？")
	mutClass(t, &n, Validate(cp), ErrConsistencyBroken, "⑪three-fractions")

	cp = cloneOf(t, base)
	put2(cp.Content["blocks"].([]any)[0].(map[string]any), "rendered", "计算：0/8 ＋ 3/8 = ？")
	mutClass(t, &n, Validate(cp), ErrFormatViolation, "⑫zero-numerator")

	cp = cloneOf(t, base)
	put2(cp.Content["blocks"].([]any)[0].(map[string]any), "rendered", "计算：8/8 ＋ 3/8 = ？")
	mutClass(t, &n, Validate(cp), ErrFormatViolation, "⑬non-proper-operand")

	cp = cloneOf(t, base)
	put2(cp.Content, "explanation", "看出来的，选 A。")
	mutClass(t, &n, Validate(cp), ErrConsistencyBroken, "⑭explanation-thin")

	cp = cloneOf(t, base)
	cp.ErrorBindings = cp.ErrorBindings[:len(cp.ErrorBindings)-1]
	mutClass(t, &n, Validate(cp), ErrConsistencyBroken, "⑮bindings-count")

	cp = cloneOf(t, base)
	cp.Content["blocks"] = cp.Content["blocks"].([]any)[:3]
	mutClass(t, &n, Validate(cp), ErrShapeInvalid, "⑯option-count")

	cp = cloneOf(t, base)
	cp.Content["blocks"].([]any)[3].(map[string]any)["rendered"] = "A. 9/8"
	mutClass(t, &n, Validate(cp), ErrShapeInvalid, "⑰letters-broken")

	if n < 15 {
		t.Fatalf("负例数 %d < 15", n)
	}
}

// ────────────────────────────────────────────────────────────────
// 母题⑥ 小数大小比较：地面真值 + 负例
// ────────────────────────────────────────────────────────────────

func TestValidateDecCompareGroundTruth(t *testing.T) {
	truths := []struct {
		stem, expl string
		labels     []string
		ans        string
	}{
		{"0.5 和 0.8 比较大小：更大的数是（  ）",
			"比较 0.5 与 0.8：补零对齐成 1 位小数：0.8 与 0.5，因为 0.8 > 0.5，所以更大的数是 0.8，选 A。",
			[]string{"0.8", "0.5", "0.3", "0.9"}, "0.8"},
		// 位数陷阱：1.3 > 1.28（位数多不等于大——经典迷思的钉死点）
		{"1.3 和 1.28 比较大小：更大的数是（  ）",
			"比较 1.3 与 1.28：补零对齐成 2 位小数：1.30 与 1.28，因为 1.30 > 1.28，所以更大的数是 1.3，选 A。",
			[]string{"1.3", "1.28", "1.2", "1.5"}, "1.3"},
		{"0.05 和 0.5 比较大小：更大的数是（  ）",
			"比较 0.05 与 0.5：补零对齐成 2 位小数：0.50 与 0.05，因为 0.50 > 0.05，所以更大的数是 0.5，选 A。",
			[]string{"0.5", "0.05", "0.2", "0.7"}, "0.5"},
		{"1.05 和 1.5 比较大小：更大的数是（  ）",
			"比较 1.05 与 1.5：补零对齐成 2 位小数：1.50 与 1.05，因为 1.50 > 1.05，所以更大的数是 1.5，选 A。",
			[]string{"1.5", "1.05", "1.2", "1.9"}, "1.5"},
	}
	for _, tc := range truths {
		inst := handBuiltChoice(idDecCmp, tc.stem, tc.expl, tc.labels, tc.ans)
		if err := validateDecCompare(inst); err != nil {
			t.Errorf("地面真值 [%s=%s] 被验证器误杀: %v", tc.stem, tc.ans, err)
		}
	}

	// 位数迷思错答（位数多当大数）必须被拒
	wrongs := []struct{ stem, wrongAns string }{
		{"1.3 和 1.28 比较大小：更大的数是（  ）", "1.28"},
		{"0.05 和 0.5 比较大小：更大的数是（  ）", "0.05"},
	}
	for _, tc := range wrongs {
		inst := handBuiltChoice(idDecCmp, tc.stem, "位数多的更大，是 "+tc.wrongAns+"，选 A。",
			[]string{tc.wrongAns, "1.3", "0.8", "0.2"}, tc.wrongAns)
		err := validateDecCompare(inst)
		wantClass(t, err, ErrAnswerMismatch, "deccmp-wrong["+tc.wrongAns+"]")
	}
}

func TestValidateDecCompareRejectsMutants(t *testing.T) {
	base := firstValid(t, idDecCmp, 11)
	n := 0

	cp := cloneOf(t, base)
	dig(t, cp.Content, "answer")["value"] = "0"
	mutClass(t, &n, Validate(cp), ErrAnswerMismatch, "①answer-value")

	cp = cloneOf(t, base)
	dig(t, cp.ScoringRef, "scorer_params")["answer"] = "0"
	mutClass(t, &n, Validate(cp), ErrAnswerMismatch, "②scorer-answer")

	cp = cloneOf(t, base)
	cp.Content["blocks"].([]any)[2].(map[string]any)["rendered"] = "B. 1.30"
	mutClass(t, &n, Validate(cp), ErrFormatViolation, "③trailing-zero-option")

	cp = cloneOf(t, base)
	cp.Content["blocks"].([]any)[3].(map[string]any)["rendered"] = "C. 01.2"
	mutClass(t, &n, Validate(cp), ErrFormatViolation, "④leading-zero-option")

	cp = cloneOf(t, base)
	blocks := cp.Content["blocks"].([]any)
	prevLabel := strings.SplitN(blocks[2].(map[string]any)["rendered"].(string), ". ", 2)[1]
	blocks[3].(map[string]any)["rendered"] = "C. " + prevLabel
	mutClass(t, &n, Validate(cp), ErrConsistencyBroken, "⑤dup-options")

	cp = cloneOf(t, base)
	put2(cp.Content["blocks"].([]any)[0].(map[string]any), "rendered", "0.5 和 0.50 比较大小：更大的数是（  ）")
	mutClass(t, &n, Validate(cp), ErrFormatViolation, "⑥equal-value-noncanon")

	cp = cloneOf(t, base)
	put2(cp.Content["blocks"].([]any)[0].(map[string]any),
		"rendered", "0.5 和 0.8 与 0.9 比较大小：更大的数是（  ）")
	mutClass(t, &n, Validate(cp), ErrConsistencyBroken, "⑦three-decimals")

	cp = cloneOf(t, base)
	put2(cp.Content["blocks"].([]any)[0].(map[string]any), "rendered", "3 和 1.2 比较大小：更大的数是（  ）")
	mutClass(t, &n, Validate(cp), ErrConsistencyBroken, "⑧integer-operand")

	cp = cloneOf(t, base)
	put2(cp.Content["blocks"].([]any)[0].(map[string]any), "rendered", "99.5 和 1.2 比较大小：更大的数是（  ）")
	mutClass(t, &n, Validate(cp), ErrFormatViolation, "⑨out-of-domain")

	cp = cloneOf(t, base)
	put2(cp.Content, "explanation", "直接看出来的，选 A。")
	mutClass(t, &n, Validate(cp), ErrConsistencyBroken, "⑩explanation-thin")

	cp = cloneOf(t, base)
	put2(cp.Content, "explanation", "比较一下就知道了：1.30 更大，选 A。")
	mutClass(t, &n, Validate(cp), ErrConsistencyBroken, "⑪explanation-half-elements")

	cp = cloneOf(t, base)
	cp.ErrorBindings = cp.ErrorBindings[:len(cp.ErrorBindings)-1]
	mutClass(t, &n, Validate(cp), ErrConsistencyBroken, "⑫bindings-count")

	cp = cloneOf(t, base)
	ansVal := dig(t, cp.Content, "answer")["value"].(string)
	distractorLet := ""
	for _, b := range cp.Content["blocks"].([]any)[1:] {
		rendered := b.(map[string]any)["rendered"].(string)
		parts := strings.SplitN(rendered, ". ", 2)
		if len(parts) == 2 && parts[1] != ansVal {
			distractorLet = parts[0]
			break
		}
	}
	dig(t, cp.Content, "answer")["letter"] = distractorLet
	mutClass(t, &n, Validate(cp), ErrAnswerMismatch, "⑬letter-flip")

	cp = cloneOf(t, base)
	cp.Content["blocks"].([]any)[3].(map[string]any)["rendered"] = "C"
	mutClass(t, &n, Validate(cp), ErrShapeInvalid, "⑭malformed-option")

	cp = cloneOf(t, base)
	cp.InteractionRef = map[string]any{"interaction_id": "numeric_blank"}
	mutClass(t, &n, Validate(cp), ErrShapeInvalid, "⑮interaction")

	cp = cloneOf(t, base)
	cp.Lineage["tier"] = "C"
	mutClass(t, &n, Validate(cp), ErrShapeInvalid, "⑯tier")

	if n < 15 {
		t.Fatalf("负例数 %d < 15", n)
	}
}

// ────────────────────────────────────────────────────────────────
// 母题⑦ 整数进位加/退位减：地面真值 + 负例
// ────────────────────────────────────────────────────────────────

func TestValidateIntAddSubGroundTruth(t *testing.T) {
	truths := []struct{ stem, ans, expl string }{
		{"358 ＋ 79 = （  ）", "437",
			"358 ＋ 79：个位 8＋9=17，满十向十位进 1，得 437。"},
		{"403 － 56 = （  ）", "347",
			"403 － 56：个位 3 减 6 不够减，向十位借 1 再减，得 347。"},
		// 情境式（无算符，关键词锚点）
		{"文具店上午卖出 129 张贴纸，下午卖出 88 张，全天一共卖出（  ）张贴纸。", "217",
			"129 ＋ 88：个位 9＋8=17，满十向十位进 1，得 217。"},
		{"果园里有 305 个苹果，摘走了 48 个，还剩（  ）个苹果。", "257",
			"305 － 48：个位 5 减 8 不够减，向十位借 1 再减，得 257。"},
		{"算一算：567 ＋ 78 = （  ）", "645",
			"567 ＋ 78：个位 7＋8=15，满十向十位进 1，得 645。"},
		{"100 － 99 = （  ）", "1",
			"100 － 99：个位 0 减 9 不够减，向十位借 1 再减，得 1。"},
	}
	for _, tc := range truths {
		inst := handBuiltNB(idIntAddSub, tc.stem, tc.ans, tc.expl)
		if err := validateIntAddSub(inst); err != nil {
			t.Errorf("地面真值 [%s=%s] 被验证器误杀: %v", tc.stem, tc.ans, err)
		}
	}

	// 历史错答形态：忘进位、退位错
	wrongs := []struct{ stem, wrongAns string }{
		{"358 ＋ 79 = （  ）", "427"}, // 忘加进位 1
		{"403 － 56 = （  ）", "357"}, // 退位错
		{"567 ＋ 78 = （  ）", "635"}, // 忘加进位 1
	}
	for _, tc := range wrongs {
		inst := handBuiltNB(idIntAddSub, tc.stem, tc.wrongAns,
			"口算得到 "+tc.wrongAns+"。")
		err := validateIntAddSub(inst)
		wantClass(t, err, ErrAnswerMismatch, "addsub-wrong["+tc.stem+"="+tc.wrongAns+"]")
	}
}

func TestValidateIntAddSubRejectsMutants(t *testing.T) {
	base := firstValid(t, idIntAddSub, 9)
	n := 0

	cp := cloneOf(t, base)
	dig(t, cp.Content, "answer")["value"] = "0"
	mutClass(t, &n, Validate(cp), ErrAnswerMismatch, "①answer-value")

	cp = cloneOf(t, base)
	dig(t, cp.ScoringRef, "scorer_params")["answer"] = "0"
	mutClass(t, &n, Validate(cp), ErrAnswerMismatch, "②scorer-answer")

	// 无进位的加法（超空间约束；答案本身正确，命中的是性质断言）
	cp = cloneOf(t, base)
	put2(cp.Content["blocks"].([]any)[0].(map[string]any), "rendered", "123 ＋ 85 = （  ）")
	mutClass(t, &n, Validate(cp), ErrConsistencyBroken, "③no-carry")

	// 无退位的减法（超空间约束）
	cp = cloneOf(t, base)
	put2(cp.Content["blocks"].([]any)[0].(map[string]any), "rendered", "567 － 43 = （  ）")
	mutClass(t, &n, Validate(cp), ErrConsistencyBroken, "④no-borrow")

	cp = cloneOf(t, base)
	put2(cp.Content["blocks"].([]any)[0].(map[string]any), "rendered", "99 ＋ 12 = （  ）")
	mutClass(t, &n, Validate(cp), ErrFormatViolation, "⑤a-below-domain")

	cp = cloneOf(t, base)
	put2(cp.Content["blocks"].([]any)[0].(map[string]any), "rendered", "345 ＋ 7 = （  ）")
	mutClass(t, &n, Validate(cp), ErrFormatViolation, "⑥b-below-domain")

	cp = cloneOf(t, base)
	put2(cp.Content["blocks"].([]any)[0].(map[string]any), "rendered", "567 ＋ 476 = （  ）")
	mutClass(t, &n, Validate(cp), ErrFormatViolation, "⑦b-above-domain")

	cp = cloneOf(t, base)
	put2(cp.Content["blocks"].([]any)[0].(map[string]any), "rendered", "358 ＋ 247 － 100 = （  ）")
	mutClass(t, &n, Validate(cp), ErrShapeInvalid, "⑧both-operators")

	cp = cloneOf(t, base)
	put2(cp.Content["blocks"].([]any)[0].(map[string]any), "rendered", "358 与 247 的关系是（  ）")
	mutClass(t, &n, Validate(cp), ErrShapeInvalid, "⑨no-op-no-keyword")

	cp = cloneOf(t, base)
	put2(cp.Content["blocks"].([]any)[0].(map[string]any), "rendered", "第 3 天：358 ＋ 247 = （  ）")
	mutClass(t, &n, Validate(cp), ErrConsistencyBroken, "⑩three-tokens")

	cp = cloneOf(t, base)
	stemStr := stemRendered(t, cp)
	put2(cp.Content["blocks"].([]any)[0].(map[string]any),
		"rendered", strings.Replace(stemStr, "（  ）", "____", 1))
	mutClass(t, &n, Validate(cp), ErrShapeInvalid, "⑪no-blank")

	cp = cloneOf(t, base)
	put2(cp.Content, "explanation", "口算可得。")
	mutClass(t, &n, Validate(cp), ErrConsistencyBroken, "⑫explanation-thin")

	cp = cloneOf(t, base)
	delete(dig(t, cp.Content, "answer"), "blank_id")
	mutClass(t, &n, Validate(cp), ErrShapeInvalid, "⑬blank-id")

	cp = cloneOf(t, base)
	cp.ErrorBindings[0]["subject"] = "option:A"
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

	if n < 15 {
		t.Fatalf("负例数 %d < 15", n)
	}
}

// TestNewValidatorsNoCrossTemplateLeak 新验证器对异母题实例必须拒绝
// （Validate 分发正确性的补充断言：拿母题④的实例喂母题⑦的验证器必炸）。
func TestNewValidatorsNoCrossTemplateLeak(t *testing.T) {
	round := firstValid(t, idIntRound, 3)
	if err := validateIntAddSub(round); err == nil {
		t.Fatal("round 实例喂 addsub 验证器必须被拒")
	}
	addsub := firstValid(t, idIntAddSub, 3)
	if err := validateIntRound(addsub); err == nil {
		t.Fatal("addsub 实例喂 round 验证器必须被拒")
	}
	fa := firstValid(t, idFracAddSub, 3)
	if err := validateDecCompare(fa); err == nil {
		t.Fatal("frac-addsub 实例喂 dec-cmp 验证器必须被拒")
	}
	dc := firstValid(t, idDecCmp, 3)
	if err := validateFracAddSub(dc); err == nil {
		t.Fatal("dec-cmp 实例喂 frac-addsub 验证器必须被拒")
	}
}

// TestValidatorSentinelCoverageW6M2 确认四个新验证器的拒绝都落在四类哨兵上
// （抽样断言：每类哨兵至少被一个新验证器产出，拒绝原因分布口径可用）。
func TestValidatorSentinelCoverageW6M2(t *testing.T) {
	seen := map[error]bool{}
	check := func(inst *Instance, fn func(*Instance) error) {
		t.Helper()
		if err := fn(inst); err != nil {
			seen[ErrShapeInvalid] = seen[ErrShapeInvalid] || errors.Is(err, ErrShapeInvalid)
			seen[ErrAnswerMismatch] = seen[ErrAnswerMismatch] || errors.Is(err, ErrAnswerMismatch)
			seen[ErrFormatViolation] = seen[ErrFormatViolation] || errors.Is(err, ErrFormatViolation)
			seen[ErrConsistencyBroken] = seen[ErrConsistencyBroken] || errors.Is(err, ErrConsistencyBroken)
		}
	}
	check(handBuiltNB(idIntRound, "99 ≈ （  ）（四舍五入到十位）", "100", "略。"), validateIntRound)
	check(handBuiltNB(idIntRound, "1299 = （  ）（四舍五入到百位）", "1300", "略。"), validateIntRound)
	check(handBuiltNB(idIntRound, "1299 ≈ （  ）（四舍五入到百位）", "9999", "略。"), validateIntRound)
	check(handBuiltNB(idIntRound, "1299 ≈ （  ）（四舍五入到百位）", "1300", "略。"), validateIntRound)
	for e, hit := range seen {
		if !hit {
			t.Fatalf("哨兵 %v 未被新验证器覆盖", e)
		}
	}
	if len(seen) != 4 {
		t.Fatalf("哨兵覆盖数=%d，应为 4", len(seen))
	}
}
