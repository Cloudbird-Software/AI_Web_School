"""生成 T-W2-038 周更批处理样例 PDF.

用法：python generate_sample_pdf.py
输出：out/sample-paper.pdf + out/sample-solution.pdf
"""
from __future__ import annotations

import sys
from pathlib import Path

# 让脚本能 import 项目 src
PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.core.render.weekly_batch import (
    WeeklyConstraints,
    WeeklyScope,
    run,
)


def _make_item(
    *,
    item_version_id: str,
    item_id: str,
    interaction_id: str,
    text: str,
    options: list[tuple[str, str]] | None = None,
    blank_id: str | None = None,
    answer: str | None = None,
) -> dict:
    """构造测试用 ItemVersion dict."""
    blocks: list[dict] = [{"type": "text", "value": text}]
    if interaction_id in ("single_choice", "multi_choice"):
        assert options is not None
        blocks.append({
            "type": "choice",
            "mode": "single" if interaction_id == "single_choice" else "multi",
            "options": [{"id": oid, "label": label} for oid, label in options],
        })
    elif interaction_id in ("text_blank", "numeric_blank"):
        assert blank_id is not None
        kind = "text" if interaction_id == "text_blank" else "numeric"
        blocks.append({"type": "fill", "blank_id": blank_id, "kind": kind})
    scorer_params: dict = {}
    if answer is not None:
        scorer_params["answer"] = answer
    return {
        "item_version_id": item_version_id,
        "item_id": item_id,
        "interaction_ref": {"interaction_id": interaction_id},
        "content": {"blocks": blocks},
        "scoring_ref": {"scorer_params": scorer_params},
    }


def main() -> int:
    """生成样例 PDF 到 out/ 目录."""
    # 5 题实例池
    pool = [
        _make_item(
            item_version_id="iv-sc-001",
            item_id="i-sc-001",
            interaction_id="single_choice",
            text="1 + 1 = ?",
            options=[("A", "1"), ("B", "2"), ("C", "3"), ("D", "4")],
            answer="B",
        ),
        _make_item(
            item_version_id="iv-sc-002",
            item_id="i-sc-002",
            interaction_id="single_choice",
            text="2 + 2 = ?",
            options=[("A", "3"), ("B", "4"), ("C", "5"), ("D", "6")],
            answer="B",
        ),
        _make_item(
            item_version_id="iv-mc-001",
            item_id="i-mc-001",
            interaction_id="multi_choice",
            text="下列哪些是偶数？",
            options=[("A", "1"), ("B", "2"), ("C", "3"), ("D", "4")],
            answer="B,D",
        ),
        _make_item(
            item_version_id="iv-tb-001",
            item_id="i-tb-001",
            interaction_id="text_blank",
            text="中国的首都是______。",
            blank_id="b1",
            answer="北京",
        ),
        _make_item(
            item_version_id="iv-nb-001",
            item_id="i-nb-001",
            interaction_id="numeric_blank",
            text="3 + 5 = ______。",
            blank_id="b1",
            answer="8",
        ),
    ]

    scope = WeeklyScope(
        subject_pack_id="subject-math",
        gradeband="M",
        kp_codes=("math.nal.decimal.compare",),
        kp_snapshot_ref="snap-2026-W30-001",
    )
    constraints = WeeklyConstraints(
        num_items=4,
        interaction_distribution={
            "single_choice": 1,
            "multi_choice": 1,
            "text_blank": 1,
            "numeric_blank": 1,
        },
        seed=42,
        paper_title="三年级数学周练·样例",
    )

    output_dir = PROJECT_ROOT / "out"
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"[sample] 输出目录: {output_dir}")
    print(f"[sample] 题量: {constraints.num_items}（4 种交互各 1 题）")
    print(f"[sample] 开始生成 PDF（Edge headless）...")

    result = run(
        scope,
        constraints,
        output_dir,
        item_version_pool=pool,
        paper_id="01H3K7X9P0Q1R2S3T4V5W6X7Y",
        paper_spec_id="spec-sample-2026-W30-001",
    )

    print(f"[sample] ✅ 试卷 PDF: {result.paper_pdf_path}")
    print(f"[sample] ✅ 解析册 PDF: {result.solution_pdf_path}")
    print(f"[sample] 卷码: {result.paper_code}")
    print(f"[sample] 卷规格 ID: {result.paper_spec_id}")
    print(f"[sample] 题短码列表:")
    for row in result.paper_item_rows:
        print(f"  - 题 {row['item_number']}: {row['item_short_code']} (item_version={row['item_version_id']})")

    # 校验 PDF 文件
    assert result.paper_pdf_path.is_file(), "试卷 PDF 未生成"
    assert result.solution_pdf_path.is_file(), "解析册 PDF 未生成"
    assert result.paper_pdf_path.stat().st_size > 1000, "试卷 PDF 过小"
    assert result.paper_pdf_path.read_bytes()[:5].startswith(b"%PDF"), "试卷 PDF 魔数错误"

    print(f"[sample] PDF 大小: 试卷={result.paper_pdf_path.stat().st_size}B 解析册={result.solution_pdf_path.stat().st_size}B")
    print(f"[sample] 完成")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
