// variation_test.go 受控变式引擎与变体证书的验收测试（T-W2-005）。
//
// 测试策略（对齐冻结基准 src/core/instantiation/variation/*.py 的验收）：
//  1. 跨语言地面真值：compute_objective_signature / issue_certificate 的
//     certificate_id 由冻结 Python 实跑产出并硬编码（禁止手改期望值）；
//  2. 默认采样器：int 回绕 / decimal 定点 / fraction 最简 / choice 轮转，
//     期望值与 Python _default_sampler 实跑逐项核对；
//  3. 确定性：同输入两组变式必得同一批 item_version_id 与同一证书 id（D3）；
//  4. objective 依赖：全槽变式 / choice 槽进表达式 → 拒绝发证（UNPROVEN、
//     空变式）；AI 自由改写永标 UNPROVEN（验收 §3/§4）。
package variation

import (
	"strings"
	"sync"
	"testing"

	"github.com/Cloudbird-Software/AI_Web_School/core/instantiation"
	"github.com/Cloudbird-Software/AI_Web_School/core/instantiation/dsl"
)

// objectiveSignaturePython 冻结 Python 实跑：
// compute_objective_signature({'kp_set': [{'dimension': 'kp',
//
//	'code': 'math.nal.int.add'}], 'kp_set_mode': 'single',
//	'cognitive_level': 'apply', 'gradeband': 'L', 'graph_release': '2026.1'})
const objectiveSignaturePython = "sha256:8c3aa707be5fd01b39aae0b7a6037d8a5aa5f196dfdf6c9b7a3eee566fe59681"

// certificateIDPython 冻结 Python 实跑 issue_certificate：
// operator=controlled-variation-engine, axis=ax-1, certified=True,
// reason=r, objective_signature=sha256:abc,
// kp/skill unchanged=True, axis_slots=[b a], frozen_slots=[c],
// variant_ids=[sha256:v2 sha256:v1]
const certificateIDPython = "sha256:e6fe843087867f9ff8b61c567f096dc48eacfcf89d789d87802bf19a801e1b20"

func TestComputeObjectiveSignatureGolden(t *testing.T) {
	sig, err := ComputeObjectiveSignature(map[string]any{
		"kp_set":          []any{map[string]any{"dimension": "kp", "code": "math.nal.int.add"}},
		"kp_set_mode":     "single",
		"cognitive_level": "apply",
		"gradeband":       "L",
		"graph_release":   "2026.1", // 不参与签名（考什么技能与投放图版本无关）
	})
	if err != nil {
		t.Fatalf("签名计算失败: %v", err)
	}
	if sig != objectiveSignaturePython {
		t.Fatalf("objective 签名与冻结实现不一致:\n got  %s\n want %s", sig, objectiveSignaturePython)
	}
	// kp_set 顺序无关（codes 升序）
	sig2, _ := ComputeObjectiveSignature(map[string]any{
		"kp_set": []any{
			map[string]any{"dimension": "kp", "code": "math.nal.int.add"},
			map[string]any{"dimension": "kp", "code": "math.nal.int.sub"},
		},
		"kp_set_mode":     "all_required",
		"cognitive_level": "apply",
		"gradeband":       "L",
	})
	sig3, _ := ComputeObjectiveSignature(map[string]any{
		"kp_set": []any{
			map[string]any{"dimension": "kp", "code": "math.nal.int.sub"},
			map[string]any{"dimension": "kp", "code": "math.nal.int.add"},
		},
		"kp_set_mode":     "all_required",
		"cognitive_level": "apply",
		"gradeband":       "L",
	})
	if sig2 != sig3 {
		t.Errorf("kp_set 顺序应无关（codes 升序规范化）: %s vs %s", sig2, sig3)
	}
}

func TestIssueCertificateGolden(t *testing.T) {
	cert := IssueCertificate(
		ControlLedVariationOperator,
		"ax-1",
		true,
		"r（reason 不进 id）",
		"sha256:abc",
		true, true,
		[]string{"b", "a"}, // 乱序传入，evidence 内排序
		[]string{"c"},
		[]string{"sha256:v2", "sha256:v1"},
	)
	if cert.CertificateID != certificateIDPython {
		t.Fatalf("certificate_id 与冻结实现不一致:\n got  %s\n want %s", cert.CertificateID, certificateIDPython)
	}
	// evidence 内 axis_slots/frozen_slots 排序
	if got := cert.InvariantEvidence["axis_slots"].([]any); got[0] != "a" || got[1] != "b" {
		t.Errorf("axis_slots 应排序: %v", got)
	}
	// id 只依赖确定性字段：reason 变化 → id 不变
	cert2 := IssueCertificate(ControlLedVariationOperator, "ax-1", true,
		"完全不同的措辞", "sha256:abc", true, true,
		[]string{"a", "b"}, []string{"c"},
		[]string{"sha256:v2", "sha256:v1"})
	if cert2.CertificateID != cert.CertificateID {
		t.Errorf("reason 不应影响证书 id")
	}
	// 变式 id 顺序敏感（生成顺序即谱系）
	cert3 := IssueCertificate(ControlLedVariationOperator, "ax-1", true,
		"r", "sha256:abc", true, true,
		[]string{"a", "b"}, []string{"c"},
		[]string{"sha256:v1", "sha256:v2"})
	if cert3.CertificateID == cert.CertificateID {
		t.Errorf("vids 顺序变化必须改变 id（谱系顺序语义）")
	}
	if cert.IsUnproven() {
		t.Errorf("certified 证书不应是 UNPROVEN")
	}
}

func TestMarkUnproven(t *testing.T) {
	cert := MarkUnproven("ai-model-x", "拒绝原因", "sha256:sig", "ax-1",
		[]string{"a"}, []string{"b"}, []string{"sha256:v1"})
	if cert.Certified {
		t.Fatalf("MarkUnproven 必须 certified=false")
	}
	if cert.InvariantEvidence["kp_set_unchanged"] != false ||
		cert.InvariantEvidence["skill_set_unchanged"] != false {
		t.Errorf("UNPROVEN 的不变性证据应为 false（无法证明）")
	}
}

func TestDefaultSamplerPythonGroundTruths(t *testing.T) {
	slot := func(typ string, min, max any, choices []any) (s dsl.Slot) {
		s = dsl.Slot{Type: typ}
		s.Min, s.Max, s.Choices = min, max, choices
		return s
	}
	cases := []struct {
		name  string
		slot  dsl.Slot
		base  any
		index int
		want  any
	}{
		{"int 递增", slot("int", nil, nil, nil), 10, 0, int64(11)},
		{"int 递增 i1", slot("int", nil, nil, nil), 10, 1, int64(12)},
		{"int 区间回绕", slot("int", int64(10), int64(13), nil), 10, 4, int64(12)},
		{"decimal 定点", slot("decimal", nil, nil, nil), "3.10", 0, "4.1"},
		{"decimal 定点 i1", slot("decimal", nil, nil, nil), "3.10", 1, "5.1"},
		{"fraction 最简", slot("fraction", nil, nil, nil), "3/4", 0, "7/4"},
		{"fraction 小数基准", slot("fraction", nil, nil, nil), "0.75", 1, "11/4"},
		{"choice 轮转", slot("choice", nil, nil, []any{"a", "b", "c"}), "a", 0, "b"},
		{"choice 未知基准", slot("choice", nil, nil, []any{"a", "b", "c"}), "x", 0, "b"},
		{"string 不变", slot("string", nil, nil, nil), "foo", 3, "foo"},
		{"bool 不变", slot("bool", nil, nil, nil), true, 3, true},
	}
	for _, tc := range cases {
		got, err := DefaultSampler("x", tc.slot, tc.base, tc.index)
		if err != nil {
			t.Errorf("case %q: 采样失败 %v", tc.name, err)
			continue
		}
		if !valuesEqualAny(got, tc.want) {
			t.Errorf("case %q: got %v want %v", tc.name, got, tc.want)
		}
	}
}

// variantTemplate 含数值轴（a/b int）与一个 choice 槽（op）的母题。
func variantTemplate() map[string]any {
	return map[string]any{
		"template_version_id": "sha256:fixture-template-variation",
		"template_id":         "tpl-variation",
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
				"a":  map[string]any{"type": "int", "difficulty_relevant": true, "min": 1, "max": 20},
				"b":  map[string]any{"type": "int", "difficulty_relevant": true, "min": 1, "max": 20},
				"op": map[string]any{"type": "choice", "difficulty_relevant": false, "choices": []any{"+", "*"}},
			},
			"variation_axes": map[string]any{
				"axes": []any{
					map[string]any{"axis_id": "ax-num", "slots": []any{"a"}},
					map[string]any{"axis_id": "ax-both", "slots": []any{"a", "b", "op"}},
				},
			},
			"presentation": map[string]any{
				"blocks": []any{map[string]any{"kind": "text", "template": "{a} + {b} = ?"}},
			},
			"answer_program": map[string]any{"expression": "a + b", "returns": "number"},
			"distractor_rules": map[string]any{
				"rules": []any{},
			},
		},
	}
}

func variantOpts() GenerateOptions {
	return GenerateOptions{
		PackDigest:    "sha256:pack-math-fixture",
		InteractionID: "numeric_blank",
		ScorerID:      "exact_match",
		Locale:        "zh-CN",
		Seed:          0,
	}
}

func TestGenerateVariantsDeterministic(t *testing.T) {
	base := map[string]any{"a": 3, "b": 4, "op": "+"}
	variants1, cert1, err := GenerateVariants(variantTemplate(), "ax-num", 5, base, variantOpts())
	if err != nil {
		t.Fatalf("变式生成失败: %v", err)
	}
	if len(variants1) != 5 {
		t.Fatalf("应生成 5 个变式，实际 %d", len(variants1))
	}
	if !cert1.Certified {
		t.Fatalf("受控变式应发证")
	}
	if cert1.AxisID != "ax-num" {
		t.Errorf("cert axis = %q", cert1.AxisID)
	}
	if len(cert1.VariantIDs) != 5 {
		t.Errorf("证书应记录 5 个变式 id")
	}
	// D3：同输入第二遍必得同 id、同证书
	variants2, cert2, err := GenerateVariants(variantTemplate(), "ax-num", 5, base, variantOpts())
	if err != nil {
		t.Fatalf("第二次变式生成失败: %v", err)
	}
	for i := range variants1 {
		if variants1[i].ItemVersionID != variants2[i].ItemVersionID {
			t.Fatalf("变式 [%d] id 漂移（D3 破坏）", i)
		}
	}
	if cert1.CertificateID != cert2.CertificateID {
		t.Fatalf("证书 id 漂移（D3 破坏）")
	}
	// 冻结槽不变形：b/op 全部保持基准值
	for _, v := range variants1 {
		norm := v.Lineage["params"].(map[string]any)["normalized"].(map[string]any)
		if norm["b"] != int64(4) {
			t.Errorf("冻结槽 b 被重采样: %v", norm["b"])
		}
		if norm["op"] != "+" {
			t.Errorf("冻结槽 op 被重采样: %v", norm["op"])
		}
	}
	// 轴槽按采样器推进：a = 4..8（base 3 + i + 1）
	for i, v := range variants1 {
		norm := v.Lineage["params"].(map[string]any)["normalized"].(map[string]any)
		if norm["a"] != int64(4+i) {
			t.Errorf("变式 [%d] a = %v, 期望 %d", i, norm["a"], 4+i)
		}
	}
}

func TestGenerateVariantsConcurrentDeterministic(t *testing.T) {
	base := map[string]any{"a": 3, "b": 4, "op": "+"}
	var wg sync.WaitGroup
	const runs = 8
	certs := make([]string, runs)
	for i := range runs {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			_, cert, err := GenerateVariants(variantTemplate(), "ax-num", 3, base, variantOpts())
			if err != nil {
				t.Errorf("并发变式 [%d] 失败: %v", i, err)
				return
			}
			certs[i] = cert.CertificateID
		}(i)
	}
	wg.Wait()
	for i := range certs {
		if certs[i] != certs[0] {
			t.Fatalf("并发证书 id 漂移 [%d]: %s vs %s", i, certs[i], certs[0])
		}
	}
}

func TestGenerateVariantsObjectiveDependency(t *testing.T) {
	base := map[string]any{"a": 3, "b": 4, "op": "+"}
	// 规则 1：全槽变式（ax-both 覆盖 a/b/op 全部槽）→ 拒绝发证
	variants, cert, err := GenerateVariants(variantTemplate(), "ax-both", 2, base, variantOpts())
	if err != nil {
		t.Fatalf("objective 依赖应走 UNPROVEN 而非报错: %v", err)
	}
	if len(variants) != 0 {
		t.Fatalf("objective 依赖时变式列表应为空，实际 %d", len(variants))
	}
	if cert.IsUnproven() {
		// 预期路径
	} else {
		t.Fatalf("objective 依赖必须 UNPROVEN")
	}
	if !strings.Contains(cert.Reason, "objective 依赖槽") {
		t.Errorf("拒绝原因应说明 objective 依赖: %s", cert.Reason)
	}
}

func TestMarkAIFreeRewrite(t *testing.T) {
	iv, err := instantiation.Instantiate(variantTemplate(), map[string]any{"a": 3, "b": 4, "op": "+"},
		instantiation.InstantiateOptions{
			PackDigest:    "sha256:pack-math-fixture",
			InteractionID: "numeric_blank",
			ScorerID:      "exact_match",
			Locale:        "zh-CN",
		})
	if err != nil {
		t.Fatalf("实例化失败: %v", err)
	}
	cert := MarkAIFreeRewrite(iv, "ai-model-x", "", "")
	if cert.IsUnproven() != true {
		t.Fatalf("AI 自由改写必须永标 UNPROVEN（验收 §4）")
	}
	if cert.OperatorID != "ai-model-x" {
		t.Errorf("operator 应为 AI 改写者: %q", cert.OperatorID)
	}
	if len(cert.VariantIDs) != 1 || cert.VariantIDs[0] != iv.ItemVersionID {
		t.Errorf("证书应关联改写产物 id: %v", cert.VariantIDs)
	}
}

func TestGenerateVariantsFailClosed(t *testing.T) {
	base := map[string]any{"a": 3, "b": 4, "op": "+"}
	// n <= 0
	if _, _, err := GenerateVariants(variantTemplate(), "ax-num", 0, base, variantOpts()); err == nil {
		t.Errorf("n=0 应拒绝")
	}
	// 轴不存在
	if _, _, err := GenerateVariants(variantTemplate(), "ax-missing", 1, base, variantOpts()); err == nil {
		t.Errorf("未知轴应拒绝")
	}
	// 基准参数缺槽
	if _, _, err := GenerateVariants(variantTemplate(), "ax-num", 1, map[string]any{"a": 3}, variantOpts()); err == nil {
		t.Errorf("基准参数缺槽应拒绝")
	}
}
