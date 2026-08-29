package render

import (
	"strings"
	"testing"
)

func lowBandFixture(t *testing.T) RenderIR {
	t.Helper()
	return *mustIR(t, `{
		"item_version_id": "iv-l", "item_id": "item-l",
		"interaction_ref": {"interaction_id": "numeric_blank"},
		"content": {"blocks": [{"type": "text", "value": "小鸟飞翔"}]}
	}`, ItemToIRInput{})
}

// 地面真值：低段注音烘焙产物与 html_hints 全字段取自冻结 Python 实现
// （gradeband_adapter.py + phonetic_overlay.py）的真实运行输出.
func TestAdaptForGradebandLowBand(t *testing.T) {
	ir := lowBandFixture(t)
	adapt := AdaptForGradeband(ir, "L", &Hints{
		Phonetic:        true,
		FontSize:        "24px",
		ReadAloud:       true,
		Keyboard:        "numeric",
		KeyboardAllowed: "0123456789",
	}, map[string]string{"小": "xiǎo", "鸟": "niǎo"})

	if !adapt.PhoneticApplied {
		t.Fatal("低段 + 注音 + 有字典应 applied")
	}
	// 注音烘焙进 text 块（渲染器无感知学段差异）
	wantText := "<ruby>小<rp>(</rp><rt>xiǎo</rt><rp>)</rp></ruby>" +
		"<ruby>鸟<rp>(</rp><rt>niǎo</rt><rp>)</rp></ruby>飞翔"
	tb, ok := adapt.IR.Blocks[0].(TextBlock)
	if !ok || tb.Value != wantText {
		t.Fatalf("注音烘焙错:\n got: %q\nwant: %q", tb.Value, wantText)
	}
	// html_hints 全字段
	h := adapt.HTMLHints
	if h.GradeBand != "L" || !h.Phonetic || h.PhoneticCoverage != "" || h.FontSize != "24px" ||
		h.FontClass != LowBandFontClass || !h.ReadAloud || h.ReadAloudAttr != ReadAloudDataAttr ||
		h.Keyboard != "numeric" || h.KeyboardAllowed != "0123456789" ||
		h.KeyboardAttr != NumericKeyboardDataAttr || h.PhoneticAttr != PhoneticDataAttr {
		t.Fatalf("html_hints 错: %+v", h)
	}
	// 适配后 IR 仍可走 HTML 渲染器，输出与冻结实现一致（ruby 串经转义）
	got, err := RenderItem(&adapt.IR)
	if err != nil {
		t.Fatalf("RenderItem: %v", err)
	}
	wantEscaped := "<p class=\"item-text\">&lt;ruby&gt;小&lt;rp&gt;(&lt;/rp&gt;&lt;rt&gt;xiǎo&lt;/rt&gt;&lt;rp&gt;)&lt;/rp&gt;&lt;/ruby&gt;&lt;ruby&gt;鸟&lt;rp&gt;(&lt;/rp&gt;&lt;rt&gt;niǎo&lt;/rt&gt;&lt;rp&gt;)&lt;/rp&gt;&lt;/ruby&gt;飞翔</p>"
	if !strings.Contains(got, wantEscaped) {
		t.Fatalf("低段渲染与冻结实现不一致:\n got: %s", got)
	}
}

// 验收 #3：中/高段不注入低段专属元素.
func TestAdaptForGradebandMidBand(t *testing.T) {
	ir := lowBandFixture(t)
	adapt := AdaptForGradeband(ir, "M", &Hints{Phonetic: false}, map[string]string{"小": "xiǎo"})
	if adapt.PhoneticApplied {
		t.Fatal("中段不应注入注音")
	}
	h := adapt.HTMLHints
	if h.Phonetic || h.FontClass != "" || h.ReadAloudAttr != "" || h.KeyboardAttr != "" || h.PhoneticAttr != "" {
		t.Fatalf("中段 hints 应全空: %+v", h)
	}
	if tb := adapt.IR.Blocks[0].(TextBlock); tb.Value != "小鸟飞翔" {
		t.Fatalf("中段文本应原样: %q", tb.Value)
	}
	// 高段同判据
	adaptH := AdaptForGradeband(ir, "H", nil, nil)
	if adaptH.HTMLHints.KeyboardAttr != "" || adaptH.PhoneticApplied {
		t.Fatalf("高段不应注入: %+v", adaptH.HTMLHints)
	}
}

// 键盘触发只对数值类交互生效（核心域交互类型分类，非学段包特判）.
func TestAdaptForGradebandKeyboardGate(t *testing.T) {
	choiceIR := *mustIR(t, `{
		"item_version_id": "iv-c", "item_id": "i",
		"interaction_ref": {"interaction_id": "single_choice"},
		"content": {"blocks": []}
	}`, ItemToIRInput{})
	adapt := AdaptForGradeband(choiceIR, "L", &Hints{Keyboard: "numeric"}, nil)
	if adapt.HTMLHints.KeyboardAttr != "" {
		t.Fatalf("选择题不应触发数字键盘: %+v", adapt.HTMLHints)
	}
	for _, interaction := range []string{"numeric_blank", "text_blank_numeric"} {
		ir := *mustIR(t, `{
			"item_version_id": "iv-k", "item_id": "i",
			"interaction_ref": {"interaction_id": "`+interaction+`"},
			"content": {"blocks": []}
		}`, ItemToIRInput{})
		adapt := AdaptForGradeband(ir, "L", &Hints{Keyboard: "numeric"}, nil)
		if adapt.HTMLHints.KeyboardAttr != NumericKeyboardDataAttr {
			t.Fatalf("%s 应触发数字键盘: %+v", interaction, adapt.HTMLHints)
		}
	}
}

// IR 不可变：适配不改入参；题组共享素材不注音（语篇由学科组件处理）.
func TestAdaptForGradebandImmutableAndGroup(t *testing.T) {
	ir := lowBandFixture(t)
	orig := ir.Blocks[0].(TextBlock).Value
	phoneticMap := map[string]string{"小": "xiǎo", "鸟": "niǎo", "飞": "fēi", "翔": "xiáng"}
	adapt := AdaptForGradeband(ir, "L", &Hints{Phonetic: true}, phoneticMap)
	if ir.Blocks[0].(TextBlock).Value != orig {
		t.Fatal("入参 IR 被修改（不可变纪律破坏）")
	}
	if adapt.IR.Blocks[0].(TextBlock).Value == orig {
		t.Fatal("适配后 IR 应含注音")
	}

	groupIR := *mustIR(t, `{
		"item_version_id": "iv-g", "item_id": "i",
		"interaction_ref": {"interaction_id": "single_choice"},
		"content": {"blocks": [{"type": "group", "material": "小鸟素材", "items": [
			{"item_version_id": "s", "item_id": "s", "interaction_id": "single_choice",
			 "blocks": [{"type": "text", "value": "小鸟子题"}]}
		]}]}
	}`, ItemToIRInput{})
	gadapt := AdaptForGradeband(groupIR, "L", &Hints{Phonetic: true}, phoneticMap)
	gb := gadapt.IR.Blocks[0].(GroupBlock)
	if gb.Material != "小鸟素材" {
		t.Fatalf("共享素材不应注音: %q", gb.Material)
	}
	subText := gb.Items[0].Blocks[0].(TextBlock).Value
	if !strings.Contains(subText, "<ruby>小") {
		t.Fatalf("子题文本应注音: %q", subText)
	}
}

// 注音覆盖组件（phonetic_overlay.py 地面真值）.
func TestApplyPhoneticToText(t *testing.T) {
	// 有 map：命中字包裹 ruby，未命中字转义原样
	want := "<ruby>a<rp>(</rp><rt>ē</rt><rp>)</rp></ruby>&lt;b&amp;&#x27;&quot;"
	if got := ApplyPhoneticToText("a<b&'\"", map[string]string{"a": "ē"}); got != want {
		t.Fatalf("注音转义错:\n got: %s\nwant: %s", got, want)
	}
	// 无 map：仅转义（安全契约）
	if got := ApplyPhoneticToText("a<b", nil); got != "a&lt;b" {
		t.Fatalf("无字典应仅转义: %q", got)
	}
	if got := ApplyPhoneticToText("a<b", map[string]string{}); got != "a&lt;b" {
		t.Fatalf("空字典应仅转义: %q", got)
	}

	if !HasPhoneticCoverage("小鸟", map[string]string{"鸟": "niǎo"}) {
		t.Fatal("含命中字应有覆盖")
	}
	if HasPhoneticCoverage("小鸟", map[string]string{"猫": "māo"}) {
		t.Fatal("无命中字不应有覆盖")
	}
	if HasPhoneticCoverage("", map[string]string{"鸟": "niǎo"}) {
		t.Fatal("空文本不应有覆盖")
	}
	if HasPhoneticCoverage("小鸟", nil) {
		t.Fatal("空字典不应有覆盖")
	}
}
