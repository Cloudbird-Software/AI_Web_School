package render

import (
	"errors"
	"strings"
	"testing"
)

// 快照断言：期望字符串逐字符取自冻结 Python 实现（src/core/render/
// html_renderer.py）对同一 fixture 的真实运行输出——跨实现逐字符对齐
// （题干/选项/答题区/追溯行结构一致），不依赖 Go 侧自证。
func mustIR(t *testing.T, fixture string, in ItemToIRInput) *RenderIR {
	t.Helper()
	ir, err := ItemToIR(mustJSON(t, fixture), in)
	if err != nil {
		t.Fatalf("ItemToIR: %v", err)
	}
	return ir
}

func TestRenderItemSnapshots(t *testing.T) {
	tests := []struct {
		name string
		ir   *RenderIR
		want string
	}{
		{
			name: "单选题_题号_选项_追溯行",
			ir: mustIR(t, `{
				"item_version_id": "iv-001", "item_id": "item-001",
				"interaction_ref": {"interaction_id": "single_choice"},
				"content": {"blocks": [
					{"type": "text", "value": "3 & 5 < 7, \"引号\"与'单引号'"},
					{"type": "choice", "options": [
						{"id": "A", "label": "1/2"}, {"id": "B", "label": "3/4 <1"}]}
				]}
			}`, ItemToIRInput{ItemNumber: "3", PlacementToken: "q2.sub1", ItemShortCode: "BB4M9T5"}),
			want: `<div class="item" data-item-version-id="iv-001" data-item-id="item-001" data-interaction-id="single_choice" data-page-break-before="false" data-keep-with-next="false" data-preferred-columns="1"><div class="item-number">3.</div><div class="item-body"><p class="item-text">3 &amp; 5 &lt; 7, &quot;引号&quot;与&#x27;单引号&#x27;</p><ul class="options single"><li><span class="option-label">A</span><span class="option-text">1/2</span></li><li><span class="option-label">B</span><span class="option-text">3/4 &lt;1</span></li></ul></div><div class="item-trace"><span class="placement-token">q2.sub1</span><span class="item-short-code">BB4M9T5</span></div></div>`,
		},
		{
			name: "数值填空_单位_宽度_无题号无追溯行",
			ir: mustIR(t, `{
				"item_version_id": "iv-002", "item_id": "item-002",
				"interaction_ref": {"interaction_id": "numeric_blank"},
				"content": {"blocks": [
					{"type": "text", "value": "求 $x$ 的值："},
					{"type": "fill", "blank_id": "b1", "unit": "cm", "width": 8},
					{"type": "fill", "blank_id": "b2"}
				]}
			}`, ItemToIRInput{}),
			want: `<div class="item" data-item-version-id="iv-002" data-item-id="item-002" data-interaction-id="numeric_blank" data-page-break-before="false" data-keep-with-next="false" data-preferred-columns="1"><div class="item-body"><p class="item-text">求 $x$ 的值：</p><span class="blank" data-blank-id="b1" data-kind="numeric" data-unit="cm" style="--blank-width:8ch"></span><span class="blank" data-blank-id="b2" data-kind="numeric"></span></div></div>`,
		},
		{
			name: "数学SVG原样嵌入_图注转义",
			ir: mustIR(t, `{
				"item_version_id": "iv-003", "item_id": "item-003",
				"interaction_ref": {"interaction_id": "single_choice"},
				"content": {"blocks": [
					{"type": "math_svg", "svg": "<svg viewBox='0 0 10 10'></svg>", "caption": "图 <1> 示意"}
				]}
			}`, ItemToIRInput{}),
			want: `<div class="item" data-item-version-id="iv-003" data-item-id="item-003" data-interaction-id="single_choice" data-page-break-before="false" data-keep-with-next="false" data-preferred-columns="1"><div class="item-body"><figure class="math-svg"><svg viewBox='0 0 10 10'></svg><figcaption>图 &lt;1&gt; 示意</figcaption></figure></div></div>`,
		},
		{
			name: "题组_共享素材_两种子题形态嵌套",
			ir: mustIR(t, `{
				"item_version_id": "iv-g", "item_id": "item-g",
				"interaction_ref": {"interaction_id": "multi_choice"},
				"content": {"blocks": [
					{"type": "text", "value": "组题题干"},
					{"type": "group", "material": "共享素材 <素材>", "items": [
						{"item_version_id": "iv-s1", "item_id": "item-s1",
						 "content": {"blocks": [{"type": "text", "value": "子题1"}]},
						 "interaction_ref": {"interaction_id": "single_choice"}},
						{"item_version_id": "iv-s2", "item_id": "item-s2",
						 "interaction_id": "single_choice",
						 "blocks": [{"type": "fill", "blank_id": "sb1", "kind": "text"}]}
					]}
				]}
			}`, ItemToIRInput{}),
			want: `<div class="item" data-item-version-id="iv-g" data-item-id="item-g" data-interaction-id="multi_choice" data-page-break-before="false" data-keep-with-next="false" data-preferred-columns="1"><div class="item-body"><p class="item-text">组题题干</p><div class="group"><div class="group-material">共享素材 &lt;素材&gt;</div><div class="group-items"><div class="item" data-item-version-id="iv-s1" data-item-id="item-s1" data-interaction-id="single_choice" data-page-break-before="false" data-keep-with-next="false" data-preferred-columns="1"><div class="item-body"><p class="item-text">子题1</p></div></div><div class="item" data-item-version-id="iv-s2" data-item-id="item-s2" data-interaction-id="single_choice" data-page-break-before="false" data-keep-with-next="false" data-preferred-columns="1"><div class="item-body"><span class="blank" data-blank-id="sb1" data-kind="text"></span></div></div></div></div></div></div>`,
		},
		{
			name: "版式提示data属性透传",
			ir: mustIR(t, `{
				"item_version_id": "iv-h", "item_id": "item-h",
				"interaction_ref": {"interaction_id": "text_blank"},
				"content": {"blocks": [{"type": "text", "value": "版式"}],
					"layout_hints": {"page_break_before": true, "keep_with_next": true, "preferred_columns": 2}}
			}`, ItemToIRInput{ItemNumber: "12"}),
			want: `<div class="item" data-item-version-id="iv-h" data-item-id="item-h" data-interaction-id="text_blank" data-page-break-before="true" data-keep-with-next="true" data-preferred-columns="2"><div class="item-number">12.</div><div class="item-body"><p class="item-text">版式</p></div></div>`,
		},
		{
			name: "多选题class为multi_空选项输出空ul",
			ir: mustIR(t, `{
				"item_version_id": "iv-m", "item_id": "item-m",
				"interaction_ref": {"interaction_id": "multi_choice"},
				"content": {"blocks": [{"type": "choice", "options": []}]}
			}`, ItemToIRInput{}),
			want: `<div class="item" data-item-version-id="iv-m" data-item-id="item-m" data-interaction-id="multi_choice" data-page-break-before="false" data-keep-with-next="false" data-preferred-columns="1"><div class="item-body"><ul class="options multi"></ul></div></div>`,
		},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got, err := RenderItem(tt.ir)
			if err != nil {
				t.Fatalf("RenderItem: %v", err)
			}
			if got != tt.want {
				t.Fatalf("快照不匹配（须与冻结 Python 实现逐字符对齐）:\n got: %s\nwant: %s", got, tt.want)
			}
		})
	}
}

// 追溯行分段语义：两者都缺省不输出；只提供其一时只渲染提供的部分.
func TestRenderTracePartial(t *testing.T) {
	base := `{"item_version_id": "iv", "item_id": "i",
		"interaction_ref": {"interaction_id": "single_choice"},
		"content": {"blocks": []}}`
	cases := []struct {
		name string
		in   ItemToIRInput
		want string
	}{
		{name: "仅位置标识", in: ItemToIRInput{PlacementToken: "q1"},
			want: `<div class="item-trace"><span class="placement-token">q1</span></div>`},
		{name: "仅短码", in: ItemToIRInput{ItemShortCode: "BB4M9T5"},
			want: `<div class="item-trace"><span class="item-short-code">BB4M9T5</span></div>`},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got, err := RenderItem(mustIR(t, base, tc.in))
			if err != nil {
				t.Fatalf("RenderItem: %v", err)
			}
			if !strings.Contains(got, tc.want) {
				t.Fatalf("追溯行缺失:\n got: %s\nwant contains: %s", got, tc.want)
			}
		})
	}
}

// RenderItems 多题拼接（无分隔符，与冻结实现 join 一致）.
func TestRenderItems(t *testing.T) {
	fixture := `{"item_version_id": "iv", "item_id": "i",
		"interaction_ref": {"interaction_id": "text_blank"},
		"content": {"blocks": [{"type": "text", "value": "甲"}]}}`
	irs := []RenderIR{*mustIR(t, fixture, ItemToIRInput{}), *mustIR(t, fixture, ItemToIRInput{ItemNumber: "2"})}
	got, err := RenderItems(irs)
	if err != nil {
		t.Fatalf("RenderItems: %v", err)
	}
	one, _ := RenderItem(&irs[0])
	two, _ := RenderItem(&irs[1])
	if got != one+two {
		t.Fatal("RenderItems 应为逐题无分隔拼接")
	}
}

// XSS 防线（验收 #4）：用户内容转义；SVG 白名单拒绝危险构造且不静默降级.
func TestRenderSecurity(t *testing.T) {
	textIR := &RenderIR{
		ItemVersionID: "iv", ItemID: "i", InteractionID: "text_blank",
		Blocks: []Block{TextBlock{Value: `<script>alert(1)</script>&'"`}},
	}
	got, err := RenderItem(textIR)
	if err != nil {
		t.Fatalf("RenderItem: %v", err)
	}
	if strings.Contains(got, "<script") {
		t.Fatalf("输出含未转义 script: %s", got)
	}
	if !strings.Contains(got, "&lt;script&gt;alert(1)&lt;/script&gt;&amp;&#x27;&quot;") {
		t.Fatalf("转义口径与 Python html.escape 不一致: %s", got)
	}

	dangerous := []struct {
		name string
		svg  string
	}{
		{"script标签", `<svg><script>alert(1)</script></svg>`},
		{"script变体空白", `<svg>< SCRIPT >x</SCRIPT></svg>`},
		{"on事件属性", `<svg onload="alert(1)"></svg>`},
		{"javascript链接", `<svg><a href="javascript:alert(1)">x</a></svg>`},
	}
	for _, d := range dangerous {
		t.Run("SVG拒绝_"+d.name, func(t *testing.T) {
			svgIR := &RenderIR{
				ItemVersionID: "iv", ItemID: "i", InteractionID: "single_choice",
				Blocks: []Block{MathSVGBlock{SVG: d.svg}},
			}
			if _, err := RenderItem(svgIR); !errors.Is(err, ErrUnsafeSVG) {
				t.Fatalf("期望 ErrUnsafeSVG，得到 %v", err)
			}
		})
	}

	// 属性位转义：data-item-version-id 注入引号无法逃逸属性
	attrIR := &RenderIR{
		ItemVersionID: `iv" onmouseover="alert(1)`, ItemID: "i", InteractionID: "text_blank",
	}
	got, err = RenderItem(attrIR)
	if err != nil {
		t.Fatalf("RenderItem: %v", err)
	}
	if strings.Contains(got, `" onmouseover`) {
		t.Fatalf("属性位可逃逸: %s", got)
	}
}
