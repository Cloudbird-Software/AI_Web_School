"""T-W4-037 渲染学段适配层单元测试.

验收标准逐条覆盖：
1. adapt_for_gradeband(render_ir, grade_band) 返回适配后 IR + 学段专属标记/样式提示。
2. 低段渲染输出含：<ruby> 注音标签、朗读按钮 data 属性、大字号 CSS 类、数字键盘触发标记。
3. 中段/高段渲染不注入低段专属元素，保持常规呈现。
5. 不 import 学科包；学段参数通过 grade_band 配置注入（hints dict）。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from src.core.render.gradeband_adapter import (
    LOW_BAND_FONT_CLASS,
    NUMERIC_KEYBOARD_DATA_ATTR,
    PHONETIC_DATA_ATTR,
    READ_ALOUD_DATA_ATTR,
    GradeBandAdaptation,
    adapt_for_gradeband,
)
from src.core.render.components.phonetic_overlay import (
    apply_phonetic_to_text,
    has_phonetic_coverage,
)
from src.core.render.ir import (
    ChoiceBlock,
    FillBlock,
    LayoutHints,
    MathSvgBlock,
    OptionItem,
    RenderIR,
    TextBlock,
)

# ════════════════════════════════════════════════════════════════════
# 测试夹具
# ════════════════════════════════════════════════════════════════════


def _make_text_ir(
    text: str = "小鸟飞翔",
    interaction_id: str = "single_choice",
) -> RenderIR:
    """构造单文本块 IR."""
    return RenderIR(
        item_version_id="iv-1",
        item_id="i-1",
        interaction_id=interaction_id,
        item_number="1",
        blocks=[TextBlock(value=text)],
    )


def _make_numeric_ir() -> RenderIR:
    """构造数值填空 IR（触发数字键盘）."""
    return RenderIR(
        item_version_id="iv-2",
        item_id="i-2",
        interaction_id="numeric_blank",
        item_number="2",
        blocks=[
            TextBlock(value="填一填："),
            FillBlock(blank_id="b1", kind="numeric"),
        ],
    )


_LOW_HINTS = {
    "grade_band": "L",
    "phonetic": True,
    "phonetic_coverage": "full",
    "font_size": "24px",
    "read_aloud": True,
    "keyboard": "numeric",
    "keyboard_allowed": "0123456789",
}

_MID_HINTS = {
    "grade_band": "M",
    "phonetic": False,
    "font_size": None,
    "read_aloud": False,
    "keyboard": None,
    "keyboard_allowed": None,
}

_PHONETIC_MAP = {"小": "xiǎo", "鸟": "nǐao", "飞": "fēi", "翔": "xiáng"}


# ════════════════════════════════════════════════════════════════════
# 验收 #1：adapt_for_gradeband 返回适配后 IR + 学段专属标记/样式提示
# ════════════════════════════════════════════════════════════════════


def test_adapt_for_gradeband_returns_adaptation_dataclass():
    """返回 GradeBandAdaptation 含 ir / html_hints / grade_band."""
    ir = _make_text_ir()
    result = adapt_for_gradeband(ir, "L", hints=_LOW_HINTS, phonetic_map=_PHONETIC_MAP)
    assert isinstance(result, GradeBandAdaptation)
    assert result.grade_band == "L"
    assert isinstance(result.ir, RenderIR)
    assert isinstance(result.html_hints, dict)


def test_adapt_for_gradeband_preserves_ir_metadata():
    """适配后 IR 的 item_version_id / item_id / interaction_id 不变."""
    ir = _make_text_ir()
    result = adapt_for_gradeband(ir, "L", hints=_LOW_HINTS, phonetic_map=_PHONETIC_MAP)
    assert result.ir.item_version_id == "iv-1"
    assert result.ir.item_id == "i-1"
    assert result.ir.interaction_id == "single_choice"
    assert result.ir.item_number == "1"


def test_adapt_for_gradeband_does_not_mutate_input_ir():
    """适配层不修改入参 IR（不可变契约）."""
    ir = _make_text_ir("小鸟飞翔")
    original_text = ir.blocks[0].value
    adapt_for_gradeband(ir, "L", hints=_LOW_HINTS, phonetic_map=_PHONETIC_MAP)
    assert ir.blocks[0].value == original_text  # 入参未变


def test_adapt_for_gradeband_html_hints_has_grade_band_field():
    """html_hints 含 grade_band 字段."""
    result = adapt_for_gradeband(_make_text_ir(), "L", hints=_LOW_HINTS)
    assert result.html_hints["grade_band"] == "L"


def test_adapt_for_gradeband_without_hints_uses_defaults():
    """未提供 hints 时使用核心默认（不注入学段元素）."""
    result = adapt_for_gradeband(_make_text_ir(), "L")
    assert result.html_hints["phonetic"] is False
    assert result.html_hints["font_size"] is None
    assert result.html_hints["read_aloud"] is False
    assert result.html_hints["keyboard"] is None


# ════════════════════════════════════════════════════════════════════
# 验收 #2：低段渲染输出含 <ruby> 注音 / 朗读按钮 data 属性 / 大字号 CSS 类 / 数字键盘标记
# ════════════════════════════════════════════════════════════════════


def test_low_band_phonetic_bakes_ruby_into_text_block():
    """低段注音烘焙进 text.value，输出含 <ruby> 标签（验收 #2）."""
    ir = _make_text_ir("小鸟飞翔")
    result = adapt_for_gradeband(ir, "L", hints=_LOW_HINTS, phonetic_map=_PHONETIC_MAP)
    text_value = result.ir.blocks[0].value
    assert "<ruby>" in text_value
    assert "<rt>xiǎo</rt>" in text_value
    assert "<rt>nǐao</rt>" in text_value
    assert result.phonetic_applied is True


def test_low_band_phonetic_uses_rp_for_fallback():
    """<ruby> 含 <rp> 提供不支持 ruby 浏览器的回退显示（HTML 标准）."""
    ir = _make_text_ir("小")
    result = adapt_for_gradeband(ir, "L", hints=_LOW_HINTS, phonetic_map={"小": "xiǎo"})
    text_value = result.ir.blocks[0].value
    assert "<rp>(</rp>" in text_value
    assert "<rp>)</rp>" in text_value


def test_low_band_read_aloud_data_attribute_in_hints():
    """低段 html_hints 含朗读按钮 data 属性（验收 #2）."""
    result = adapt_for_gradeband(_make_text_ir(), "L", hints=_LOW_HINTS)
    assert result.html_hints["read_aloud"] is True
    assert result.html_hints["read_aloud_attr"] == READ_ALOUD_DATA_ATTR


def test_low_band_font_class_in_hints():
    """低段 html_hints 含大字号 CSS 类（验收 #2）."""
    result = adapt_for_gradeband(_make_text_ir(), "L", hints=_LOW_HINTS)
    assert result.html_hints["font_size"] == "24px"
    assert result.html_hints["font_class"] == LOW_BAND_FONT_CLASS


def test_low_band_numeric_keyboard_marker_for_numeric_interaction():
    """低段数值填空 IR 触发数字键盘标记（验收 #2）."""
    ir = _make_numeric_ir()
    result = adapt_for_gradeband(ir, "L", hints=_LOW_HINTS)
    assert result.html_hints["keyboard"] == "numeric"
    assert result.html_hints["keyboard_attr"] == NUMERIC_KEYBOARD_DATA_ATTR
    assert result.html_hints["keyboard_allowed"] == "0123456789"


def test_low_band_phonetic_attr_in_hints_when_phonetic_enabled():
    """低段 phonetic=True 时 html_hints 含注音 data 属性."""
    result = adapt_for_gradeband(_make_text_ir(), "L", hints=_LOW_HINTS)
    assert result.html_hints["phonetic"] is True
    assert result.html_hints["phonetic_attr"] == PHONETIC_DATA_ATTR


def test_low_band_no_phonetic_map_skips_baking():
    """低段 phonetic=True 但未提供 phonetic_map → 不烘焙注音（仅出标记）."""
    ir = _make_text_ir("小鸟飞翔")
    result = adapt_for_gradeband(ir, "L", hints=_LOW_HINTS, phonetic_map=None)
    # text.value 保持原文（未烘焙注音）
    assert result.ir.blocks[0].value == "小鸟飞翔"
    assert result.phonetic_applied is False
    # 但 html_hints 仍标注 phonetic=True（前端可自行按 data 属性注音）
    assert result.html_hints["phonetic"] is True


def test_low_band_numeric_keyboard_not_triggered_for_non_numeric():
    """低段非数值填空 IR 不触发数字键盘（数字键盘仅数值交互）."""
    ir = _make_text_ir(interaction_id="single_choice")
    result = adapt_for_gradeband(ir, "L", hints=_LOW_HINTS)
    assert result.html_hints["keyboard_attr"] is None


# ════════════════════════════════════════════════════════════════════
# 验收 #3：中段/高段不注入低段专属元素
# ════════════════════════════════════════════════════════════════════


def test_mid_band_does_not_inject_low_band_elements():
    """中段不注入注音/大字号/朗读/数字键盘（验收 #3）."""
    ir = _make_text_ir("小鸟飞翔")
    result = adapt_for_gradeband(ir, "M", hints=_MID_HINTS, phonetic_map=_PHONETIC_MAP)
    assert result.html_hints["phonetic"] is False
    assert result.html_hints["font_size"] is None
    assert result.html_hints["font_class"] is None
    assert result.html_hints["read_aloud"] is False
    assert result.html_hints["read_aloud_attr"] is None
    assert result.html_hints["keyboard"] is None
    assert result.html_hints["keyboard_attr"] is None
    assert result.html_hints["phonetic_attr"] is None


def test_high_band_does_not_inject_low_band_elements():
    """高段不注入低段专属元素."""
    ir = _make_text_ir("小鸟飞翔")
    result = adapt_for_gradeband(ir, "H", hints=_MID_HINTS, phonetic_map=_PHONETIC_MAP)
    assert result.html_hints["phonetic"] is False
    assert result.html_hints["font_size"] is None
    assert result.html_hints["read_aloud"] is False


def test_mid_band_text_not_phoneticized_even_with_map():
    """中段即使提供 phonetic_map 也不烘焙注音（学段差异化）."""
    ir = _make_text_ir("小鸟飞翔")
    result = adapt_for_gradeband(ir, "M", hints=_MID_HINTS, phonetic_map=_PHONETIC_MAP)
    assert result.ir.blocks[0].value == "小鸟飞翔"  # 原文未变
    assert result.phonetic_applied is False


def test_mid_band_numeric_interaction_no_keyboard():
    """中段数值填空 IR 不触发数字键盘（数字键盘仅低段）."""
    ir = _make_numeric_ir()
    result = adapt_for_gradeband(ir, "M", hints=_MID_HINTS)
    assert result.html_hints["keyboard_attr"] is None


def test_mid_band_ir_unchanged_without_phonetic():
    """中段适配后 IR 与入参一致（无任何 block 改动）."""
    ir = _make_text_ir("小鸟飞翔")
    result = adapt_for_gradeband(ir, "M", hints=_MID_HINTS)
    assert result.ir.blocks[0].value == "小鸟飞翔"
    # IR 实例可能不同（model_copy），但内容一致
    assert [b.value for b in result.ir.blocks] == [b.value for b in ir.blocks]


# ════════════════════════════════════════════════════════════════════
# 验收 #5：不 import 学科包/学段包；学段参数通过 hints 注入
# ════════════════════════════════════════════════════════════════════


def test_gradeband_adapter_module_does_not_import_packs():
    """gradeband_adapter.py 不 import 学科包/学段包（A5 静态实证）."""
    src = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "core"
        / "render"
        / "gradeband_adapter.py"
    ).read_text(encoding="utf-8")
    forbidden = (
        "packs.",
        "gradeband_low",
        "subject-math",
        "subject-chinese",
        "subject-english",
    )
    for needle in forbidden:
        assert needle not in src, f"gradeband_adapter.py 含禁用 import: {needle!r}"


def test_phonetic_overlay_module_does_not_import_packs():
    """phonetic_overlay.py 不 import 学科包/学段包（A5 静态实证）."""
    src = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "core"
        / "render"
        / "components"
        / "phonetic_overlay.py"
    ).read_text(encoding="utf-8")
    forbidden = (
        "packs.",
        "gradeband_low",
        "subject-math",
        "subject-chinese",
        "subject-english",
    )
    for needle in forbidden:
        assert needle not in src, f"phonetic_overlay.py 含禁用 import: {needle!r}"


def test_adapt_for_gradeband_consumes_hints_dict_not_pack_import():
    """学段参数通过 hints dict 注入（不通过 import 学段包）.

    场景：调用方（编排层）加载 gradeband_low.render_hints() 产出 hints dict，
    传给核心 adapt_for_gradeband——核心不感知 pack 模块位置。
    """
    # 模拟调用方加载学段包产出的 hints
    caller_loaded_hints = {
        "grade_band": "L",
        "phonetic": True,
        "phonetic_coverage": "full",
        "font_size": "24px",
        "read_aloud": True,
        "keyboard": "numeric",
        "keyboard_allowed": "0123456789",
    }
    result = adapt_for_gradeband(
        _make_text_ir(), "L", hints=caller_loaded_hints
    )
    # 核心按 hints dict 内容生效，不依赖任何 pack import
    assert result.html_hints["phonetic"] is True
    assert result.html_hints["font_size"] == "24px"


# ════════════════════════════════════════════════════════════════════
# phonetic_overlay 组件级测试
# ════════════════════════════════════════════════════════════════════


def test_apply_phonetic_to_text_returns_ruby_for_mapped_chars():
    """mapped 字符包裹 <ruby>，未映射字符原样输出."""
    out = apply_phonetic_to_text("小鸟飞", {"小": "xiǎo", "鸟": "nǐao"})
    assert "<ruby>小<rp>(</rp><rt>xiǎo</rt>" in out
    assert "<ruby>鸟<rp>(</rp><rt>nǐao</rt>" in out
    # "飞" 未在 map 中，原样输出（不在 <ruby> 内）
    assert "<ruby>飞" not in out
    assert "飞" in out


def test_apply_phonetic_to_text_empty_map_returns_escaped_text():
    """空 map 时仅做 HTML 转义（不注音）."""
    out = apply_phonetic_to_text("<script>", None)
    assert "&lt;script&gt;" == out


def test_apply_phonetic_to_text_escapes_pinyin():
    """拼音中的特殊字符经 HTML 转义（XSS 防护）."""
    out = apply_phonetic_to_text("x", {"x": "<evil>"})
    assert "&lt;evil&gt;" in out
    assert "<evil>" not in out


def test_apply_phonetic_to_text_handles_empty_string():
    """空文本返回空串."""
    assert apply_phonetic_to_text("", {"小": "xiǎo"}) == ""


def test_has_phonetic_coverage_detects_overlap():
    """has_phonetic_coverage 检测文本与 map 是否有覆盖."""
    assert has_phonetic_coverage("小鸟", {"小": "xiǎo"}) is True
    assert has_phonetic_coverage("大象", {"小": "xiǎo"}) is False
    assert has_phonetic_coverage("", {"小": "xiǎo"}) is False
    assert has_phonetic_coverage("小鸟", {}) is False


# ════════════════════════════════════════════════════════════════════
# 复合 IR 适配（题组嵌套 / 多块类型）
# ════════════════════════════════════════════════════════════════════


def test_adapt_phoneticizes_all_text_blocks_in_ir():
    """IR 含多个文本块时全部烘焙注音."""
    ir = RenderIR(
        item_version_id="iv",
        item_id="i",
        interaction_id="single_choice",
        blocks=[
            TextBlock(value="小鸟"),
            ChoiceBlock(
                mode="single",
                options=[OptionItem(id="A", label="bird")],
            ),
            TextBlock(value="飞翔"),
        ],
    )
    result = adapt_for_gradeband(ir, "L", hints=_LOW_HINTS, phonetic_map=_PHONETIC_MAP)
    # 两个文本块都烘焙了注音
    assert "<ruby>" in result.ir.blocks[0].value
    assert "<ruby>" in result.ir.blocks[2].value
    # 选项块未受影响
    assert isinstance(result.ir.blocks[1], ChoiceBlock)
    assert result.ir.blocks[1].options[0].label == "bird"


def test_adapt_recurses_into_group_block():
    """题组块递归适配子题 IR 的文本块."""
    from src.core.render.ir import GroupBlock
    sub_ir = RenderIR(
        item_version_id="iv-sub",
        item_id="i-sub",
        interaction_id="single_choice",
        blocks=[TextBlock(value="小鸟")],
    )
    ir = RenderIR(
        item_version_id="iv",
        item_id="i",
        interaction_id="group",
        blocks=[
            TextBlock(value="小鸟飞翔"),  # 含 map 字符，确保烘焙
            GroupBlock(material="阅读：", items=[sub_ir]),
        ],
    )
    result = adapt_for_gradeband(ir, "L", hints=_LOW_HINTS, phonetic_map=_PHONETIC_MAP)
    # 顶层文本块烘焙
    assert "<ruby>" in result.ir.blocks[0].value
    # 子题文本块也烘焙（递归）
    group_block = result.ir.blocks[1]
    assert isinstance(group_block, GroupBlock)
    sub_adapted = group_block.items[0]
    assert "<ruby>" in sub_adapted.blocks[0].value


def test_adapt_preserves_fill_block_kind():
    """填空块经适配后 kind 不变（numeric 仍是 numeric）."""
    ir = _make_numeric_ir()
    result = adapt_for_gradeband(ir, "L", hints=_LOW_HINTS, phonetic_map=_PHONETIC_MAP)
    # 第二块是 FillBlock
    fill = result.ir.blocks[1]
    assert isinstance(fill, FillBlock)
    assert fill.kind == "numeric"


def test_adapt_preserves_math_svg_block():
    """数学 SVG 块经适配后 svg 字符串原样（学段不修改学科产物）."""
    ir = RenderIR(
        item_version_id="iv",
        item_id="i",
        interaction_id="single_choice",
        blocks=[
            MathSvgBlock(svg="<svg></svg>", caption="图"),
        ],
    )
    result = adapt_for_gradeband(ir, "L", hints=_LOW_HINTS, phonetic_map=_PHONETIC_MAP)
    svg_block = result.ir.blocks[0]
    assert isinstance(svg_block, MathSvgBlock)
    assert svg_block.svg == "<svg></svg>"
    assert svg_block.caption == "图"
