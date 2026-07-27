"""W3 遗留 S9-①：卷面印每题短码（placement_token + item_short_code）渲染测试.

覆盖：
  §1 RenderIR 新增可选字段 placement_token / item_short_code（默认 None，
     不改变既有输出——无卷上下文的单题渲染不输出追溯行）。
  §2 item_to_ir 透传两个字段。
  §3 render_item 在提供时输出 .item-trace 追溯行（含位置标识与短码），
     用户内容经 HTML 转义。
  §4 weekly_batch.run 组卷链路：卷面 HTML 印有每题短码与位置标识，
     且与 paper_item 行一致（扫码可回溯）。
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from src.core.render.html_renderer import render_item
from src.core.render.ir import RenderIR, TextBlock
from src.core.render.item_to_ir import item_to_ir
from src.core.render.weekly_batch import (
    WeeklyConstraints,
    WeeklyScope,
    run,
)


# ────────────────────────────────────────────────────────────────────
# 辅助
# ────────────────────────────────────────────────────────────────────


def _make_ir(**kwargs) -> RenderIR:
    """构造最小 RenderIR（text 块）."""
    defaults = dict(
        item_version_id="sha256:iv-test",
        item_id="item-test",
        interaction_id="single_choice",
        blocks=[TextBlock(value="1 + 1 = ?")],
    )
    defaults.update(kwargs)
    return RenderIR(**defaults)


def _fake_pdf_export(html: str, output_path: Path) -> Path:
    """伪 PDF 导出：写非空文件（避免依赖 Edge/playwright）."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(b"%PDF-1.4\nfake\n")
    return output_path


def _make_choice_item(item_version_id: str) -> dict:
    """构造单选 ItemVersion dict（weekly_batch pool 用）."""
    return {
        "item_version_id": item_version_id,
        "item_id": f"item-{item_version_id}",
        "interaction_ref": {"interaction_id": "single_choice"},
        "content": {
            "blocks": [
                {"type": "text", "value": "2 + 2 = ?"},
                {
                    "type": "choice",
                    "mode": "single",
                    "options": [
                        {"id": "A", "label": "3"},
                        {"id": "B", "label": "4"},
                    ],
                },
            ]
        },
        "scoring_ref": {"scorer_params": {"answer": "B"}},
    }


# ────────────────────────────────────────────────────────────────────
# §1 RenderIR 可选字段与默认行为
# ────────────────────────────────────────────────────────────────────


class TestRenderIRTraceFields:
    """RenderIR 新增 placement_token / item_short_code 可选字段."""

    def test_default_none(self) -> None:
        """两字段默认 None（无卷上下文的单题渲染保持原语义）."""
        ir = _make_ir()
        assert ir.placement_token is None
        assert ir.item_short_code is None

    def test_no_trace_line_when_absent(self) -> None:
        """缺省时渲染输出不含 item-trace（既有输出不变）."""
        html = render_item(_make_ir())
        assert "item-trace" not in html

    def test_extra_fields_still_forbidden(self) -> None:
        """extra='forbid' 仍然生效（新增字段是显式声明而非放开）."""
        with pytest.raises(Exception):
            RenderIR(
                item_version_id="x",
                item_id="y",
                interaction_id="single_choice",
                bogus_field="z",
            )


# ────────────────────────────────────────────────────────────────────
# §2 item_to_ir 透传
# ────────────────────────────────────────────────────────────────────


class TestItemToIRTracePassthrough:
    """item_to_ir 将 placement_token / item_short_code 透传进 IR."""

    def test_passthrough(self) -> None:
        iv = {
            "item_version_id": "iv-1",
            "item_id": "i-1",
            "interaction_ref": {"interaction_id": "single_choice"},
            "content": {"blocks": [{"type": "text", "value": "题面"}]},
        }
        ir = item_to_ir(
            iv, item_number="1", placement_token="q1", item_short_code="ABC1234"
        )
        assert ir.placement_token == "q1"
        assert ir.item_short_code == "ABC1234"

    def test_default_none(self) -> None:
        iv = {
            "item_version_id": "iv-1",
            "item_id": "i-1",
            "interaction_ref": {"interaction_id": "single_choice"},
            "content": {"blocks": [{"type": "text", "value": "题面"}]},
        }
        ir = item_to_ir(iv, item_number="1")
        assert ir.placement_token is None
        assert ir.item_short_code is None


# ────────────────────────────────────────────────────────────────────
# §3 render_item 追溯行
# ────────────────────────────────────────────────────────────────────


class TestRenderItemTraceLine:
    """render_item 在提供追溯字段时输出 .item-trace 行."""

    def test_trace_line_rendered(self) -> None:
        ir = _make_ir(placement_token="q1", item_short_code="ABC1234")
        html = render_item(ir)
        assert '<div class="item-trace">' in html
        assert '<span class="placement-token">q1</span>' in html
        assert '<span class="item-short-code">ABC1234</span>' in html

    def test_only_short_code(self) -> None:
        """只提供短码时只渲染短码."""
        html = render_item(_make_ir(item_short_code="ABC1234"))
        assert "item-short-code" in html
        assert "placement-token" not in html

    def test_only_placement_token(self) -> None:
        """只提供位置标识时只渲染位置标识."""
        html = render_item(_make_ir(placement_token="q2.sub1"))
        assert "placement-token" in html
        assert "q2.sub1" in html
        assert "item-short-code" not in html

    def test_trace_line_escaped(self) -> None:
        """追溯字段内容经 HTML 转义（防注入）."""
        html = render_item(_make_ir(item_short_code='<script>alert(1)</script>'))
        assert "<script>" not in html
        assert "&lt;script&gt;" in html


# ────────────────────────────────────────────────────────────────────
# §4 weekly_batch 组卷链路：卷面印短码
# ────────────────────────────────────────────────────────────────────


class TestWeeklyBatchPrintsShortCode:
    """组卷批处理：卷面 HTML 印有每题 placement_token 与 item_short_code."""

    def test_paper_html_contains_short_codes(self, tmp_path: Path) -> None:
        scope = WeeklyScope(
            subject_pack_id="subject-math",
            gradeband="M",
            kp_codes=("math.nal.decimal.compare",),
            kp_snapshot_ref="snap-test",
        )
        constraints = WeeklyConstraints(
            num_items=2,
            interaction_distribution={"single_choice": 2},
            seed=7,
        )
        pool = [_make_choice_item("iv-a"), _make_choice_item("iv-b")]
        captured: list[str] = []

        def _capture(html: str, output_path: Path) -> Path:
            captured.append(html)
            return _fake_pdf_export(html, output_path)

        with patch(
            "src.core.render.weekly_batch.PdfExporter.export",
            side_effect=_capture,
        ):
            result = run(scope, constraints, tmp_path, item_version_pool=pool)

        # captured[0] = 试卷 HTML；captured[1] = 解析册 HTML
        paper_html = captured[0]
        for row in result.paper_item_rows:
            # 卷面印有该题位置标识与短码（扫码查源入口）
            assert row["item_short_code"] in paper_html
            assert f'class="item-trace"' in paper_html
        # 位置标识 q1/q2 出现在卷面
        tokens = [r["placement_token"] for r in result.paper_item_rows]
        assert "q1" in tokens and "q2" in tokens
        for token in tokens:
            assert f'<span class="placement-token">{token}</span>' in paper_html
