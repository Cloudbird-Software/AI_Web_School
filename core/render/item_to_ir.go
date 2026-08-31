// item_to_ir.go 承载 ItemVersion → RenderIR 映射（T-W2-033；Python 冻结实现
// src/core/render/item_to_ir.py 的 Go 重锚定）。
//
// 将 ItemVersion 六大块中的 content.blocks（permissive dict）转换为强类型
// RenderIR blocks，保留题号、选项、填空位置、题组嵌套。
//
// 输入形态：Go 侧只吃 dict 形态（map[string]any，与 ORM/Pydantic 序列化形态
// 一致）——批处理从 serving 视图读出的就是 dict（Python 冻结实现的入参三态
// ORM/Pydantic/dict 在 Go 进程内只有 dict 一态，序列化形状相同，语义不变）。
//
// 失败面（fail fast，避免静默丢题）：content.blocks 含未知 type、缺必要字段、
// 缺 interaction_ref.interaction_id、题组子题形态不合法 → 返回错误。
package render

import (
	"encoding/json"
	"errors"
	"fmt"
	"sort"
	"strconv"
)

// ErrInvalidItemVersion 是 ItemVersion → IR 映射的哨兵错误：入参违反内容契约
// （细分原因见 wrap 文本）。调用方按 errors.Is 分支处理，不用字符串匹配.
var ErrInvalidItemVersion = errors.New("render: ItemVersion 不满足内容契约，拒绝转换为 IR")

// interaction_id → 默认 choice mode / fill kind 映射
// 为什么需要：content.blocks 可能省略 mode/kind，由 interaction_id 推导
// （Python 冻结实现 _INTERACTION_CHOICE_MODE / _INTERACTION_FILL_KIND）.
var interactionChoiceMode = map[string]string{
	"single_choice": ChoiceModeSingle,
	"multi_choice":  ChoiceModeMulti,
}

var interactionFillKind = map[string]string{
	"text_blank":    FillKindText,
	"numeric_blank": FillKindNumeric,
}

// ItemToIRInput 是 ItemToIR 的可选上下文（由组卷器/批处理分配，Python 冻结
// 实现的关键字参数形态）.
type ItemToIRInput struct {
	// ItemNumber 卷内题号（可选，空=不渲染题号行）
	ItemNumber string
	// PlacementToken 卷内位置标识（如 'q1'/'q2.sub1'，可选；W3 遗留 S9）
	PlacementToken string
	// ItemShortCode 题短码（paper_item.item_short_code，可选；
	// 与 PlacementToken 一起印于卷面供扫码查源）
	ItemShortCode string
}

// ItemToIR 将 ItemVersion（dict 形态）转换为 RenderIR.
//
// 映射语义与 Python item_to_ir 逐字段对齐：
//   - interaction_ref.interaction_id 必填（缺顶层 interaction_id 兜底时报错）；
//   - content.blocks 按 type 分发转换；mode/kind 优先取 block 声明，否则由
//     interaction_id 推导（缺省 single/text）；
//   - content.layout_hints 可选（缺省 PreferredColumns=1，其余 false）；
//   - item_version_id / item_id 缺省为空串（与冻结实现 `or ""` 一致）。
//
// 与冻结实现的显式偏离（均收紧、不放宽，见各 helper 注释）：数值字段
// 拒绝小数截断（Python int() 静默截断）；布尔字段拒绝真值语义化（Python
// bool("false")==True 的陷阱）；字符串字段拒绝 str() 强转（Python
// str(raw["value"]) 会把任意标量静默字符串化，跨实现产物不可比对）。
func ItemToIR(itemVersion map[string]any, in ItemToIRInput) (*RenderIR, error) {
	interactionID, err := resolveInteractionID(itemVersion)
	if err != nil {
		return nil, err
	}

	content, _ := itemVersion["content"].(map[string]any)
	// content 非 dict 时按空 content 处理（与冻结实现 isinstance(content, Mapping)
	// 的退化分支一致：raw_blocks=[]、layout_hints 缺省）
	rawBlocks, _ := content["blocks"].([]any)

	blocks := make([]Block, 0, len(rawBlocks))
	for i, rb := range rawBlocks {
		raw, ok := rb.(map[string]any)
		if !ok {
			return nil, fmt.Errorf("%w: content.blocks[%d] 必须是 dict，得到 %T", ErrInvalidItemVersion, i, rb)
		}
		b, err := convertBlock(raw, interactionID, false)
		if err != nil {
			return nil, err
		}
		blocks = append(blocks, b)
	}

	hints, err := parseLayoutHints(content["layout_hints"])
	if err != nil {
		return nil, err
	}

	return &RenderIR{
		ItemVersionID:  stringField(itemVersion, "item_version_id"),
		ItemID:         stringField(itemVersion, "item_id"),
		InteractionID:  interactionID,
		ItemNumber:     in.ItemNumber,
		PlacementToken: in.PlacementToken,
		ItemShortCode:  in.ItemShortCode,
		Blocks:         blocks,
		LayoutHints:    hints,
	}, nil
}

// resolveInteractionID 取 interaction_ref.interaction_id（缺顶层
// interaction_id 兜底；两者皆缺 → 错误，与冻结实现同判据）.
func resolveInteractionID(itemVersion map[string]any) (string, error) {
	ref, _ := itemVersion["interaction_ref"].(map[string]any)
	if ref != nil {
		if id, _ := ref["interaction_id"].(string); id != "" {
			return id, nil
		}
	}
	if id, _ := itemVersion["interaction_id"].(string); id != "" {
		return id, nil
	}
	return "", fmt.Errorf("%w: 缺 interaction_ref.interaction_id", ErrInvalidItemVersion)
}

// stringField 取字符串字段；非字符串按空串处理（冻结实现 str() 强转的收敛面：
// JSONB 源头的身份字段只会是字符串，非字符串即脏数据，取空交由上层契约拦截）.
func stringField(m map[string]any, key string) string {
	s, _ := m[key].(string)
	return s
}

// convertBlock 单 block dict → 强类型 Block（按 type 分发）。
//
// strict=true 用于题组子题的「已是 IR dict」形态：该形态在冻结实现里经
// pydantic model_validate（extra=forbid），块内未知键拒绝、必填字段（fill.kind /
// choice.mode / choice.options）缺失即拒绝——已用冻结实现实证（缺 kind 触发
// ValidationError）。strict=false 用于 ItemVersion 形态（冻结实现直接构造
// 类型对象，宽容未知键，mode/kind 缺省由 interaction_id 推导）.
func convertBlock(raw map[string]any, interactionID string, strict bool) (Block, error) {
	if strict {
		if err := rejectUnknownKeys(raw, blockKnownKeys(raw)); err != nil {
			return nil, err
		}
		switch raw["type"] {
		case string(BlockFill):
			if err := requireStrictString(raw, "kind"); err != nil {
				return nil, err
			}
		case string(BlockChoice):
			if err := requireStrictString(raw, "mode"); err != nil {
				return nil, err
			}
			if _, ok := raw["options"]; !ok {
				return nil, fmt.Errorf("%w: choice 块缺 options（严格解析路径必填）", ErrInvalidItemVersion)
			}
		}
	}
	// 内容方言归一（kind 方言兼容层）：A 线实例化引擎（core/instantiation，
	// Go packs/subjectmath 同构）产出的 content block 是 {kind, template,
	// rendered} 形态（黄金数据集 expected_content_snapshot 同构）；B 线
	// 装配器与 IR 严格形态用 {type, value}。冻结契约对 content.blocks 保持
	// permissive（openapi §2.2 未强制 schema），渲染边界同时接受两种方言：
	// type 缺失而 kind 存在时按 kind 分发（E2E 实证 papergen 对 mathgen
	// 产物全量 IR 转换失败即此分歧）。
	blockType, _ := raw["type"].(string)
	if blockType == "" {
		if kind, ok := raw["kind"].(string); ok && kind != "" {
			blockType = kind
		}
	}
	switch BlockType(blockType) {
	case BlockText:
		return convertTextBlock(raw)
	case BlockFill:
		return convertFillBlock(raw, interactionID)
	case BlockChoice:
		return convertChoiceBlock(raw, interactionID)
	case BlockMathSVG:
		return convertMathSVGBlock(raw)
	case BlockGroup:
		return convertGroupBlock(raw, interactionID)
	case "":
		return nil, fmt.Errorf("%w: block 缺 type 字段: %v", ErrInvalidItemVersion, raw)
	default:
		return nil, fmt.Errorf("%w: 未知 block type: %q", ErrInvalidItemVersion, blockType)
	}
}

// requireStrictString 严格解析路径的必填字符串字段校验（pydantic 必填
// Literal/str 字段的 fail-closed 形态：缺失/非字符串/空串均拒绝）.
func requireStrictString(raw map[string]any, key string) error {
	v, ok := raw[key]
	if !ok {
		return fmt.Errorf("%w: %s 必填（严格解析路径）", ErrInvalidItemVersion, key)
	}
	s, isStr := v.(string)
	if !isStr || s == "" {
		return fmt.Errorf("%w: %s 必须是非空字符串，得到 %T", ErrInvalidItemVersion, key, v)
	}
	return nil
}

func convertTextBlock(raw map[string]any) (Block, error) {
	value, ok := raw["value"].(string)
	if !ok {
		// kind 方言（A 线引擎产物）：value 缺失时取 rendered（插值后文本），
		// 再退 template（未插值模板）。两者皆缺才拒绝——与冻结实现对
		// value 的 str() 收紧口径不冲突（此处不字符串化任意标量）。
		if rendered, rok := raw["rendered"].(string); rok && rendered != "" {
			return TextBlock{Value: rendered}, nil
		}
		if tmpl, tok := raw["template"].(string); tok && tmpl != "" {
			return TextBlock{Value: tmpl}, nil
		}
		// 显式偏离（收紧）：冻结实现 str(raw["value"]) 把任意标量静默字符串化
		// （str(5)=="5"、str(True)=="True"），跨实现产物不可比对——Go 侧拒绝。
		return nil, fmt.Errorf("%w: text 块缺 value（须为字符串）", ErrInvalidItemVersion)
	}
	return TextBlock{Value: value}, nil
}

// convertFillBlock 填空块转换：kind 优先取 block 声明，否则由 interaction_id 推导.
func convertFillBlock(raw map[string]any, interactionID string) (Block, error) {
	blankID, ok := raw["blank_id"].(string)
	if !ok {
		// 显式偏离（收紧）：冻结实现 str() 强转，见 convertTextBlock。
		return nil, fmt.Errorf("%w: fill 块缺 blank_id（须为字符串）", ErrInvalidItemVersion)
	}
	// kind 声明为非字符串真值时冻结实现会在 pydantic 构造处拒绝（Literal 值域），
	// Go 侧同判据 fail-closed；缺失/nil/空串（Python 真值语义为 falsy）走推导.
	rawKind, hasKind := raw["kind"]
	if hasKind && rawKind != nil {
		if _, isStr := rawKind.(string); !isStr {
			return nil, fmt.Errorf("%w: fill 块 kind 必须是字符串，得到 %T", ErrInvalidItemVersion, rawKind)
		}
	}
	kind, _ := raw["kind"].(string)
	if kind == "" {
		kind = interactionFillKind[interactionID]
		if kind == "" {
			kind = FillKindText
		}
	}
	if kind != FillKindText && kind != FillKindNumeric {
		return nil, fmt.Errorf("%w: fill 块 kind %q 不在 text/numeric 值域", ErrInvalidItemVersion, kind)
	}
	if err := optionalStringField(raw, "unit"); err != nil {
		return nil, err
	}
	unit, _ := raw["unit"].(string)
	width, err := intField(raw, "width", 0)
	if err != nil {
		return nil, err
	}
	return FillBlock{BlankID: blankID, Kind: kind, Unit: unit, Width: width}, nil
}

// optionalStringField 校验可选字符串字段：缺省/nil 放行；存在但非字符串拒绝
// （pydantic Optional[str] 的 fail-closed 形态）.
func optionalStringField(raw map[string]any, key string) error {
	v, present := raw[key]
	if !present || v == nil {
		return nil
	}
	if _, ok := v.(string); !ok {
		return fmt.Errorf("%w: %s 必须是字符串，得到 %T", ErrInvalidItemVersion, key, v)
	}
	return nil
}

// convertChoiceBlock 选择题块转换：mode 优先取 block 声明，否则由 interaction_id 推导.
func convertChoiceBlock(raw map[string]any, interactionID string) (Block, error) {
	// mode 判据与 fill.kind 同构（见上）.
	rawMode, hasMode := raw["mode"]
	if hasMode && rawMode != nil {
		if _, isStr := rawMode.(string); !isStr {
			return nil, fmt.Errorf("%w: choice 块 mode 必须是字符串，得到 %T", ErrInvalidItemVersion, rawMode)
		}
	}
	mode, _ := raw["mode"].(string)
	if mode == "" {
		mode = interactionChoiceMode[interactionID]
		if mode == "" {
			mode = ChoiceModeSingle
		}
	}
	if mode != ChoiceModeSingle && mode != ChoiceModeMulti {
		return nil, fmt.Errorf("%w: choice 块 mode %q 不在 single/multi 值域", ErrInvalidItemVersion, mode)
	}
	rawOptions, _ := raw["options"].([]any)
	options := make([]OptionItem, 0, len(rawOptions))
	for i, ro := range rawOptions {
		o, ok := ro.(map[string]any)
		if !ok {
			return nil, fmt.Errorf("%w: choice 块 options[%d] 必须是 dict，得到 %T", ErrInvalidItemVersion, i, ro)
		}
		id, ok1 := o["id"].(string)
		label, ok2 := o["label"].(string)
		// 显式偏离（收紧）：冻结实现 str(o["id"]) 强转，见 convertTextBlock；
		// 缺键在冻结实现是 KeyError，此处同判据报错。
		if !ok1 || !ok2 {
			return nil, fmt.Errorf("%w: choice 块 options[%d] 缺 id/label（须为字符串）", ErrInvalidItemVersion, i)
		}
		options = append(options, OptionItem{ID: id, Label: label})
	}
	return ChoiceBlock{Mode: mode, Options: options}, nil
}

func convertMathSVGBlock(raw map[string]any) (Block, error) {
	svg, ok := raw["svg"].(string)
	if !ok {
		// 显式偏离（收紧）：冻结实现 str() 强转，见 convertTextBlock。
		return nil, fmt.Errorf("%w: math_svg 块缺 svg（须为字符串）", ErrInvalidItemVersion)
	}
	if err := optionalStringField(raw, "caption"); err != nil {
		return nil, err
	}
	caption, _ := raw["caption"].(string)
	return MathSVGBlock{SVG: svg, Caption: caption}, nil
}

// convertGroupBlock 题组块转换：递归转换子题。
//
// 子题允许两种形态（与冻结实现同判据）：
//   - 完整 ItemVersion dict（含 content/interaction_ref）：走 ItemToIR 路径
//   - 已是 IR dict（顶层有 blocks + interaction_id）：走严格解析路径
//
// 其他形态 → 错误（fail fast，避免静默丢题）.
func convertGroupBlock(raw map[string]any, interactionID string) (Block, error) {
	if err := optionalStringField(raw, "material"); err != nil {
		return nil, err
	}
	material, _ := raw["material"].(string)
	rawItems, _ := raw["items"].([]any)
	subItems := make([]RenderIR, 0, len(rawItems))
	for i, sub := range rawItems {
		m, ok := sub.(map[string]any)
		if !ok {
			return nil, fmt.Errorf("%w: 题组子题必须是 dict，得到 %T（items[%d]）", ErrInvalidItemVersion, sub, i)
		}
		// ItemVersion 形态：有 content 键（blocks 嵌套在 content 下）或 interaction_ref
		if _, hasContent := m["content"]; hasContent {
			ir, err := ItemToIR(m, ItemToIRInput{})
			if err != nil {
				return nil, err
			}
			subItems = append(subItems, *ir)
			continue
		}
		if _, hasRef := m["interaction_ref"]; hasRef {
			ir, err := ItemToIR(m, ItemToIRInput{})
			if err != nil {
				return nil, err
			}
			subItems = append(subItems, *ir)
			continue
		}
		// 已是 IR 形态：顶层有 blocks + interaction_id
		if _, hasBlocks := m["blocks"]; hasBlocks {
			if _, hasIID := m["interaction_id"]; hasIID {
				ir, err := irFromDict(m)
				if err != nil {
					return nil, err
				}
				subItems = append(subItems, *ir)
				continue
			}
		}
		return nil, fmt.Errorf("%w: 题组子题缺少必要字段（需 content/interaction_ref 或 blocks/interaction_id）: %v", ErrInvalidItemVersion, m)
	}
	return GroupBlock{Material: material, Items: subItems}, nil
}

// irFromDict 解析「已是 IR dict」形态（冻结实现 RenderIR.model_validate）：
// 必填 item_version_id / item_id / interaction_id，blocks 严格校验
// （extra=forbid 语义），layout_hints 可选.
func irFromDict(m map[string]any) (*RenderIR, error) {
	if err := rejectUnknownKeys(m, map[string]bool{
		"item_version_id": true, "item_id": true, "interaction_id": true,
		"item_number": true, "placement_token": true, "item_short_code": true,
		"blocks": true, "layout_hints": true,
	}); err != nil {
		return nil, err
	}
	ivID, _ := m["item_version_id"].(string)
	itemID, _ := m["item_id"].(string)
	interactionID, _ := m["interaction_id"].(string)
	if ivID == "" || itemID == "" || interactionID == "" {
		return nil, fmt.Errorf("%w: 题组子题 IR dict 缺 item_version_id/item_id/interaction_id", ErrInvalidItemVersion)
	}
	rawBlocks, _ := m["blocks"].([]any)
	blocks := make([]Block, 0, len(rawBlocks))
	for i, rb := range rawBlocks {
		raw, ok := rb.(map[string]any)
		if !ok {
			return nil, fmt.Errorf("%w: IR dict blocks[%d] 必须是 dict，得到 %T", ErrInvalidItemVersion, i, rb)
		}
		b, err := convertBlock(raw, interactionID, true)
		if err != nil {
			return nil, err
		}
		blocks = append(blocks, b)
	}
	hints, err := parseLayoutHints(m["layout_hints"])
	if err != nil {
		return nil, err
	}
	return &RenderIR{
		ItemVersionID:  ivID,
		ItemID:         itemID,
		InteractionID:  interactionID,
		ItemNumber:     stringField(m, "item_number"),
		PlacementToken: stringField(m, "placement_token"),
		ItemShortCode:  stringField(m, "item_short_code"),
		Blocks:         blocks,
		LayoutHints:    hints,
	}, nil
}

// blockKnownKeys 按 block 的 type 返回已知键集（extra=forbid 的白名单面）.
func blockKnownKeys(raw map[string]any) map[string]bool {
	blockType, _ := raw["type"].(string)
	known := map[string]bool{"type": true}
	switch BlockType(blockType) {
	case BlockText:
		known["value"] = true
	case BlockFill:
		known["blank_id"], known["kind"], known["unit"], known["width"] = true, true, true, true
	case BlockChoice:
		known["mode"], known["options"] = true, true
	case BlockMathSVG:
		known["svg"], known["caption"] = true, true
	case BlockGroup:
		known["material"], known["items"] = true, true
	}
	return known
}

// rejectUnknownKeys 拒绝白名单外的键（pydantic extra=forbid 的 Go 形态）.
func rejectUnknownKeys(m map[string]any, known map[string]bool) error {
	keys := make([]string, 0, len(m))
	for k := range m {
		if !known[k] {
			keys = append(keys, k)
		}
	}
	if len(keys) == 0 {
		return nil
	}
	sort.Strings(keys)
	return fmt.Errorf("%w: 含未知键 %v（extra=forbid）", ErrInvalidItemVersion, keys)
}

// parseLayoutHints 解析版式提示（content.layout_hints；非 dict 或缺省 →
// 默认值 PreferredColumns=1）.
func parseLayoutHints(raw any) (LayoutHints, error) {
	m, ok := raw.(map[string]any)
	if !ok {
		return LayoutHints{PreferredColumns: 1}, nil
	}
	pbb, err := boolField(m, "page_break_before", false)
	if err != nil {
		return LayoutHints{}, err
	}
	kwn, err := boolField(m, "keep_with_next", false)
	if err != nil {
		return LayoutHints{}, err
	}
	cols, err := intField(m, "preferred_columns", 1)
	if err != nil {
		return LayoutHints{}, err
	}
	return LayoutHints{PageBreakBefore: pbb, KeepWithNext: kwn, PreferredColumns: cols}, nil
}

// boolField 取布尔字段（缺省 default）。拒绝非 bool：冻结实现 bool() 真值化
// 会把 "false" 静默当 true（陷阱），Go 侧 fail-closed.
func boolField(m map[string]any, key string, def bool) (bool, error) {
	v, present := m[key]
	if !present || v == nil {
		return def, nil
	}
	b, ok := v.(bool)
	if !ok {
		return false, fmt.Errorf("%w: %s 必须是布尔，得到 %T", ErrInvalidItemVersion, key, v)
	}
	return b, nil
}

// intField 取整数字段（缺省 default）。接受 int 族/整值 float64/json.Number/
// 数字字符串（Python int() 的收敛面）；拒绝小数截断（int(8.5)==8 的静默陷阱）.
func intField(m map[string]any, key string, def int) (int, error) {
	v, present := m[key]
	if !present || v == nil {
		return def, nil
	}
	switch x := v.(type) {
	case int:
		return x, nil
	case int32:
		return int(x), nil
	case int64:
		return int(x), nil
	case float64:
		if x != float64(int(x)) {
			return 0, fmt.Errorf("%w: %s 必须是整数，得到 %v（拒绝小数截断）", ErrInvalidItemVersion, key, x)
		}
		return int(x), nil
	case json.Number:
		n, err := x.Int64()
		if err != nil {
			return 0, fmt.Errorf("%w: %s 必须是整数，得到 %s", ErrInvalidItemVersion, key, x.String())
		}
		return int(n), nil
	case string:
		n, err := strconv.Atoi(x)
		if err != nil {
			return 0, fmt.Errorf("%w: %s 必须是整数，得到 %q", ErrInvalidItemVersion, key, x)
		}
		return n, nil
	default:
		return 0, fmt.Errorf("%w: %s 必须是整数，得到 %T", ErrInvalidItemVersion, key, v)
	}
}
