// phonetic.go 承载注音覆盖组件（T-W4-037；Python 冻结实现
// src/core/render/components/phonetic_overlay.py 的 Go 重锚定）。
//
// 为什么独立成组件：注音是低段专属能力，与 RenderIR 的文本块语义正交；
// 中/高段调用方完全不引用本组件（不注入注音层）。
//
// 设计要点：
//   - 纯函数 + 数据驱动：phonetic 注解由调用方提供（学段包/学科包按字典
//     生成，核心不感知拼音来源）；本组件只负责把注解应用到文本生成 <ruby>。
//   - HTML 安全：文本与拼音均经 pyEscape 转义（与冻结实现 html.escape 同构），
//     杜绝 XSS。
//   - 核心域零特判（A5）：本组件不 import 学科包/学段包；拼音字典通过
//     phoneticMap 参数注入。
package render

import "strings"

// ApplyPhoneticToText 为文本逐字应用注音，输出 <ruby> HTML 片段.
//
// 输出示例（input "小鸟飞翔", map={"小":"xiǎo","鸟":"nǐao","飞":"fēi","翔":"xiáng"}）：
//
//	<ruby>小<rp>(</rp><rt>xiǎo</rt><rp>)</rp></ruby>...
//
// text: 待注音的纯文本（已是渲染最终态，变量替换后）。
// phoneticMap: {字符: 拼音} 映射；nil 或空 → 原样转义返回（不注音）。
// 调用方决定覆盖范围（full=全文 / out_of_syllabus=仅超纲字）。
//
// 未在 map 中的字符原样转义输出（不强制全文注音；调用方决定覆盖范围）。
// 拼音中的声调符号（如 ǎ ē ī ō ū ǖ）是 Unicode 字符，HTML 直接支持，
// 无需额外编码。<rp> 提供不支持 ruby 的浏览器回退显示。
// 按 rune（码点）逐字处理，与 Python str 按码点迭代同构。
func ApplyPhoneticToText(text string, phoneticMap map[string]string) string {
	if len(phoneticMap) == 0 {
		// 无注音字典：仅做 HTML 转义（保持调用方安全契约）
		return pyEscape(text)
	}
	var b strings.Builder
	for _, ch := range text {
		pinyin, ok := phoneticMap[string(ch)]
		if !ok {
			b.WriteString(pyEscape(string(ch)))
			continue
		}
		// <ruby>字符<rp>(</rp><rt>拼音</rt><rp>)</rp></ruby>
		b.WriteString("<ruby>")
		b.WriteString(pyEscape(string(ch)))
		b.WriteString("<rp>(</rp><rt>")
		b.WriteString(pyEscape(pinyin))
		b.WriteString("</rt><rp>)</rp></ruby>")
	}
	return b.String()
}

// HasPhoneticCoverage 检查文本是否至少有一个字符被注音（用于断言注音是否生效）.
//
// 用于适配层断言：低段全文注音模式下，非空文本至少应有注音。
// 按 rune（码点）逐字检查，与 Python str 迭代同构.
func HasPhoneticCoverage(text string, phoneticMap map[string]string) bool {
	if text == "" || len(phoneticMap) == 0 {
		return false
	}
	for _, ch := range text {
		if _, ok := phoneticMap[string(ch)]; ok {
			return true
		}
	}
	return false
}
