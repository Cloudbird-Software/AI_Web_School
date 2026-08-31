package render

// dialect_kind_test.go 钉死内容方言归一层（kind 方言兼容）的行为：
// A 线实例化引擎产出 {kind, template, rendered} 形态 content block
// （黄金数据集 expected_content_snapshot 同构），渲染边界必须接受——
// E2E 实证（2026-08-31）：papergen 对 mathgen 产物第一题即
// "block 缺 type 字段" 崩溃，本兼容层是该断链的修复面。
import "testing"

func TestKindDialectTextBlock(t *testing.T) {
	// mathgen/unit_convert 真实产物形态（含 stem 前缀插值后文本）
	iv := map[string]any{
		"item_version_id": "sha256:kind-1",
		"interaction_ref": map[string]any{"interaction_id": "numeric_blank"},
		"content": map[string]any{
			"blocks": []any{
				map[string]any{
					"kind":     "text",
					"template": "在括号里填上合适的数：{value} {from} = （  ）{to}",
					"rendered": "在括号里填上合适的数：912 厘米 = （  ）米",
				},
			},
		},
	}
	ir, err := ItemToIR(iv, ItemToIRInput{ItemNumber: "1"})
	if err != nil {
		t.Fatalf("kind 方言 block 应被接受: %v", err)
	}
	if len(ir.Blocks) != 1 {
		t.Fatalf("blocks 数 = %d, want 1", len(ir.Blocks))
	}
	tb, ok := ir.Blocks[0].(TextBlock)
	if !ok {
		t.Fatalf("block 类型 = %T, want TextBlock", ir.Blocks[0])
	}
	if tb.Value != "在括号里填上合适的数：912 厘米 = （  ）米" {
		t.Fatalf("value 应取 rendered（插值后文本），得到 %q", tb.Value)
	}
}

func TestKindDialectFallsBackToTemplate(t *testing.T) {
	iv := map[string]any{
		"interaction_ref": map[string]any{"interaction_id": "text_blank"},
		"content": map[string]any{
			"blocks": []any{
				map[string]any{"kind": "text", "template": "{m} 米 = （  ） 厘米"},
			},
		},
	}
	ir, err := ItemToIR(iv, ItemToIRInput{})
	if err != nil {
		t.Fatalf("缺 rendered 时应退 template: %v", err)
	}
	tb := ir.Blocks[0].(TextBlock)
	if tb.Value != "{m} 米 = （  ） 厘米" {
		t.Fatalf("fallback template 值不对: %q", tb.Value)
	}
}

func TestKindDialectOptionTextBlocks(t *testing.T) {
	// subjectmath 单选题产物：stem + A/B/C/D 选项全部为 kind:text 块
	iv := map[string]any{
		"interaction_ref": map[string]any{"interaction_id": "single_choice"},
		"content": map[string]any{
			"blocks": []any{
				map[string]any{"kind": "text", "template": "下面哪个算式积最大？", "rendered": "下面哪个算式积最大？"},
				map[string]any{"kind": "text", "template": "A. {A}", "rendered": "A. 168"},
				map[string]any{"kind": "text", "template": "B. {B}", "rendered": "B. 336"},
			},
		},
	}
	ir, err := ItemToIR(iv, ItemToIRInput{})
	if err != nil {
		t.Fatalf("选项文本块序列应被接受: %v", err)
	}
	if len(ir.Blocks) != 3 {
		t.Fatalf("blocks 数 = %d, want 3（stem + 2 选项）", len(ir.Blocks))
	}
}

func TestKindDialectStillRejectsGarbage(t *testing.T) {
	// 兼容层不放宽失败面：kind 既非已知 type 也无可用文本来源仍拒绝
	iv := map[string]any{
		"interaction_ref": map[string]any{"interaction_id": "text_blank"},
		"content": map[string]any{
			"blocks": []any{
				map[string]any{"kind": "video", "src": "x.mp4"},
			},
		},
	}
	if _, err := ItemToIR(iv, ItemToIRInput{}); err == nil {
		t.Fatal("未知 kind 应拒绝（fail-closed 不因兼容层放宽）")
	}
	// 空块（无 type 无 kind）仍拒绝
	iv2 := map[string]any{
		"interaction_ref": map[string]any{"interaction_id": "text_blank"},
		"content": map[string]any{
			"blocks": []any{map[string]any{"template": "只有模板没有 kind"}},
		},
	}
	if _, err := ItemToIR(iv2, ItemToIRInput{}); err == nil {
		t.Fatal("无 type/kind 的块应拒绝")
	}
}

func TestTypeDialectUnchanged(t *testing.T) {
	// 既有 {type, value} 方言行为不回归
	iv := map[string]any{
		"interaction_ref": map[string]any{"interaction_id": "text_blank"},
		"content": map[string]any{
			"blocks": []any{map[string]any{"type": "text", "value": "3 & 5 < 7"}},
		},
	}
	ir, err := ItemToIR(iv, ItemToIRInput{})
	if err != nil {
		t.Fatalf("type 方言回归: %v", err)
	}
	if ir.Blocks[0].(TextBlock).Value != "3 & 5 < 7" {
		t.Fatal("type 方言 value 应原样保留")
	}
}
