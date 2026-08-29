package scoring

// exact_match 评分器（scorer.yaml §57；Python 冻结实现
// src/core/scoring/platform_scorers.py ExactMatchScorer 的 Go 移植）。
//
// T-W5-016 遗留项：全角/半角归一表补全——Python 全量映射（数字 10 + 大写 26 +
// 小写 26）之上补齐 FF01–FF5E 全角 ASCII 块的运算符与常用标点、空白归一，
// 共 96 项，表驱动测试逐对断言。
//
// 答案形态（随交互类型，scorer.yaml §57 params_schema.answer）：
//   - 标量：single_choice 的 option_id / 单空答案；
//   - 数组：multi_choice 的正确选项集合（无序）或 ordering 的标准序列（有序，
//     params.ordered=true 判定——Python 侧的 interaction_id=ordering 判据在
//     Go Runner 面不可达，由调用方经 ordered 参数声明）；
//   - 对象：text_blank/numeric_blank 的 {blank_id: 答案}、matching 的
//     {left_id: right_id}、drawing_operation 的 {element_id: state}。
//
// 作答形态判定（Python _judge_mapping 的响应形状回退路径）：响应载荷含
// pairs → matching、elements → drawing_operation、blanks → 逐空。

import (
	"context"
	"encoding/json"
	"fmt"
	"sort"
	"strings"

	"github.com/Cloudbird-Software/AI_Web_School/registry"
)

// versionExactMatch 评分器版本（Python 冻结实现同值——trace 口径跨语言对齐）.
const versionExactMatch = "1.0.0+platform"

// fullwidthToHalf 全角→半角归一表（96 项：FF01–FF5E 全角 ASCII 块 94 项 +
// 全角空格/不换行空格 2 项）.
var fullwidthToHalf = buildFullwidthTable()

// buildFullwidthTable 构造归一表：Python 冻结实现全量映射（0xFF10–19 数字、
// 0xFF21–3A 大写、0xFF41–5A 小写）+ T-W5-016 验收 #3 补全面（块内其余符号 =
// 运算符/常用标点，FF01–FF5E 与 ASCII 0x21–0x7E 恒差 0xFEE0）+ 空白.
func buildFullwidthTable() map[rune]rune {
	t := make(map[rune]rune, 96)
	for i := 0; i < 10; i++ {
		t[rune(0xFF10+i)] = rune('0' + i)
	}
	for i := 0; i < 26; i++ {
		t[rune(0xFF21+i)] = rune('A' + i)
		t[rune(0xFF41+i)] = rune('a' + i)
	}
	for r := rune(0xFF01); r <= rune(0xFF5E); r++ {
		if _, ok := t[r]; !ok {
			t[r] = r - 0xFEE0
		}
	}
	t[0x3000] = ' ' // 全角空格
	t[0x00A0] = ' ' // 不换行空格
	return t
}

// textNormalization 是 normalization 参数的三开关（scorer.yaml exact_match
// params_schema.normalization：strip/casefold/fullwidth_to_half）.
type textNormalization struct {
	Strip           bool
	Casefold        bool
	FullwidthToHalf bool
}

// parseNormalization 解析 normalization 参数；缺省仅 strip 开启
// （Python normalization.get("strip", True) 同源）.
func parseNormalization(params map[string]any) textNormalization {
	n := textNormalization{Strip: true}
	raw, ok := params["normalization"].(map[string]any)
	if !ok {
		return n
	}
	if v, ok := raw["strip"].(bool); ok {
		n.Strip = v
	}
	if v, ok := raw["casefold"].(bool); ok {
		n.Casefold = v
	}
	if v, ok := raw["fullwidth_to_half"].(bool); ok {
		n.FullwidthToHalf = v
	}
	return n
}

// NormalizeText 文本规范化：全角→半角（可选）→ 折叠连续空白为单空格（默认）
// → 大小写折叠（可选），三步次序与 Python 冻结实现一致。exact_match 与
// keypoint_hit 共用同一口径.
//
// Casefold 以 strings.ToLower 近似（标准库无 casefold；ß 等特殊折叠不在本
// 平台文本域——小学语料为中文/英文/数字）.
func NormalizeText(s string, n textNormalization) string {
	if n.FullwidthToHalf {
		s = strings.Map(func(r rune) rune {
			if half, ok := fullwidthToHalf[r]; ok {
				return half
			}
			return r
		}, s)
	}
	if n.Strip {
		s = strings.Join(strings.Fields(s), " ")
	}
	if n.Casefold {
		s = strings.ToLower(s)
	}
	return s
}

// textsEqual 规范化后比较两个标量（统一 scalarString 化，兼容数字答案与
// 字符串作答——Python _texts_equal 同构）.
func textsEqual(a, b any, n textNormalization) bool {
	return NormalizeText(scalarString(a), n) == NormalizeText(scalarString(b), n)
}

// matchJudgement 是单部分判定明细（evidence.judgements；落 trace 供教研抽检，
// 结构体序列化字段序固定——可回放断言逐字节稳定的前提）.
type matchJudgement struct {
	Part     string `json:"part"`
	Expected any    `json:"expected"`
	Actual   any    `json:"actual"`
	OK       bool   `json:"ok"`
}

// ExactMatchScorer 是 exact_match 评分器（确定性；多部分按命中比例给分须
// 显式 partial_credit.per_item，缺省全对才得分——契约原文）.
type ExactMatchScorer struct{}

// NewExactMatchScorer 构造 exact_match 评分器.
func NewExactMatchScorer() *ExactMatchScorer { return &ExactMatchScorer{} }

// Entry 实现 registry.Scorer.
func (s *ExactMatchScorer) Entry() registry.Entry {
	return registry.Entry{ID: "exact_match", Version: versionExactMatch}
}

// ScorerContract 实现 registry.Contracted：answer 形态随交互类型（契约未声明
// type），声明为 KindAny 由本评分器按形态分派；必备键存在性仍由 Runner 强制.
func (s *ExactMatchScorer) ScorerContract() registry.ScorerSpec {
	return registry.ScorerSpec{
		Entry:         s.Entry(),
		InputSchema:   map[string]registry.ParamKind{"answer": registry.KindAny},
		Deterministic: true,
	}
}

// Score 执行精确匹配判定。缺失 answer 是配置错误：显式失败而非静默判错
// （Python 冻结实现返回置信度 0 结果，Go 侧按 fail-loud 纪律收紧为错误——
// runner.ErrInvalidInput 的字面语义：无法判定必须在出分前明确失败）.
func (s *ExactMatchScorer) Score(_ context.Context, answer string, params map[string]any) (registry.ScoreResult, error) {
	expected, ok := params["answer"]
	if !ok || expected == nil {
		return registry.ScoreResult{}, fmt.Errorf("%w: exact_match 缺 answer（禁止静默判错）", ErrInvalidInput)
	}
	norm := parseNormalization(params)
	perItem := false
	if pc, ok := params["partial_credit"].(map[string]any); ok {
		if v, ok := pc["per_item"].(bool); ok {
			perItem = v
		}
	}
	ordered, _ := params["ordered"].(bool)

	resp := decodeAnswer(answer)

	var judgements []matchJudgement
	switch exp := expected.(type) {
	case map[string]any:
		judgements = judgeMapping(exp, resp, norm)
	case []any:
		judgements = judgeSequence(exp, resp, ordered, norm)
	default:
		judgements = judgeScalar(expected, resp, norm)
	}

	total := len(judgements)
	hits := 0
	for _, j := range judgements {
		if j.OK {
			hits++
		}
	}
	// Python 同构：per_item 且有部分 → 命中比例；否则全对才得分.
	var score float64
	if perItem && total > 0 {
		score = float64(hits) / float64(total)
	} else if total > 0 && hits == total {
		score = 1.0
	}

	evidence, err := json.Marshal(map[string]any{
		"judgements": judgements,
		"per_item":   perItem,
	})
	if err != nil {
		return registry.ScoreResult{}, fmt.Errorf("scoring: exact_match 证据序列化失败: %w", err)
	}
	return registry.ScoreResult{
		Correct:      score >= 1.0, // service.py 口径：correct >= 1.0 为对，部分分一律记错
		Score:        score,
		Confidence:   1.0, // 确定性评分器
		EvidenceJSON: string(evidence),
	}, nil
}

// judgeScalar 标量答案：single_choice.selected / 单值作答
// （selected > answer > value 取值优先级与 Python 一致，键存在即取值）.
func judgeScalar(expected, resp any, n textNormalization) []matchJudgement {
	var actual any
	if m, ok := resp.(map[string]any); ok {
		for _, key := range []string{"selected", "answer", "value"} {
			if v, ok := m[key]; ok {
				actual = v
				break
			}
		}
	} else {
		actual = resp
	}
	return []matchJudgement{{
		Part:     "answer",
		Expected: expected,
		Actual:   actual,
		OK:       actual != nil && textsEqual(actual, expected, n),
	}}
}

// judgeSequence 数组答案：multi_choice 集合比对（无序）/ ordering 序列比对
// （有序）。多出元素记 extra 判定（不计入标准位，Python 同构）.
func judgeSequence(expected []any, resp any, ordered bool, n textNormalization) []matchJudgement {
	var actualList []string
	switch act := resp.(type) {
	case []any:
		for _, x := range act {
			actualList = append(actualList, scalarString(x))
		}
	case map[string]any:
		raw, ok := act["selected"]
		if !ok {
			raw = act["sequence"]
		}
		actualList = stringSlice(raw)
	}

	judgements := []matchJudgement{}
	if ordered {
		for i, exp := range expected {
			var act any
			if i < len(actualList) {
				act = actualList[i]
			}
			judgements = append(judgements, matchJudgement{
				Part:     fmt.Sprintf("pos%d", i+1),
				Expected: exp,
				Actual:   act,
				OK:       act != nil && textsEqual(act, exp, n),
			})
		}
		if len(actualList) > len(expected) {
			judgements = append(judgements, matchJudgement{
				Part:   "extra",
				Actual: actualList[len(expected):],
				OK:     false,
			})
		}
		return judgements
	}

	// 无序集合：每个期望元素判定是否被选中；多选的错误项逐条记录
	// （extras 排序——Go map 迭代乱序，evidence 确定性要求）.
	actualSet := make(map[string]bool, len(actualList))
	for _, x := range actualList {
		actualSet[NormalizeText(x, n)] = true
	}
	expectedSet := make(map[string]bool, len(expected))
	for _, exp := range expected {
		key := NormalizeText(scalarString(exp), n)
		expectedSet[key] = true
		judgements = append(judgements, matchJudgement{
			Part:     scalarString(exp),
			Expected: exp,
			Actual:   actualSet[key],
			OK:       actualSet[key],
		})
	}
	extras := []string{}
	for key := range actualSet {
		if !expectedSet[key] {
			extras = append(extras, key)
		}
	}
	sort.Strings(extras)
	for _, x := range extras {
		judgements = append(judgements, matchJudgement{Part: x, Actual: x, OK: false})
	}
	return judgements
}

// judgeMapping 对象答案：pairs（匹配）/ elements（作图状态集）/ blanks（逐空，
// {blank_id: str | {value, unit?}}）。判定明细按 part 排序（Go map 迭代乱序，
// evidence 确定性要求；Python dict 插入序在 JSON 通道不保序）.
func judgeMapping(expected map[string]any, resp any, n textNormalization) []matchJudgement {
	actual := map[string]any{}
	if m, ok := resp.(map[string]any); ok {
		switch {
		case hasArray(m, "pairs"):
			for _, p := range m["pairs"].([]any) {
				pm, ok := p.(map[string]any)
				if !ok {
					continue
				}
				actual[scalarString(pm["left_id"])] = pm["right_id"]
			}
		case hasArray(m, "elements"):
			for _, e := range m["elements"].([]any) {
				em, ok := e.(map[string]any)
				if !ok {
					continue
				}
				actual[scalarString(em["element_id"])] = em["state"]
			}
		case hasObject(m, "blanks"):
			for bid, val := range m["blanks"].(map[string]any) {
				if vm, ok := val.(map[string]any); ok {
					actual[bid] = vm["value"]
				} else {
					actual[bid] = val
				}
			}
		}
	}

	judgements := []matchJudgement{}
	for key, exp := range expected {
		expVal := exp
		if vm, ok := exp.(map[string]any); ok {
			expVal = vm["value"]
		}
		act := actual[key]
		judgements = append(judgements, matchJudgement{
			Part:     key,
			Expected: expVal,
			Actual:   act,
			OK:       act != nil && textsEqual(act, expVal, n),
		})
	}
	sort.Slice(judgements, func(i, j int) bool { return judgements[i].Part < judgements[j].Part })
	return judgements
}

// hasArray/hasObject 报告载荷键是否存在且形态正确（null 与形态不符均视作
// 缺席——Python `is not None` 判据的 Go 投影）.
func hasArray(m map[string]any, key string) bool {
	v, ok := m[key].([]any)
	return ok && v != nil
}

func hasObject(m map[string]any, key string) bool {
	v, ok := m[key].(map[string]any)
	return ok && v != nil
}
