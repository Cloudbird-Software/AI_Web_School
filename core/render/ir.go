// ir.go 承载渲染中间表示（T-W2-033；Python 冻结实现 src/core/render/ir.py 的
// Go 重锚定）。
//
// IR 是 ItemVersion → 渲染出口（HTML/PDF）之间的纯内容表示，承载：题号、
// 题面文本、选项、填空位、数学 SVG、题组嵌套、版式提示。
//
// 为什么不直接从 ItemVersion.content 渲染：content.blocks 是 permissive dict
// （契约 §5 未强制 schema），交互类型决定块语义；IR 把这种隐式语义显式化为
// 强类型 Block，让 HTML/PDF 渲染器只面对一种稳定契约。
//
// Block 是封闭联合：Python 用 type 字段 discriminated union，Go 用接口 +
// 五个具体类型表达同一联合；新增块类型必须先改冻结契约（波内契约冻结）。
package render

// BlockType 是块的类型判别值（与 Python ir.py 各 Block 的 type 字面量同域）.
type BlockType string

// 五种块类型（与冻结契约一致；discriminated union 的判别面）.
const (
	BlockText    BlockType = "text"
	BlockFill    BlockType = "fill"
	BlockChoice  BlockType = "choice"
	BlockMathSVG BlockType = "math_svg"
	BlockGroup   BlockType = "group"
)

// Block 是渲染块的封闭联合（Python ir.py Block discriminated union 的 Go 形态）。
// 只有本包五个具体块类型实现本接口；渲染器按具体类型分发，未知实现报错
// （fail-closed，不静默丢块）.
type Block interface {
	// blockKind 返回块类型判别值（包内封闭：外部类型不可冒充）.
	blockKind() BlockType
}

// TextBlock 纯文本块（题面段落、说明文字）。
// Value 为已渲染的最终文本（变量已替换、语料已嵌入）；若含数学公式，用
// KaTeX 定界符 $...$ / $$...$$ 标记，由 HTML 渲染器原样保留（浏览器端
// KaTeX auto-render）.
type TextBlock struct {
	Value string
}

// FillBlock 填空块（text_blank / numeric_blank 交互的空位）。
// Kind 区分文本填空与数值填空（呼应 interaction.yaml）；Numeric 情形下
// Unit 可选（题目声明需要单位时填单位 id）。BlankID 与 scoring_ref 的
// blanks 键对齐，评分器按 blank_id 逐空判分。Width 为空位显示长度
// （字符数），0 表示用默认下划线长度.
type FillBlock struct {
	BlankID string
	Kind    string // "text" | "numeric"
	Unit    string // 空 = 无单位
	Width   int
}

// FillKind 两值域（与 Python FillBlock.kind Literal 同域）.
const (
	FillKindText    = "text"
	FillKindNumeric = "numeric"
)

// OptionItem 选项条目（single_choice / multi_choice 交互的选项）.
type OptionItem struct {
	ID    string
	Label string
}

// ChoiceBlock 选择题块（single_choice / multi_choice 交互的选项集合）。
// Mode 由 interaction_ref.interaction_id 推导：single_choice→single，
// multi_choice→multi。Options 为已渲染的最终选项文本（变量已替换）.
type ChoiceBlock struct {
	Mode    string // ChoiceModeSingle | ChoiceModeMulti
	Options []OptionItem
}

// ChoiceMode 两值域（与 Python ChoiceBlock.mode Literal 同域）.
const (
	ChoiceModeSingle = "single"
	ChoiceModeMulti  = "multi"
)

// MathSVGBlock 数学 SVG 块（学科包渲染组件产出的 SVG 原样嵌入）。
// SVG 为完整 <svg>...</svg> 字符串；Caption 为图注（可选）。
// 核心域不解释 SVG 语义，只做白名单校验后的透传嵌入——学科零特判（A5）.
type MathSVGBlock struct {
	SVG     string
	Caption string // 空 = 无图注
}

// GroupBlock 题组块（一材多题：共享素材 + 嵌套子题 IR）。
// 题组（item_group）的 RenderIR 用一个 group 块承载共享素材与子题列表；
// 子题各自是完整的 RenderIR（递归结构）。Material 为共享素材的已渲染文本
// （语篇/图表说明等），可为空.
type GroupBlock struct {
	Material string // 空 = 无共享素材
	Items    []RenderIR
}

// blockKind 实现：五个具体类型的判别面（包内封闭）.
func (TextBlock) blockKind() BlockType    { return BlockText }
func (FillBlock) blockKind() BlockType    { return BlockFill }
func (ChoiceBlock) blockKind() BlockType  { return BlockChoice }
func (MathSVGBlock) blockKind() BlockType { return BlockMathSVG }
func (GroupBlock) blockKind() BlockType   { return BlockGroup }

// LayoutHints 版式提示（渲染器参考，非强制）。
//
// 为什么用提示而非强制：不同出口（PDF 分页 vs HTML 流式）对版式约束的
// 执行能力不同；IR 表达意图，出口自行取舍。
//   - PageBreakBefore：本块前强制分页（大题开始）
//   - KeepWithNext：与下一块保同页（题干+选项不被分页隔开）
//   - PreferredColumns：选项/填空排列列数（1=纵向，2=两列）
type LayoutHints struct {
	PageBreakBefore  bool
	KeepWithNext     bool
	PreferredColumns int
}

// RenderIR 渲染中间态顶层（T-W2-033）。
//
// 一份 RenderIR = 一道题（含题组嵌套）的纯内容表示：
//   - ItemVersionID / ItemID：溯源到 item_version 表（D3 内容寻址）
//   - InteractionID：来自 interaction_ref，决定作答采集与评分契约
//   - ItemNumber：卷内题号（由组卷器分配，IR 自身不含排序逻辑）
//   - PlacementToken / ItemShortCode：卷内位置标识与题短码（W3 遗留 S9：
//     卷面印每题短码；由组卷器/批处理在组卷时分配，IR 只做透传展示；
//     单题渲染无卷上下文时为空，不输出追溯行）
//   - Blocks：题面内容序列（text/fill/choice/math_svg/group）
//   - LayoutHints：版式提示
//
// 不可变纪律：RenderIR 视为不可变快照（序列化结果可物化为 rendered_snapshot，
// D2 复现不依赖引擎）；任何适配（学段/注音）返回副本，绝不修改入参。
type RenderIR struct {
	ItemVersionID  string
	ItemID         string
	InteractionID  string
	ItemNumber     string // 空 = 无题号行
	PlacementToken string // 空 = 追溯行缺省
	ItemShortCode  string // 空 = 追溯行缺省
	Blocks         []Block
	LayoutHints    LayoutHints
}
