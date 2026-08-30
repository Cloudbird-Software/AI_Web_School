// html_renderer.go 承载 RenderIR → HTML 片段渲染（T-W2-034；Python 冻结实现
// src/core/render/html_renderer.py 的 Go 重锚定）。
//
// 将强类型 RenderIR blocks 转为可嵌入试卷页面的 HTML 字符串：
//   - text → <p class="item-text">（保留 $...$ / $$...$$ KaTeX 定界符，浏览器端
//     KaTeX auto-render）
//   - fill → <span class="blank"> 下划线空位
//   - choice → <ul class="options"> 选项列表
//   - math_svg → <figure class="math-svg"> 原样嵌入 SVG（白名单校验后）
//   - group → <div class="group"> 素材 + 嵌套子题
//
// 安全（验收标准 #4）：
//   - 所有用户内容（文本/选项标签/图注/素材）经 pyEscape 转义（与 Python
//     html.escape(quote=True) 逐字符同构，含 &#x27; 熵替换，保证与冻结实现
//     输出逐字符对齐，供跨实现快照比对）
//   - SVG 块做白名单校验：拒绝含 <script / on*= 事件属性 / javascript: 链接
//     的 SVG（ErrUnsafeSVG）
//   - 输出无 <script 标签、无 on* 事件属性（DOMPurify 风格白名单）
//
// 为什么不用 text/template 渲染单题：item 片段结构简单且需精细控制 escape，
// string builder 比模板引擎更直白（与冻结实现 f-string 同构）；模板引擎属
// 页面级装配（非目标，见 doc.go）。
package render

import (
	"errors"
	"fmt"
	"regexp"
	"strings"
)

// ErrUnsafeSVG 是 SVG 白名单校验失败的哨兵错误（细分原因见 wrap 文本）.
var ErrUnsafeSVG = errors.New("render: SVG 含危险构造，拒绝渲染")

// SVG 白名单校验：拒绝含危险构造的 SVG。
// 为什么用正则而非 HTML 解析器：SVG 是 XML 子集，解析器引入复杂依赖；
// 纸卷渲染的 SVG 由学科包本地组件产出（T-W2-029），非用户自由输入，
// 此处只做兜底防线而非完整 sanitizer（与冻结实现同口径）.
var (
	scriptRe    = regexp.MustCompile(`(?i)<\s*script`)
	eventAttrRe = regexp.MustCompile(`(?i)\son\w+\s*=`)
	hrefJSRe    = regexp.MustCompile(`(?i)href\s*=\s*['"]?\s*javascript:`)
)

// pyEscaper 是 pyEscape 的单遍替换器（等价于 Python html.escape 的顺序替换：
// & 最先替换后，后续替换引入的 & 不会再被转义，单遍替换与该顺序结果一致）.
var pyEscaper = strings.NewReplacer(
	"&", "&amp;",
	"<", "&lt;",
	">", "&gt;",
	`"`, "&quot;",
	"'", "&#x27;",
)

// pyEscape 与 Python html.escape(s, quote=True) 逐字符同构的 HTML 转义：
// & → &amp;（必须最先）、< → &lt;、> → &gt;、" → &quot;、' → &#x27;。
// 不用标准库 html.EscapeString：其对 ' 的替换是 &#39;，与冻结实现的
// &#x27; 不同——为跨实现快照可比对，这里复刻冻结实现口径.
func pyEscape(text string) string {
	return pyEscaper.Replace(text)
}

// EscapeText 导出 pyEscape（#147 编排/CLI 面组装卷头元数据时的转义入口）：
// 卷页包装层的动态值与单题内容走同一把转义器，不在包外复刻第二份实现
// （转义口径漂移 = 跨实现快照不可比对）.
func EscapeText(text string) string {
	return pyEscape(text)
}

// sanitizeSVG SVG 白名单校验：拒绝 script/事件属性/javascript: 链接。
// 校验通过返回原 svg 字符串；否则返回 ErrUnsafeSVG 包装错误。
// 不做转义——SVG 是结构化 XML，转义会破坏渲染（与冻结实现一致）.
func sanitizeSVG(svg string) (string, error) {
	if scriptRe.MatchString(svg) {
		return "", fmt.Errorf("%w：含 <script> 标签", ErrUnsafeSVG)
	}
	if eventAttrRe.MatchString(svg) {
		return "", fmt.Errorf("%w：含 on* 事件属性", ErrUnsafeSVG)
	}
	if hrefJSRe.MatchString(svg) {
		return "", fmt.Errorf("%w：含 javascript: 链接", ErrUnsafeSVG)
	}
	return svg, nil
}

// RenderItem 渲染单题为 HTML 片段（不含外层页面模板）。
//
// 输出结构（与冻结实现逐字符对齐）：
//
//	<div class="item" data-item-version-id="..." data-item-id="..."
//	     data-interaction-id="..." data-page-break-before=".."
//	     data-keep-with-next=".." data-preferred-columns="..">
//	  <div class="item-number">题号.</div>
//	  <div class="item-body">blocks...</div>
//	  <div class="item-trace">q1 · 短码</div>  <!-- 仅卷上下文提供时 -->
//	</div>
//
// ItemNumber 为空时不渲染题号行；PlacementToken / ItemShortCode 均为空时
// 不渲染追溯行（W3 遗留 S9：卷面印每题短码；单题渲染无卷上下文时保持
// 原输出不变）。layout_hints 作为 data 属性透传，CSS/JS 可据此控制分页。
func RenderItem(ir *RenderIR) (string, error) {
	var b strings.Builder
	b.WriteString(`<div class="item" data-item-version-id="`)
	b.WriteString(pyEscape(ir.ItemVersionID))
	b.WriteString(`" data-item-id="`)
	b.WriteString(pyEscape(ir.ItemID))
	b.WriteString(`" data-interaction-id="`)
	b.WriteString(pyEscape(ir.InteractionID))
	// layout_hints 作为 data 属性透传
	b.WriteString(`" data-page-break-before="`)
	b.WriteString(boolAttr(ir.LayoutHints.PageBreakBefore))
	b.WriteString(`" data-keep-with-next="`)
	b.WriteString(boolAttr(ir.LayoutHints.KeepWithNext))
	b.WriteString(`" data-preferred-columns="`)
	b.WriteString(fmt.Sprintf("%d", ir.LayoutHints.PreferredColumns))
	b.WriteString(`">`)

	if ir.ItemNumber != "" {
		b.WriteString(`<div class="item-number">`)
		b.WriteString(pyEscape(ir.ItemNumber))
		b.WriteString(`.</div>`)
	}

	b.WriteString(`<div class="item-body">`)
	for i, block := range ir.Blocks {
		html, err := renderBlock(block)
		if err != nil {
			return "", fmt.Errorf("block[%d]: %w", i, err)
		}
		b.WriteString(html)
	}
	b.WriteString(`</div>`)

	// 追溯行：卷内位置标识 + 题短码（学生/家长扫码查源，T-W2-037 回溯链入口）
	b.WriteString(renderTrace(ir))

	b.WriteString(`</div>`)
	return b.String(), nil
}

// RenderItems 渲染多题为 HTML 片段序列（供页面模板填充 items_html 插槽）.
func RenderItems(irs []RenderIR) (string, error) {
	var b strings.Builder
	for i := range irs {
		html, err := RenderItem(&irs[i])
		if err != nil {
			return "", fmt.Errorf("item[%d]: %w", i, err)
		}
		b.WriteString(html)
	}
	return b.String(), nil
}

func boolAttr(v bool) string {
	if v {
		return "true"
	}
	return "false"
}

// renderTrace 渲染卷面追溯行（placement_token + item_short_code）。
// 两者都缺省时返回空串（不改变既有输出）；只提供其一时只渲染提供的部分
// （短码是查源主键，优先展示）.
func renderTrace(ir *RenderIR) string {
	if ir.PlacementToken == "" && ir.ItemShortCode == "" {
		return ""
	}
	var b strings.Builder
	b.WriteString(`<div class="item-trace">`)
	if ir.PlacementToken != "" {
		b.WriteString(`<span class="placement-token">`)
		b.WriteString(pyEscape(ir.PlacementToken))
		b.WriteString(`</span>`)
	}
	if ir.ItemShortCode != "" {
		b.WriteString(`<span class="item-short-code">`)
		b.WriteString(pyEscape(ir.ItemShortCode))
		b.WriteString(`</span>`)
	}
	b.WriteString(`</div>`)
	return b.String()
}

// renderBlock 单 block → HTML（按具体类型分发；封闭联合之外的实现
// fail-closed 报错——理论不可达，Block 接口为包内封闭）.
func renderBlock(block Block) (string, error) {
	switch x := block.(type) {
	case TextBlock:
		return renderTextBlock(x), nil
	case FillBlock:
		return renderFillBlock(x), nil
	case ChoiceBlock:
		return renderChoiceBlock(x), nil
	case MathSVGBlock:
		return renderMathSVGBlock(x)
	case GroupBlock:
		return renderGroupBlock(x)
	default:
		return "", fmt.Errorf("render: 未知 block 类型 %T（fail-closed）", block)
	}
}

// renderTextBlock 文本块 → <p>（保留 KaTeX 定界符 $...$ / $$...$$）.
func renderTextBlock(block TextBlock) string {
	var b strings.Builder
	b.WriteString(`<p class="item-text">`)
	b.WriteString(pyEscape(block.Value))
	b.WriteString(`</p>`)
	return b.String()
}

// renderFillBlock 填空块 → 下划线空位 <span class="blank">。
// data-* 属性承载 blank_id/kind/unit，供前端采集与评分对齐；
// width 控制下划线字符宽度（0=默认）.
func renderFillBlock(block FillBlock) string {
	var b strings.Builder
	b.WriteString(`<span class="blank" data-blank-id="`)
	b.WriteString(pyEscape(block.BlankID))
	b.WriteString(`" data-kind="`)
	b.WriteString(pyEscape(block.Kind))
	b.WriteString(`"`)
	if block.Unit != "" {
		b.WriteString(` data-unit="`)
		b.WriteString(pyEscape(block.Unit))
		b.WriteString(`"`)
	}
	if block.Width > 0 {
		b.WriteString(fmt.Sprintf(` style="--blank-width:%dch"`, block.Width))
	}
	b.WriteString(`></span>`)
	return b.String()
}

// renderChoiceBlock 选择题块 → <ul class="options"> 选项列表。
// mode 标注在 class 上（single/multi），CSS 控制圈选/勾选样式；
// 选项标签（A/B/C/D）与文本均转义.
func renderChoiceBlock(block ChoiceBlock) string {
	var b strings.Builder
	modeClass := "options multi"
	if block.Mode == ChoiceModeSingle {
		modeClass = "options single"
	}
	b.WriteString(`<ul class="`)
	b.WriteString(modeClass)
	b.WriteString(`">`)
	for _, opt := range block.Options {
		b.WriteString(`<li><span class="option-label">`)
		b.WriteString(pyEscape(opt.ID))
		b.WriteString(`</span><span class="option-text">`)
		b.WriteString(pyEscape(opt.Label))
		b.WriteString(`</span></li>`)
	}
	b.WriteString(`</ul>`)
	return b.String()
}

// renderMathSVGBlock 数学 SVG 块 → <figure class="math-svg"> 原样嵌入.
func renderMathSVGBlock(block MathSVGBlock) (string, error) {
	svg, err := sanitizeSVG(block.SVG)
	if err != nil {
		return "", err
	}
	var b strings.Builder
	b.WriteString(`<figure class="math-svg">`)
	b.WriteString(svg)
	if block.Caption != "" {
		b.WriteString(`<figcaption>`)
		b.WriteString(pyEscape(block.Caption))
		b.WriteString(`</figcaption>`)
	}
	b.WriteString(`</figure>`)
	return b.String(), nil
}

// renderGroupBlock 题组块 → <div class="group"> 素材 + 嵌套子题.
func renderGroupBlock(block GroupBlock) (string, error) {
	var b strings.Builder
	b.WriteString(`<div class="group">`)
	if block.Material != "" {
		b.WriteString(`<div class="group-material">`)
		b.WriteString(pyEscape(block.Material))
		b.WriteString(`</div>`)
	}
	b.WriteString(`<div class="group-items">`)
	for i := range block.Items {
		html, err := RenderItem(&block.Items[i])
		if err != nil {
			return "", fmt.Errorf("group 子题[%d]: %w", i, err)
		}
		b.WriteString(html)
	}
	b.WriteString(`</div></div>`)
	return b.String(), nil
}
