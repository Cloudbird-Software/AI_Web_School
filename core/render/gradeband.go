// gradeband.go 承载在线渲染学段适配层（T-W4-037；Python 冻结实现
// src/core/render/gradeband_adapter.py 的 Go 重锚定）：
// AdaptForGradeband 按学段注入低段差异化——注音层 <ruby>、朗读按钮 data 属性、
// 大字号 CSS 类、数字键盘触发标记；中段（M）/ 高段（H）保持常规呈现，
// 不注入低段专属元素（验收 #3）。
//
// 设计要点：
//   - 核心域零特判（A5/X6）：本模块是核心域，不 import 学科包/学段包；
//     学段参数通过 Hints 注入（由调用方加载学段包 config.yaml 生成）。
//   - IR 不可变：本模块不修改入参 RenderIR（值拷贝语义）；返回适配后的
//     IR 副本 + 渲染提示。
//   - 与学科渲染组件正交：本适配层只做学段差异化（注音/字号/键盘/朗读），
//     不感知学科（数学 SVG / 语文语篇 / 英语听力均由各组件自处理）。
package render

// 学段适配注入的 HTML 标记常量（与冻结实现同名常量逐字对齐）.
const (
	// LowBandFontClass 低段大字号 CSS 类（HTML 渲染器附在 item 容器上）
	LowBandFontClass = "gb-low-large-font"
	// ReadAloudDataAttr 低段朗读按钮 data 属性前缀
	ReadAloudDataAttr = "data-read-aloud"
	// NumericKeyboardDataAttr 低段数字键盘触发标记
	NumericKeyboardDataAttr = "data-keyboard"
	// PhoneticDataAttr 低段注音层 CSS 类（标注在 item 容器上，CSS 控制注音渲染样式）
	PhoneticDataAttr = "data-phonetic"
)

// numericKeyboardInteractions 触发数字键盘的交互类型（与低学段包 render_hints
// 同约定；核心不 import 学段包，此常量是核心域交互类型分类）.
var numericKeyboardInteractions = map[string]bool{
	"numeric_blank":      true,
	"text_blank_numeric": true,
}

// Hints 是调用方注入的学段渲染提示（学段包 render_hints 产出的形状；
// 零值字段 = 不注入该项，与冻结实现「中/高段默认无学段专属元素」一致）.
type Hints struct {
	Phonetic         bool   // 是否注音（低段）
	PhoneticCoverage string // 注音覆盖范围（full / out_of_syllabus），可空
	FontSize         string // 字号（如 "24px"），空=默认
	ReadAloud        bool   // 是否注入朗读按钮 data 属性
	Keyboard         string // 键盘类型（"numeric" 仅数值填空类交互触发），空=默认
	KeyboardAllowed  string // 允许键集（如 "0123456789"），可空
}

// HTMLHints 是渲染器消费的学段样式提示（CSS 类 / data 属性 / 注音标记），
// 对应冻结实现返回的 html_hints dict.
type HTMLHints struct {
	GradeBand        string
	Phonetic         bool
	PhoneticCoverage string
	FontSize         string
	FontClass        string // LowBandFontClass（仅低段且配置了字号），否则空
	ReadAloud        bool
	ReadAloudAttr    string // ReadAloudDataAttr（配置朗读时），否则空
	Keyboard         string
	KeyboardAllowed  string
	KeyboardAttr     string // NumericKeyboardDataAttr（低段+数值键盘+数值类交互），否则空
	PhoneticAttr     string // PhoneticDataAttr（配置注音时），否则空
}

// GradeBandAdaptation 是 AdaptForGradeband 的返回结果.
type GradeBandAdaptation struct {
	// IR 适配后的 RenderIR 副本（学段专属标记注入 text 块值；入参不变）。
	IR RenderIR
	// HTMLHints 渲染器消费的学段样式提示。
	HTMLHints HTMLHints
	// GradeBand 学段（L/M/H）。
	GradeBand string
	// PhoneticApplied 是否实际注入了注音层（低段 + 配置注音 + 提供
	// phoneticMap 且适配后存在非空文本块时 true）。
	PhoneticApplied bool
}

// IsLowBand 判定是否低段（核心域不感知学段包，仅按标识 'L' 判定；
// 与冻结实现 _is_low_band 同构）.
func IsLowBand(gradeBand string) bool { return gradeBand == "L" }

// AdaptForGradeband 按学段适配 RenderIR，返回适配后 IR + 渲染器样式提示。
//
// ir: 待适配的 RenderIR（不可变；本函数返回副本，绝不修改入参）。
// gradeBand: 学段（L/M/H）。
// hints: 学段包 render_hints 产出的提示（含 phonetic/font_size/keyboard 等）；
// nil 时用核心默认（不注入任何学段元素）。
// phoneticMap: 注音字典 {字符: 拼音}，仅低段 + hints.Phonetic 时应用；
// nil 时不烘焙注音（适配层只输出注音标记，由前端按需注音）。
//
// 注音烘焙语义（与冻结实现一致）：RenderIR 是「最终内容表示」，注音是低段
// 专属的呈现，烘焙进 text 块 Value 后 HTML/PDF 渲染器无需感知学段差异——
// 它们只面对一种稳定契约（文本块即 HTML 片段）。题组共享素材 material 不
// 注音（素材通常是语篇，由学科包渲染组件单独处理注音）。
func AdaptForGradeband(ir RenderIR, gradeBand string, hints *Hints, phoneticMap map[string]string) GradeBandAdaptation {
	var h Hints
	if hints != nil {
		h = *hints
	}
	isLow := IsLowBand(gradeBand)

	// 注音应用：低段 + phonetic=true + 提供 phoneticMap 时烘焙
	adapted := ir
	phoneticApplied := false
	if isLow && h.Phonetic && len(phoneticMap) > 0 {
		adapted = adaptIR(ir, phoneticMap)
		// 与冻结实现同判据：适配后顶层存在非空文本块才算实际注入
		for _, b := range adapted.Blocks {
			if tb, ok := b.(TextBlock); ok && tb.Value != "" {
				phoneticApplied = true
				break
			}
		}
	}

	htmlHints := HTMLHints{
		GradeBand:        gradeBand,
		Phonetic:         h.Phonetic,
		PhoneticCoverage: h.PhoneticCoverage,
		FontSize:         h.FontSize,
		ReadAloud:        h.ReadAloud,
		Keyboard:         h.Keyboard,
		KeyboardAllowed:  h.KeyboardAllowed,
	}
	if isLow && h.FontSize != "" {
		htmlHints.FontClass = LowBandFontClass
	}
	if h.ReadAloud {
		htmlHints.ReadAloudAttr = ReadAloudDataAttr
	}
	if isLow && h.Keyboard == "numeric" && numericKeyboardInteractions[ir.InteractionID] {
		htmlHints.KeyboardAttr = NumericKeyboardDataAttr
	}
	if h.Phonetic {
		htmlHints.PhoneticAttr = PhoneticDataAttr
	}

	return GradeBandAdaptation{
		IR:              adapted,
		HTMLHints:       htmlHints,
		GradeBand:       gradeBand,
		PhoneticApplied: phoneticApplied,
	}
}

// adaptIR 对单题 IR 应用注音烘焙（返回副本；choice/fill/math_svg 学段不影响
// 内容——CSS/交互由 HTMLHints 控制）.
func adaptIR(ir RenderIR, phoneticMap map[string]string) RenderIR {
	newBlocks := make([]Block, 0, len(ir.Blocks))
	for _, b := range ir.Blocks {
		newBlocks = append(newBlocks, adaptBlock(b, phoneticMap))
	}
	ir.Blocks = newBlocks
	return ir
}

// adaptBlock 单 block 学段适配（当前仅文本块受注音影响；其他类型透传；
// 题组递归适配子题 IR，共享素材 material 不注音）.
func adaptBlock(block Block, phoneticMap map[string]string) Block {
	switch x := block.(type) {
	case TextBlock:
		return TextBlock{Value: ApplyPhoneticToText(x.Value, phoneticMap)}
	case GroupBlock:
		items := make([]RenderIR, 0, len(x.Items))
		for _, sub := range x.Items {
			items = append(items, adaptIR(sub, phoneticMap))
		}
		return GroupBlock{Material: x.Material, Items: items}
	default:
		// choice / fill / math_svg：学段不影响内容
		return block
	}
}
