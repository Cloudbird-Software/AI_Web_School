package subjectmath

import (
	"errors"
	"strings"
	"testing"
)

// validators_test.go：故意坏实例负例——每一处篡改都必须被独立验证器抓住，
// 且落在预期的哨兵类别上（拒绝原因分布的口径依据）。
// 另含「地面真值」组：手写题干/答案直接喂验证器，不经过生成器——
// 若生成器与验证器同向同错，这组事实会立刻暴露。

func firstValid(t *testing.T, id string, idx int) *Instance {
	t.Helper()
	g, _ := Get(id)
	inst, err := g.Instance(idx)
	if err != nil {
		t.Fatalf("构造基准实例失败(%s): %v", id, err)
	}
	return inst
}

func cloneOf(t *testing.T, in *Instance) *Instance {
	t.Helper()
	cp, err := deepCopy(in)
	if err != nil {
		t.Fatalf("deepCopy: %v", err)
	}
	return cp
}

// dig 沿路径取嵌套 map（缺层即测试失败——篡改点写错立刻暴露）。
func dig(t *testing.T, root map[string]any, path ...string) map[string]any {
	t.Helper()
	cur := root
	for _, p := range path {
		next, ok := cur[p].(map[string]any)
		if !ok {
			t.Fatalf("篡改路径 %v 在 %q 处断链", path, p)
		}
		cur = next
	}
	return cur
}

func stemRendered(t *testing.T, in *Instance) string {
	t.Helper()
	blk := in.Content["blocks"].([]any)[0].(map[string]any)
	s, _ := blk["rendered"].(string)
	return s
}

func TestValidatorsAcceptGoodInstances(t *testing.T) {
	for _, tc := range []struct{ id string }{
		{idIntMul}, {idFracCmp}, {idUnitConv},
		{idIntRound}, {idFracAddSub}, {idDecCmp}, {idIntAddSub},
	} {
		if err := Validate(firstValid(t, tc.id, 7)); err != nil {
			t.Fatalf("好实例被误杀(%s): %v", tc.id, err)
		}
	}
}

func TestValidateIntMulRejectsMutants(t *testing.T) {
	base := firstValid(t, idIntMul, 7)

	expect := func(err error, want error, name string) {
		t.Helper()
		if err == nil {
			t.Errorf("[%s] 坏实例未被拒", name)
			return
		}
		if !errors.Is(err, want) {
			t.Errorf("[%s] 错误类别不符: got %v want %v", name, err, want)
		}
	}

	// ① 改答案字母 → 指向非正解（换成一个必然不同的字母）
	cp := cloneOf(t, base)
	ansM := dig(t, cp.Content, "answer")
	cur := ansM["letter"].(string)
	newLet := "A"
	if cur == "A" {
		newLet = "B"
	}
	ansM["letter"] = newLet
	expect(Validate(cp), ErrAnswerMismatch, "letter-flip")

	// ② scoring_ref 答案改成错数
	cp = cloneOf(t, base)
	dig(t, cp.ScoringRef, "scorer_params")["answer"] = "0"
	expect(Validate(cp), ErrAnswerMismatch, "scorer-answer")

	// ③ 干扰项与正解同值（多解不可解）
	cp = cloneOf(t, base)
	blocks := cp.Content["blocks"].([]any)
	product := dig(t, cp.Content, "answer")["value"].(string)
	ansLetter := dig(t, cp.Content, "answer")["letter"].(string)
	for i, b := range blocks[1:] {
		blk := b.(map[string]any)
		rendered := blk["rendered"].(string)
		if !strings.HasPrefix(rendered, ansLetter+". ") {
			put2(blk, "rendered", string(rune('A'+i))+". "+product)
			break
		}
	}
	expect(Validate(cp), ErrConsistencyBroken, "multi-solution")

	// ④ 解析中的乘积数字被改坏
	cp = cloneOf(t, base)
	put2(cp.Content, "explanation", "因为 12 × 34 = 999，所以选 A。")
	expect(Validate(cp), ErrConsistencyBroken, "explanation")

	// ⑤ 选项字母断裂（出现重复字母）
	cp = cloneOf(t, base)
	blocks = cp.Content["blocks"].([]any)
	put2(blocks[3].(map[string]any), "rendered", "B. 555")
	expect(Validate(cp), ErrShapeInvalid, "letters")

	// ⑥ 删掉一条错误绑定
	cp = cloneOf(t, base)
	cp.ErrorBindings = cp.ErrorBindings[:len(cp.ErrorBindings)-1]
	expect(Validate(cp), ErrConsistencyBroken, "bindings")
}

func put2(m map[string]any, k string, v any) { m[k] = v }

func TestValidateFracCompareRejectsMutants(t *testing.T) {
	base := firstValid(t, idFracCmp, 11)

	expectErr := func(err error, name string) {
		t.Helper()
		if err == nil {
			t.Errorf("[%s] 坏实例未被拒", name)
		}
	}

	stem := stemRendered(t, base)
	fractionsInStem := fracTokenRe.FindAllString(stem, 2)
	if len(fractionsInStem) != 2 {
		t.Fatalf("基准实例题干应含两个分数：%q", stem)
	}
	bigAnswer := dig(t, base.Content, "answer")["value"].(string)
	smallerInStem := fractionsInStem[0]
	if smallerInStem == bigAnswer {
		smallerInStem = fractionsInStem[1]
	}

	// ① answer.value/scorer 答案换成较小分数
	cp := cloneOf(t, base)
	dig(t, cp.Content, "answer")["value"] = smallerInStem
	dig(t, cp.ScoringRef, "scorer_params")["answer"] = smallerInStem
	err := Validate(cp)
	if !errors.Is(err, ErrAnswerMismatch) {
		t.Errorf("[smaller-as-answer] 应按 answer-mismatch 拒，得 %v", err)
	}

	// ② 未化简分数混入选项（格式规范破坏；4/8 数值上恰是常见错答形态）
	cp = cloneOf(t, base)
	blocks := cp.Content["blocks"].([]any)
	blocks[2].(map[string]any)["rendered"] = "B. 4/8"
	err = Validate(cp)
	if !errors.Is(err, ErrFormatViolation) {
		t.Errorf("[unreduced-option] 应按 format 拒，得 %v", err)
	}

	// ③ 两个选项标签重复（多解/撞车）
	cp = cloneOf(t, base)
	lab := cp.Content["blocks"].([]any)[2].(map[string]any)["rendered"]
	cp.Content["blocks"].([]any)[3].(map[string]any)["rendered"] = lab
	expectErr(Validate(cp), "dup-options")

	// ④ 题干两分数相等
	cp = cloneOf(t, base)
	stemBlk := cp.Content["blocks"].([]any)[0].(map[string]any)
	idx := strings.Index(stem, fractionsInStem[1])
	mutated := stem[:idx] + fractionsInStem[0] + stem[idx+len(fractionsInStem[1]):]
	put2(stemBlk, "rendered", mutated)
	// 同步 answer.value 使其等于该分数，保证命中的是“相等”分支而非失配分支
	dig(t, cp.Content, "answer")["value"] = fractionsInStem[0]
	dig(t, cp.ScoringRef, "scorer_params")["answer"] = fractionsInStem[0]
	expectErr(Validate(cp), "equal-fractions")

	// ⑤ 解析串缺交叉相乘要素
	cp = cloneOf(t, base)
	put2(cp.Content, "explanation", "直接看出来的。选 A。")
	expectErr(Validate(cp), "explanation-thin")
}

func TestValidateUnitConvGroundTruthAndMutants(t *testing.T) {
	truths := []struct{ stem, ans, unit string }{
		{"1 千米 = （  ）米", "1000", "米"},
		{"19.8 米 = （  ）千米", "0.0198", "千米"},
		{"1 千克 = （  ）克", "1000", "克"},
		{"500 克 = （  ）千克", "0.5", "千克"},
		{"1 吨 = （  ）千克", "1000", "千克"},
		{"3.5 元 = （  ）角", "35", "角"},
		{"45 角 = （  ）元", "4.5", "元"},
		{"25 分米 = （  ）厘米", "250", "厘米"},
		{"70.5 毫米 = （  ）厘米", "7.05", "厘米"},
	}
	for _, tc := range truths {
		inst := handBuiltConv(tc.stem, tc.ans, tc.unit)
		if err := validateUnitConv(inst); err != nil {
			t.Errorf("地面真值 [%s=%s%s] 被验证器误杀: %v", tc.stem, tc.ans, tc.unit, err)
		}
	}

	// 反向换算答案（历史 bug：×10 方向反了正是要抓的对象）
	reversed := []struct{ stem, wrongAns, unit string }{
		{"19.8 米 = （  ）千米", "19800", "千米"},
		{"82.3 克 = （  ）千克", "82300", "千克"},
		{"70.5 毫米 = （  ）厘米", "705", "厘米"},
	}
	for _, tc := range reversed {
		inst := handBuiltConv(tc.stem, tc.wrongAns, tc.unit)
		err := validateUnitConv(inst)
		if !errors.Is(err, ErrAnswerMismatch) {
			t.Errorf("反向换算错答 [%s=%s%s] 必须按 answer-mismatch 拒绝，得 %v",
				tc.stem, tc.wrongAns, tc.unit, err)
		}
	}

	base := firstValid(t, idUnitConv, 3)

	// 单位字段篡改
	cp := cloneOf(t, base)
	dig(t, cp.Content, "answer")["unit"] = "歪单位"
	if err := Validate(cp); !errors.Is(err, ErrAnswerMismatch) {
		t.Errorf("目标单位不一致应拒: %v", err)
	}

	// 跨量纲题干（手改渲染文本）
	cp = cloneOf(t, base)
	put2(cp.Content["blocks"].([]any)[0].(map[string]any), "rendered", "3 米 = （  ）克")
	if err := Validate(cp); !errors.Is(err, ErrConsistencyBroken) {
		t.Errorf("跨量纲必须 consistency 拒绝: %v", err)
	}

	// 同单位无意义换算
	cp = cloneOf(t, base)
	put2(cp.Content["blocks"].([]any)[0].(map[string]any), "rendered", "3 米 = （  ）米")
	if err := Validate(cp); !errors.Is(err, ErrConsistencyBroken) {
		t.Errorf("同单位换算必须 consistency 拒绝: %v", err)
	}

	// 缺括号空位（句式不完整）
	cp = cloneOf(t, base)
	put2(cp.Content["blocks"].([]any)[0].(map[string]any), "rendered", "3 米 = __ 米")
	if err := Validate(cp); !errors.Is(err, ErrShapeInvalid) {
		t.Errorf("句式残缺必须 shape 拒绝: %v", err)
	}

	// 超出值域的大数
	cp = cloneOf(t, base)
	put2(cp.Content["blocks"].([]any)[0].(map[string]any), "rendered", "123456789 米 = （  ）厘米")
	if err := Validate(cp); !errors.Is(err, ErrFormatViolation) {
		t.Errorf("超域数值必须 format 拒绝: %v", err)
	}
}

// handBuiltConv 手工拼装单位换算实例（绕开生成器，作为验证器的独立喂入）。
func handBuiltConv(stem, ans, unit string) *Instance {
	return &Instance{
		TemplateID:        idUnitConv,
		TemplateVersionID: "sha256:handmade-for-test",
		Locale:            "zh-CN",
		Objective:         objective("math.nal.quantity.length", "apply", "M"),
		InteractionRef: map[string]any{
			"interaction_id":     "numeric_blank",
			"interaction_params": map[string]any{"blank_ids": []any{"b1"}},
		},
		Content: map[string]any{
			"blocks":      []any{textBlock("{value} {from} = （  ）{to}", stem)},
			"answer":      map[string]any{"blank_id": "b1", "value": ans, "unit": unit},
			"explanation": "因为进率关系，得到 " + ans + "。单位是" + unit + "。",
		},
		ScoringRef: map[string]any{
			"scorer_id":     "exact_match",
			"scorer_params": map[string]any{"answer": ans, "unit": unit, "blank_id": "b1"},
		},
		ErrorBindings: []map[string]any{{
			"subject":         "blank:b1",
			"error_type_id":   "err.conv.unit.mismatch",
			"confidence_rule": "answer-value-neq-implies-error",
		}},
		Lineage: lineage("sha256:handmade-for-test", map[string]any{}),
	}
}
