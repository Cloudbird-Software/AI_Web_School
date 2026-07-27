"""T-W2-033 Render IR v1 单元测试.

覆盖验收标准：
1. ir.py 定义 RenderIR Pydantic schema：blocks + layout_hints
2. item_to_ir.py 将 ItemVersion.content 转换为 RenderIR
3. 单元测试覆盖单选、文本填空、数学 SVG、题组四种结构
4. 不 import 任何学科包/学段包（A5 边界扫描）
"""
from __future__ import annotations

import inspect

import pytest
from pydantic import ValidationError

from src.core.render.ir import (
    ChoiceBlock,
    FillBlock,
    GroupBlock,
    LayoutHints,
    MathSvgBlock,
    OptionItem,
    RenderIR,
    TextBlock,
)
from src.core.render.item_to_ir import item_to_ir


# ════════════════════════════════════════════════════════════════════
# 辅助：构造最小 ItemVersion dict
# ════════════════════════════════════════════════════════════════════

def _make_iv(
    interaction_id: str,
    blocks: list[dict],
    *,
    item_version_id: str = "iv-001",
    item_id: str = "item-001",
    layout_hints: dict | None = None,
) -> dict:
    """构造最小 ItemVersion dict（仅含 item_to_ir 需要的字段）."""
    content: dict = {"blocks": blocks}
    if layout_hints is not None:
        content["layout_hints"] = layout_hints
    return {
        "item_version_id": item_version_id,
        "item_id": item_id,
        "interaction_ref": {"interaction_id": interaction_id, "interaction_params": {}},
        "content": content,
    }


# ════════════════════════════════════════════════════════════════════
# 1. IR schema 基础
# ════════════════════════════════════════════════════════════════════

class TestRenderIRSchema:
    def test_text_block_minimal(self):
        b = TextBlock(value="hello")
        assert b.type == "text"
        assert b.value == "hello"

    def test_fill_block_requires_blank_id(self):
        with pytest.raises(ValidationError):
            FillBlock(kind="text")  # type: ignore[call-arg]

    def test_fill_block_kind_numeric_with_unit(self):
        b = FillBlock(blank_id="b1", kind="numeric", unit="cm")
        assert b.unit == "cm"
        assert b.width == 0

    def test_choice_block_options(self):
        b = ChoiceBlock(
            mode="single",
            options=[OptionItem(id="A", label="选项 A")],
        )
        assert len(b.options) == 1
        assert b.options[0].id == "A"

    def test_math_svg_block(self):
        b = MathSvgBlock(svg="<svg></svg>", caption="数轴")
        assert b.svg == "<svg></svg>"
        assert b.caption == "数轴"

    def test_layout_hints_defaults(self):
        h = LayoutHints()
        assert h.page_break_before is False
        assert h.keep_with_next is False
        assert h.preferred_columns == 1

    def test_renderir_rejects_extra_fields(self):
        with pytest.raises(ValidationError):
            RenderIR(
                item_version_id="iv",
                item_id="item",
                interaction_id="single_choice",
                blocks=[],
                bogus_field=True,  # type: ignore[call-arg]
            )


# ════════════════════════════════════════════════════════════════════
# 2. item_to_ir：四种结构覆盖（验收标准 #3）
# ════════════════════════════════════════════════════════════════════

class TestItemToIrSingleChoice:
    """验收标准 #3：单选结构."""

    def test_single_choice_basic(self):
        iv = _make_iv(
            "single_choice",
            [
                {"type": "text", "value": "1 + 1 = ?"},
                {
                    "type": "choice",
                    "options": [
                        {"id": "A", "label": "1"},
                        {"id": "B", "label": "2"},
                    ],
                },
            ],
        )
        ir = item_to_ir(iv, item_number="1")
        assert ir.item_version_id == "iv-001"
        assert ir.item_id == "item-001"
        assert ir.interaction_id == "single_choice"
        assert ir.item_number == "1"
        assert len(ir.blocks) == 2
        # 文本块
        assert isinstance(ir.blocks[0], TextBlock)
        assert ir.blocks[0].value == "1 + 1 = ?"
        # 选择块：mode 由 interaction_id 推导为 single
        assert isinstance(ir.blocks[1], ChoiceBlock)
        assert ir.blocks[1].mode == "single"
        assert [o.id for o in ir.blocks[1].options] == ["A", "B"]
        assert [o.label for o in ir.blocks[1].options] == ["1", "2"]

    def test_single_choice_mode_explicit_overrides(self):
        """block 显式声明 mode 优先于 interaction_id 推导."""
        iv = _make_iv(
            "single_choice",
            [{"type": "choice", "mode": "multi", "options": []}],
        )
        ir = item_to_ir(iv)
        assert ir.blocks[0].mode == "multi"  # type: ignore[union-attr]


class TestItemToIrMultiChoice:
    def test_multi_choice_mode_inferred(self):
        iv = _make_iv(
            "multi_choice",
            [
                {"type": "text", "value": "下列哪些是质数？"},
                {
                    "type": "choice",
                    "options": [
                        {"id": "A", "label": "2"},
                        {"id": "B", "label": "3"},
                        {"id": "C", "label": "4"},
                    ],
                },
            ],
        )
        ir = item_to_ir(iv)
        assert isinstance(ir.blocks[1], ChoiceBlock)
        assert ir.blocks[1].mode == "multi"
        assert len(ir.blocks[1].options) == 3


class TestItemToIrTextBlank:
    """验收标准 #3：文本填空结构."""

    def test_text_blank_kind_inferred(self):
        iv = _make_iv(
            "text_blank",
            [
                {"type": "text", "value": "首都是"},
                {"type": "fill", "blank_id": "b1"},
                {"type": "text", "value": "。"},
            ],
        )
        ir = item_to_ir(iv)
        assert len(ir.blocks) == 3
        assert isinstance(ir.blocks[1], FillBlock)
        assert ir.blocks[1].blank_id == "b1"
        assert ir.blocks[1].kind == "text"  # 由 interaction_id 推导
        assert ir.blocks[1].unit is None
        assert ir.blocks[1].width == 0


class TestItemToIrNumericBlank:
    def test_numeric_blank_with_unit(self):
        iv = _make_iv(
            "numeric_blank",
            [
                {"type": "text", "value": "长方形长 3 cm 宽 2 cm，面积="},
                {"type": "fill", "blank_id": "b1", "unit": "cm²", "width": 4},
            ],
        )
        ir = item_to_ir(iv)
        assert isinstance(ir.blocks[1], FillBlock)
        assert ir.blocks[1].kind == "numeric"
        assert ir.blocks[1].unit == "cm²"
        assert ir.blocks[1].width == 4


class TestItemToIrMathSvg:
    """验收标准 #3：数学 SVG 结构."""

    def test_math_svg_block_passthrough(self):
        svg_str = '<svg xmlns="http://www.w3.org/2000/svg" width="200"><line x1="0" y1="50" x2="200" y2="50"/></svg>'
        iv = _make_iv(
            "single_choice",
            [
                {"type": "text", "value": "在数轴上标出 1/2："},
                {"type": "math_svg", "svg": svg_str, "caption": "0 到 1 数轴"},
                {"type": "choice", "options": [{"id": "A", "label": "中点"}]},
            ],
        )
        ir = item_to_ir(iv)
        assert len(ir.blocks) == 3
        assert isinstance(ir.blocks[1], MathSvgBlock)
        assert ir.blocks[1].svg == svg_str
        assert ir.blocks[1].caption == "0 到 1 数轴"


class TestItemToIrGroup:
    """验收标准 #3：题组结构（一材多题，嵌套子题）."""

    def test_group_with_two_subitems(self):
        iv = _make_iv(
            "single_choice",  # 题组父题的 interaction_id（题组本身无作答）
            [
                {"type": "text", "value": "阅读下文，回答 1-2 题。"},
                {
                    "type": "group",
                    "material": "某语篇正文…",
                    "items": [
                        _make_iv(
                            "single_choice",
                            [
                                {"type": "text", "value": "第 1 题"},
                                {
                                    "type": "choice",
                                    "options": [{"id": "A", "label": "x"}],
                                },
                            ],
                            item_version_id="iv-sub1",
                            item_id="item-sub1",
                        ),
                        _make_iv(
                            "text_blank",
                            [
                                {"type": "text", "value": "第 2 题填空"},
                                {"type": "fill", "blank_id": "b1"},
                            ],
                            item_version_id="iv-sub2",
                            item_id="item-sub2",
                        ),
                    ],
                },
            ],
        )
        ir = item_to_ir(iv)
        assert len(ir.blocks) == 2
        group_block = ir.blocks[1]
        assert isinstance(group_block, GroupBlock)
        assert group_block.material == "某语篇正文…"
        assert len(group_block.items) == 2
        # 子题 1：单选
        sub1 = group_block.items[0]
        assert sub1.item_version_id == "iv-sub1"
        assert sub1.interaction_id == "single_choice"
        assert isinstance(sub1.blocks[1], ChoiceBlock)
        assert sub1.blocks[1].mode == "single"
        # 子题 2：填空
        sub2 = group_block.items[1]
        assert sub2.interaction_id == "text_blank"
        assert isinstance(sub2.blocks[1], FillBlock)
        assert sub2.blocks[1].kind == "text"

    def test_group_missing_subitem_fields_raises(self):
        iv = _make_iv(
            "single_choice",
            [
                {
                    "type": "group",
                    "material": None,
                    "items": [{"type": "text", "value": "缺 item_version_id"}],
                },
            ],
        )
        with pytest.raises(ValueError, match="题组子题缺少必要字段"):
            item_to_ir(iv)


# ════════════════════════════════════════════════════════════════════
# 3. layout_hints 与异常路径
# ════════════════════════════════════════════════════════════════════

class TestLayoutHintsAndErrors:
    def test_layout_hints_from_content(self):
        iv = _make_iv(
            "single_choice",
            [{"type": "text", "value": "大题"}],
            layout_hints={"page_break_before": True, "preferred_columns": 2},
        )
        ir = item_to_ir(iv)
        assert ir.layout_hints.page_break_before is True
        assert ir.layout_hints.keep_with_next is False
        assert ir.layout_hints.preferred_columns == 2

    def test_layout_hints_default_when_absent(self):
        iv = _make_iv("single_choice", [{"type": "text", "value": "x"}])
        ir = item_to_ir(iv)
        assert ir.layout_hints.page_break_before is False
        assert ir.layout_hints.preferred_columns == 1

    def test_missing_interaction_id_raises(self):
        iv = {
            "item_version_id": "iv",
            "item_id": "item",
            "interaction_ref": {},
            "content": {"blocks": []},
        }
        with pytest.raises(ValueError, match="interaction_ref.interaction_id"):
            item_to_ir(iv)

    def test_unknown_block_type_raises(self):
        iv = _make_iv(
            "single_choice",
            [{"type": "bogus", "value": "x"}],
        )
        with pytest.raises(ValueError, match="未知 block type"):
            item_to_ir(iv)


# ════════════════════════════════════════════════════════════════════
# 4. IR 序列化（验收标准 #1：IR 序列化）
# ════════════════════════════════════════════════════════════════════

class TestIRSerialization:
    def test_roundtrip_single_choice(self):
        """RenderIR 序列化 → 反序列化保持等价."""
        ir = RenderIR(
            item_version_id="iv-1",
            item_id="item-1",
            interaction_id="single_choice",
            item_number="3",
            blocks=[
                TextBlock(value="题面"),
                ChoiceBlock(
                    mode="single",
                    options=[OptionItem(id="A", label="a"), OptionItem(id="B", label="b")],
                ),
            ],
            layout_hints=LayoutHints(keep_with_next=True),
        )
        data = ir.model_dump()
        ir2 = RenderIR.model_validate(data)
        assert ir2 == ir

    def test_roundtrip_group_nested(self):
        ir = RenderIR(
            item_version_id="iv-parent",
            item_id="item-parent",
            interaction_id="single_choice",
            blocks=[
                GroupBlock(
                    material="素材",
                    items=[
                        RenderIR(
                            item_version_id="iv-c1",
                            item_id="item-c1",
                            interaction_id="text_blank",
                            blocks=[
                                TextBlock(value="子题"),
                                FillBlock(blank_id="b1", kind="text"),
                            ],
                        ),
                    ],
                ),
            ],
        )
        data = ir.model_dump()
        ir2 = RenderIR.model_validate(data)
        assert ir2 == ir
        # 嵌套结构保留
        group = ir2.blocks[0]
        assert isinstance(group, GroupBlock)
        assert len(group.items) == 1
        assert isinstance(group.items[0].blocks[1], FillBlock)

    def test_json_serialization(self):
        """IR 可 JSON 序列化（用于 rendered_snapshot 物化）."""
        import json

        ir = RenderIR(
            item_version_id="iv",
            item_id="item",
            interaction_id="single_choice",
            blocks=[TextBlock(value="x")],
        )
        s = json.dumps(ir.model_dump(mode="json"), separators=(",", ":"))
        assert '"type":"text"' in s
        ir2 = RenderIR.model_validate(json.loads(s))
        assert ir2.blocks[0].value == "x"  # type: ignore[union-attr]


# ════════════════════════════════════════════════════════════════════
# 5. 学科零特判（验收标准 #4：不 import 学科包/学段包）
# ════════════════════════════════════════════════════════════════════

class TestNoSubjectPackImports:
    """宪法 A5：核心域禁止 import 学科包/学段包."""

    def test_ir_module_no_pack_imports(self):
        import src.core.render.ir as ir_mod

        src_text = inspect.getsource(ir_mod)
        # 禁止出现 src.packs / src.gradeband 等 import
        assert "src.packs" not in src_text
        assert "src.gradeband" not in src_text
        assert "from src.packs" not in src_text

    def test_item_to_ir_module_no_pack_imports(self):
        import src.core.render.item_to_ir as conv_mod

        src_text = inspect.getsource(conv_mod)
        assert "src.packs" not in src_text
        assert "src.gradeband" not in src_text

    def test_render_package_under_core(self):
        """render 模块位于 src/core/ 下，不在 src/packs/ 下."""
        import src.core.render as pkg

        pkg_dir = pkg.__path__[0]
        assert "src" in pkg_dir and "core" in pkg_dir and "render" in pkg_dir
        assert "packs" not in pkg_dir
