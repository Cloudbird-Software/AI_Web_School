package render

import (
	"encoding/json"
	"errors"
	"testing"
)

// 表驱动：ItemVersion → RenderIR 字段映射（Python 冻结实现 item_to_ir.py 的
// 语义基准：mode/kind 由 interaction_id 推导、layout_hints 缺省、fail fast 面）。
func mustJSON(t *testing.T, s string) map[string]any {
	t.Helper()
	var m map[string]any
	if err := json.Unmarshal([]byte(s), &m); err != nil {
		t.Fatalf("fixture 非法 JSON: %v", err)
	}
	return m
}

func TestItemToIRMapping(t *testing.T) {
	tests := []struct {
		name    string
		fixture string
		want    func(t *testing.T, ir *RenderIR)
		wantErr bool
	}{
		{
			name: "单选题_mode由interaction推导_题号与追溯位透传",
			fixture: `{
				"item_version_id": "iv-001", "item_id": "item-001",
				"interaction_ref": {"interaction_id": "single_choice"},
				"content": {"blocks": [
					{"type": "text", "value": "3 & 5 < 7"},
					{"type": "choice", "options": [
						{"id": "A", "label": "1/2"}, {"id": "B", "label": "3/4"}]}
				]}
			}`,
			want: func(t *testing.T, ir *RenderIR) {
				if ir.ItemVersionID != "iv-001" || ir.ItemID != "item-001" {
					t.Fatalf("溯源字段错: %+v", ir)
				}
				if ir.InteractionID != "single_choice" {
					t.Fatalf("interaction_id 错: %q", ir.InteractionID)
				}
				if len(ir.Blocks) != 2 {
					t.Fatalf("块数错: %d", len(ir.Blocks))
				}
				tb, ok := ir.Blocks[0].(TextBlock)
				if !ok || tb.Value != "3 & 5 < 7" {
					t.Fatalf("text 块错: %+v", ir.Blocks[0])
				}
				cb, ok := ir.Blocks[1].(ChoiceBlock)
				if !ok {
					t.Fatalf("choice 块类型错: %T", ir.Blocks[1])
				}
				if cb.Mode != ChoiceModeSingle {
					t.Fatalf("mode 应由 single_choice 推导为 single: %q", cb.Mode)
				}
				if len(cb.Options) != 2 || cb.Options[1].Label != "3/4" {
					t.Fatalf("选项错: %+v", cb.Options)
				}
				// 缺省版式提示：PreferredColumns=1，其余 false
				if ir.LayoutHints != (LayoutHints{PreferredColumns: 1}) {
					t.Fatalf("缺省 hints 错: %+v", ir.LayoutHints)
				}
			},
		},
		{
			name: "数值填空_kind由interaction推导_带单位与宽度",
			fixture: `{
				"item_version_id": "iv-002", "item_id": "item-002",
				"interaction_ref": {"interaction_id": "numeric_blank"},
				"content": {"blocks": [
					{"type": "fill", "blank_id": "b1", "unit": "cm", "width": 8},
					{"type": "fill", "blank_id": "b2"}
				]}
			}`,
			want: func(t *testing.T, ir *RenderIR) {
				f1, ok := ir.Blocks[0].(FillBlock)
				if !ok {
					t.Fatalf("fill 块类型错: %T", ir.Blocks[0])
				}
				if f1.Kind != FillKindNumeric || f1.Unit != "cm" || f1.Width != 8 {
					t.Fatalf("fill[0] 错: %+v", f1)
				}
				f2 := ir.Blocks[1].(FillBlock)
				if f2.Kind != FillKindNumeric || f2.Unit != "" || f2.Width != 0 {
					t.Fatalf("fill[1] 应继承 numeric 推导且缺省 width: %+v", f2)
				}
			},
		},
		{
			name: "填空_kind显式声明优先于interaction推导",
			fixture: `{
				"item_version_id": "iv", "item_id": "i",
				"interaction_ref": {"interaction_id": "numeric_blank"},
				"content": {"blocks": [{"type": "fill", "blank_id": "b", "kind": "text"}]}
			}`,
			want: func(t *testing.T, ir *RenderIR) {
				f := ir.Blocks[0].(FillBlock)
				if f.Kind != FillKindText {
					t.Fatalf("显式 kind 应优先: %q", f.Kind)
				}
			},
		},
		{
			name: "多选题_mode显式multi",
			fixture: `{
				"item_version_id": "iv", "item_id": "i",
				"interaction_ref": {"interaction_id": "multi_choice"},
				"content": {"blocks": [{"type": "choice", "mode": "multi", "options": []}]}
			}`,
			want: func(t *testing.T, ir *RenderIR) {
				cb := ir.Blocks[0].(ChoiceBlock)
				if cb.Mode != ChoiceModeMulti || len(cb.Options) != 0 {
					t.Fatalf("choice 错: %+v", cb)
				}
			},
		},
		{
			name: "未知交互_fill/choice走缺省text/single",
			fixture: `{
				"item_version_id": "iv", "item_id": "i",
				"interaction_ref": {"interaction_id": "drag_order"},
				"content": {"blocks": [
					{"type": "fill", "blank_id": "b"},
					{"type": "choice", "options": [{"id": "A", "label": "甲"}]}
				]}
			}`,
			want: func(t *testing.T, ir *RenderIR) {
				if f := ir.Blocks[0].(FillBlock); f.Kind != FillKindText {
					t.Fatalf("未知交互 fill 应缺省 text: %+v", f)
				}
				if c := ir.Blocks[1].(ChoiceBlock); c.Mode != ChoiceModeSingle {
					t.Fatalf("未知交互 choice 应缺省 single: %+v", c)
				}
			},
		},
		{
			name: "题组_ItemVersion子题与IR子题两态递归",
			fixture: `{
				"item_version_id": "iv-g", "item_id": "item-g",
				"interaction_ref": {"interaction_id": "multi_choice"},
				"content": {"blocks": [
					{"type": "group", "material": "共享素材", "items": [
						{"item_version_id": "iv-s1", "item_id": "item-s1",
						 "content": {"blocks": [{"type": "text", "value": "子题1"}]},
						 "interaction_ref": {"interaction_id": "single_choice"}},
						{"item_version_id": "iv-s2", "item_id": "item-s2",
						 "interaction_id": "single_choice",
						 "blocks": [{"type": "fill", "blank_id": "sb1", "kind": "text"}]}
					]}
				]}
			}`,
			want: func(t *testing.T, ir *RenderIR) {
				gb, ok := ir.Blocks[0].(GroupBlock)
				if !ok || gb.Material != "共享素材" || len(gb.Items) != 2 {
					t.Fatalf("group 块错: %+v", ir.Blocks[0])
				}
				s1 := gb.Items[0]
				if s1.InteractionID != "single_choice" || s1.ItemVersionID != "iv-s1" {
					t.Fatalf("ItemVersion 形态子题错: %+v", s1)
				}
				s2 := gb.Items[1]
				if s2.InteractionID != "single_choice" || s2.ItemVersionID != "iv-s2" {
					t.Fatalf("IR 形态子题错: %+v", s2)
				}
				if f, ok := s2.Blocks[0].(FillBlock); !ok || f.Kind != FillKindText {
					t.Fatalf("IR 子题 fill 错: %+v", s2.Blocks[0])
				}
			},
		},
		{
			name: "版式提示_显式三字段",
			fixture: `{
				"item_version_id": "iv", "item_id": "i",
				"interaction_ref": {"interaction_id": "text_blank"},
				"content": {"blocks": [], "layout_hints":
					{"page_break_before": true, "keep_with_next": true, "preferred_columns": 2}}
			}`,
			want: func(t *testing.T, ir *RenderIR) {
				if ir.LayoutHints.PageBreakBefore != true || ir.LayoutHints.KeepWithNext != true ||
					ir.LayoutHints.PreferredColumns != 2 {
					t.Fatalf("hints 错: %+v", ir.LayoutHints)
				}
				if len(ir.Blocks) != 0 {
					t.Fatalf("blocks 应为空: %d", len(ir.Blocks))
				}
			},
		},
		{
			name: "layout_hints非dict_按缺省处理",
			fixture: `{
				"item_version_id": "iv", "item_id": "i",
				"interaction_ref": {"interaction_id": "text_blank"},
				"content": {"blocks": [], "layout_hints": "bad"}
			}`,
			want: func(t *testing.T, ir *RenderIR) {
				if ir.LayoutHints != (LayoutHints{PreferredColumns: 1}) {
					t.Fatalf("非 dict hints 应缺省: %+v", ir.LayoutHints)
				}
			},
		},
		{
			name:    "缺interaction_id_报错",
			fixture: `{"item_version_id": "iv", "item_id": "i", "content": {"blocks": []}}`,
			wantErr: true,
		},
		{
			name: "顶层interaction_id兜底",
			fixture: `{
				"item_version_id": "iv", "item_id": "i",
				"interaction_id": "single_choice", "content": {"blocks": []}
			}`,
			want: func(t *testing.T, ir *RenderIR) {
				if ir.InteractionID != "single_choice" {
					t.Fatalf("顶层 interaction_id 应兜底: %q", ir.InteractionID)
				}
			},
		},
		{
			name:    "未知block_type_报错",
			fixture: `{"item_version_id": "iv", "item_id": "i", "interaction_ref": {"interaction_id": "single_choice"}, "content": {"blocks": [{"type": "video"}]}}`,
			wantErr: true,
		},
		{
			name:    "block非dict_报错",
			fixture: `{"item_version_id": "iv", "item_id": "i", "interaction_ref": {"interaction_id": "single_choice"}, "content": {"blocks": ["text"]}}`,
			wantErr: true,
		},
		{
			name:    "text缺value_报错",
			fixture: `{"item_version_id": "iv", "item_id": "i", "interaction_ref": {"interaction_id": "text_blank"}, "content": {"blocks": [{"type": "text"}]}}`,
			wantErr: true,
		},
		{
			name:    "fill缺blank_id_报错",
			fixture: `{"item_version_id": "iv", "item_id": "i", "interaction_ref": {"interaction_id": "text_blank"}, "content": {"blocks": [{"type": "fill"}]}}`,
			wantErr: true,
		},
		{
			name:    "fill_kind非字符串_报错（pydantic Literal 同判据）",
			fixture: `{"item_version_id": "iv", "item_id": "i", "interaction_ref": {"interaction_id": "text_blank"}, "content": {"blocks": [{"type": "fill", "blank_id": "b", "kind": 5}]}}`,
			wantErr: true,
		},
		{
			name:    "fill_kind越值域_报错",
			fixture: `{"item_version_id": "iv", "item_id": "i", "interaction_ref": {"interaction_id": "text_blank"}, "content": {"blocks": [{"type": "fill", "blank_id": "b", "kind": "phone"}]}}`,
			wantErr: true,
		},
		{
			name:    "choice_mode越值域_报错",
			fixture: `{"item_version_id": "iv", "item_id": "i", "interaction_ref": {"interaction_id": "single_choice"}, "content": {"blocks": [{"type": "choice", "mode": "both", "options": []}]}}`,
			wantErr: true,
		},
		{
			name:    "choice_option缺label_报错",
			fixture: `{"item_version_id": "iv", "item_id": "i", "interaction_ref": {"interaction_id": "single_choice"}, "content": {"blocks": [{"type": "choice", "options": [{"id": "A"}]}]}}`,
			wantErr: true,
		},
		{
			name:    "width小数截断拒绝_报错",
			fixture: `{"item_version_id": "iv", "item_id": "i", "interaction_ref": {"interaction_id": "text_blank"}, "content": {"blocks": [{"type": "fill", "blank_id": "b", "width": 8.5}]}}`,
			wantErr: true,
		},
		{
			name:    "layout_hints布尔字符串拒绝_报错",
			fixture: `{"item_version_id": "iv", "item_id": "i", "interaction_ref": {"interaction_id": "text_blank"}, "content": {"blocks": [], "layout_hints": {"page_break_before": "true"}}}`,
			wantErr: true,
		},
		{
			name:    "题组子题两态皆非_报错（fail fast 不静默丢题）",
			fixture: `{"item_version_id": "iv", "item_id": "i", "interaction_ref": {"interaction_id": "single_choice"}, "content": {"blocks": [{"type": "group", "items": [{"foo": 1}]}]}}`,
			wantErr: true,
		},
		{
			name:    "题组子题IR形态缺kind_报错（pydantic 实证：Field required）",
			fixture: `{"item_version_id": "iv", "item_id": "i", "interaction_ref": {"interaction_id": "single_choice"}, "content": {"blocks": [{"type": "group", "items": [{"item_version_id": "s", "item_id": "s", "interaction_id": "single_choice", "blocks": [{"type": "fill", "blank_id": "b"}]}]}]}}`,
			wantErr: true,
		},
		{
			name:    "题组子题IR形态未知键_报错（extra=forbid）",
			fixture: `{"item_version_id": "iv", "item_id": "i", "interaction_ref": {"interaction_id": "single_choice"}, "content": {"blocks": [{"type": "group", "items": [{"item_version_id": "s", "item_id": "s", "interaction_id": "single_choice", "bogus": 1, "blocks": []}]}]}}`,
			wantErr: true,
		},
		{
			name:    "题组子题IR形态缺身份字段_报错",
			fixture: `{"item_version_id": "iv", "item_id": "i", "interaction_ref": {"interaction_id": "single_choice"}, "content": {"blocks": [{"type": "group", "items": [{"interaction_id": "single_choice", "blocks": []}]}]}}`,
			wantErr: true,
		},
		{
			name:    "group_material非字符串_报错（pydantic Optional[str] 同判据）",
			fixture: `{"item_version_id": "iv", "item_id": "i", "interaction_ref": {"interaction_id": "single_choice"}, "content": {"blocks": [{"type": "group", "material": 7, "items": []}]}}`,
			wantErr: true,
		},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			iv := mustJSON(t, tt.fixture)
			ir, err := ItemToIR(iv, ItemToIRInput{ItemNumber: "5", PlacementToken: "q1", ItemShortCode: "BB4M9T5"})
			if tt.wantErr {
				if err == nil {
					t.Fatalf("期望报错，得到 %+v", ir)
				}
				if !errors.Is(err, ErrInvalidItemVersion) {
					t.Fatalf("错误未锚定哨兵: %v", err)
				}
				return
			}
			if err != nil {
				t.Fatalf("意外报错: %v", err)
			}
			// 透传字段（组卷上下文）逐项落位
			if ir.ItemNumber != "5" || ir.PlacementToken != "q1" || ir.ItemShortCode != "BB4M9T5" {
				t.Fatalf("透传字段错: %+v", ir)
			}
			tt.want(t, ir)
		})
	}
}

// IR 不可变纪律：ItemToIR 输入 dict 不会被修改（转换只读）。
func TestItemToIRDoesNotMutateInput(t *testing.T) {
	iv := map[string]any{
		"item_version_id": "iv", "item_id": "i",
		"interaction_ref": map[string]any{"interaction_id": "text_blank"},
		"content": map[string]any{
			"blocks": []any{map[string]any{"type": "text", "value": "原值"}},
		},
	}
	if _, err := ItemToIR(iv, ItemToIRInput{}); err != nil {
		t.Fatalf("意外报错: %v", err)
	}
	blocks := iv["content"].(map[string]any)["blocks"].([]any)
	if blocks[0].(map[string]any)["value"] != "原值" {
		t.Fatal("入参 dict 被修改（不可变纪律破坏）")
	}
}
