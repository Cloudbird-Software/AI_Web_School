"""T-W2-038 周更静态批处理管线 v1.

输入：手工指定的知识点范围快照 + 份数 + 题量/交互分布约束
流程：
  1. 从 published 实例池确定性选题（同 snapshot+seed→同题序）
  2. 每题 ItemVersion → RenderIR → HTML
  3. 组装到品牌模板 page.html，含卷码 QR
  4. HTML → PDF（Edge headless 或 playwright）
  5. 生成试卷 PDF + 解析册 PDF（解析册含答案，W2 v1 不含 AI 解析）
  6. paper + paper_item 数据以 dict 形式返回（由调用方落库）

为什么 W2 v1 用确定性装填而非 CP-SAT 求解器：
- 求解器需要约束传播框架（T-W2-038 non_goal），W2 阶段先验证管线贯通
- 确定性装填：按 interaction_distribution 顺序，从 pool 中筛 matching 项，
  用 seed 随机打乱后取前 N 个；同 snapshot+seed → 同题序（可复现）

为什么 paper/paper_item 以 dict 返回而非直接写 DB：
- 调用方可能是同步或异步运行时；本函数保持纯函数特性
- DB 写入由 src/core/render/paper_writer.py（后续卡）或调用方负责
- 测试时无需 mock DB session

学科零特判（A5）：本模块是核心域，不 import 学科包；
若需挂载学科 SVG 组件，由调用方预先生成 math_svg 块注入 item_version.content。
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import ulid

from src.core.render.html_renderer import render_item
from src.core.render.item_to_ir import item_to_ir
from src.core.render.pdf_exporter import PdfBackend, PdfExporter
from src.core.render.trace_codes import (
    generate_item_short_code,
    generate_paper_code,
    generate_qr_payload,
    generate_qr_svg,
)


# ════════════════════════════════════════════════════════════════════
# 输入数据类
# ════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class WeeklyScope:
    """周更范围（学科+学段+知识点快照）.

    Attributes:
        subject_pack_id: 学科包 id（'subject-math' / 'subject-chinese' / 'subject-english'）
        gradeband: 学段（'L' / 'M' / 'H'）
        kp_codes: 知识点编码列表（如 ['math.nal.decimal.compare']）
        kp_snapshot_ref: 知识点范围快照引用 id（用于 paper 表追溯）
    """

    subject_pack_id: str
    gradeband: str
    kp_codes: tuple[str, ...]
    kp_snapshot_ref: str


@dataclass(frozen=True)
class WeeklyConstraints:
    """组卷约束.

    Attributes:
        num_items: 题量
        interaction_distribution: 交互类型→数量映射（如 {'single_choice': 2, 'numeric_blank': 1}）
        seed: 确定性种子（同 seed + 同 pool → 同题序）
        paper_title: 卷名
    """

    num_items: int
    interaction_distribution: dict[str, int]
    seed: int
    paper_title: str = "周练"


# ════════════════════════════════════════════════════════════════════
# 输出数据类
# ════════════════════════════════════════════════════════════════════

@dataclass
class WeeklyBatchResult:
    """周更批处理结果.

    Attributes:
        paper_id: 卷内部 id（ULID）
        paper_code: 人类可读卷码（27 字符）
        paper_spec_id: 卷规格 id（QR 含此 id+校验位）
        paper_pdf_path: 试卷 PDF 路径
        solution_pdf_path: 解析册 PDF 路径
        paper_row: paper 表行（dict，供调用方落库）
        paper_item_rows: paper_item 表行列表（dict）
    """

    paper_id: str
    paper_code: str
    paper_spec_id: str
    paper_pdf_path: Path
    solution_pdf_path: Path
    paper_row: dict[str, Any]
    paper_item_rows: list[dict[str, Any]]


# ════════════════════════════════════════════════════════════════════
# 选题（确定性装填）
# ════════════════════════════════════════════════════════════════════

def _get_interaction_id(item_version: dict) -> str:
    """从 item_version dict 取 interaction_id."""
    ref = item_version.get("interaction_ref") or {}
    return ref.get("interaction_id") or item_version.get("interaction_id") or ""


def _select_items(
    pool: list[dict],
    constraints: WeeklyConstraints,
) -> list[dict]:
    """确定性选题：按 interaction_distribution 从 pool 中挑选.

    策略：
    1. 对每种交互类型，筛 pool 中匹配的项
    2. 用 seed 随机打乱后取前 N 个
    3. 不足则报 ValueError（fail fast，避免静默减量）

    W2 v1 简化：不检查知识点范围（pool 已经过 serving 视图过滤）；
    后续 v2 引入 CP-SAT 求解器做硬约束传播。
    """
    rng = random.Random(constraints.seed)
    selected: list[dict] = []
    for interaction_id, count in constraints.interaction_distribution.items():
        candidates = [
            iv for iv in pool
            if _get_interaction_id(iv) == interaction_id
        ]
        if len(candidates) < count:
            raise ValueError(
                f"交互类型 {interaction_id} 不足：需 {count}，池中仅 {len(candidates)}"
            )
        # 用 seed 确定性打乱（每次同 seed → 同顺序）
        shuffled = list(candidates)
        rng.shuffle(shuffled)
        selected.extend(shuffled[:count])
    # 总数校验
    if len(selected) != constraints.num_items:
        raise ValueError(
            f"选题总数 {len(selected)} ≠ 题量 {constraints.num_items}（检查 interaction_distribution 之和）"
        )
    return selected


# ════════════════════════════════════════════════════════════════════
# 渲染：试卷 + 解析册
# ════════════════════════════════════════════════════════════════════

_BRAND_DIR = Path(__file__).parent / "brand"


def _load_brand_template() -> tuple[str, str]:
    """加载品牌模板 page.html + default.css.

    返回 (template_text, css_text)。
    """
    template_text = (_BRAND_DIR / "page.html").read_text(encoding="utf-8")
    css_text = (_BRAND_DIR / "default.css").read_text(encoding="utf-8")
    return template_text, css_text


def _render_page_html(
    *,
    paper_title: str,
    paper_code: str,
    qr_svg: str,
    items_html: str,
    css_text: str,
    template_text: str,
) -> str:
    """用 Jinja2 渲染整页 HTML."""
    from jinja2 import Environment, select_autoescape
    from jinja2.exceptions import TemplateSyntaxError

    env = Environment(autoescape=select_autoescape(["html"]))
    try:
        tpl = env.from_string(template_text)
    except TemplateSyntaxError as e:
        raise ValueError(f"品牌模板 page.html 语法错误：{e}") from e
    return tpl.render(
        paper_title=paper_title,
        paper_code=paper_code,
        qr_svg=qr_svg,
        items_html=items_html,
        css_text=css_text,
    )


def _extract_answer(item_version: dict) -> str:
    """从 scoring_ref.scorer_params 提取答案文本（用于解析册）.

    W2 v1 简化：
    - 优先取 scorer_params.answer（字符串）
    - 优先取 scorer_params.correct（字符串）
    - 优先取 scorer_params.correct_options（list，join）
    - 都没有则返回 "（答案略）"

    为什么不做复杂提取：评分器参数结构因 scorer_id 而异；
    解析册的精细答案展示是 W3 任务，W2 v1 先打通管线。
    """
    scoring_ref = item_version.get("scoring_ref") or {}
    params = scoring_ref.get("scorer_params") or {}
    if "answer" in params and params["answer"]:
        return str(params["answer"])
    if "correct" in params and params["correct"]:
        return str(params["correct"])
    if "correct_options" in params and params["correct_options"]:
        opts = params["correct_options"]
        if isinstance(opts, list):
            return ", ".join(str(o) for o in opts)
        return str(opts)
    return "（答案略）"


def _render_solution_item(ir, answer: str) -> str:
    """渲染解析册单题：题面 + 答案（无 AI 解析）."""
    item_html = render_item(ir)
    return (
        f'{item_html}'
        f'<div class="item-answer"><strong>答案：</strong>'
        f'<span class="answer-text">{answer}</span></div>'
    )


# ════════════════════════════════════════════════════════════════════
# 主入口
# ════════════════════════════════════════════════════════════════════

def run(
    scope: WeeklyScope,
    constraints: WeeklyConstraints,
    output_dir: Path,
    *,
    item_version_pool: list[dict],
    paper_id: Optional[str] = None,
    paper_spec_id: Optional[str] = None,
    pdf_backend: PdfBackend = "edge",
    created_by: str = "weekly-batch-v1",
) -> WeeklyBatchResult:
    """运行周更批处理.

    参数:
        scope: 学科/学段/知识点范围
        constraints: 题量/交互分布/种子/卷名
        output_dir: PDF 输出目录（自动创建）
        item_version_pool: 已发布实例池（dict 列表，含 interaction_ref/content/scoring_ref）
        paper_id: 可选卷内部 id（测试可注入），None 则 ULID 生成
        paper_spec_id: 可选卷规格 id，None 则 ULID 生成
        pdf_backend: PDF 后端（edge 本机 / playwright CI）
        created_by: 创建人字段（追溯用）

    返回:
        WeeklyBatchResult（含 paper_id, paper_code, PDF 路径, paper/paper_item dicts）

    异常:
        ValueError: 选题不足 / pool 缺必要字段 / 模板渲染失败
        FileNotFoundError: PDF 后端不可用
        RuntimeError: PDF 导出失败
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # ── 1. 选题 ──
    selected = _select_items(item_version_pool, constraints)

    # ── 2. 生成卷码 / QR ──
    paper_id = paper_id or str(ulid.new())
    paper_spec_id = paper_spec_id or str(ulid.new())
    paper_code = generate_paper_code()  # 用随机 ULID
    qr_payload = generate_qr_payload(paper_spec_id)
    qr_svg = generate_qr_svg(qr_payload)

    # ── 3. 渲染每题为 IR + HTML ──
    template_text, css_text = _load_brand_template()
    paper_items_html_parts: list[str] = []
    solution_items_html_parts: list[str] = []
    paper_item_rows: list[dict[str, Any]] = []

    for idx, iv in enumerate(selected, start=1):
        item_number = str(idx)
        # 先生成 paper_item_id/短码（W3 遗留 S9：卷面印每题短码），
        # 渲染时把 placement_token + item_short_code 透传进 IR 印到卷面
        paper_item_id = str(ulid.new())
        short_code = generate_item_short_code(paper_item_id)
        placement_token = f"q{idx}"
        ir = item_to_ir(
            iv,
            item_number=item_number,
            placement_token=placement_token,
            item_short_code=short_code,
        )
        paper_items_html_parts.append(render_item(ir))

        # 解析册：题面 + 答案
        answer = _extract_answer(iv)
        solution_items_html_parts.append(_render_solution_item(ir, answer))

        # paper_item 行（dict）
        paper_item_rows.append({
            "paper_item_id": paper_item_id,
            "paper_id": paper_id,
            "item_version_id": ir.item_version_id,
            "placement_token": placement_token,
            "item_number": idx,
            "item_short_code": short_code,
        })

    # ── 4. 渲染整页 HTML（试卷 + 解析册） ──
    paper_html = _render_page_html(
        paper_title=constraints.paper_title,
        paper_code=paper_code,
        qr_svg=qr_svg,
        items_html="".join(paper_items_html_parts),
        css_text=css_text,
        template_text=template_text,
    )
    # 解析册用同模板，但加 .item-answer 样式（嵌入 CSS）
    solution_css = css_text + (
        "\n.item-answer { margin: 6px 0 14px 0; padding: 6px 8px; "
        "background: #f5f5f5; border-left: 3px solid #d62728; }\n"
        ".answer-text { color: #d62728; font-weight: bold; }\n"
    )
    solution_html = _render_page_html(
        paper_title=f"{constraints.paper_title}·解析册",
        paper_code=paper_code,
        qr_svg=qr_svg,
        items_html="".join(solution_items_html_parts),
        css_text=solution_css,
        template_text=template_text,
    )

    # ── 5. 导出 PDF ──
    paper_pdf_path = output_dir / f"{paper_id}-paper.pdf"
    solution_pdf_path = output_dir / f"{paper_id}-solution.pdf"
    exporter = PdfExporter(backend=pdf_backend)
    exporter.export(paper_html, paper_pdf_path)
    exporter.export(solution_html, solution_pdf_path)

    # ── 6. 组装 paper 行 ──
    paper_row = {
        "paper_id": paper_id,
        "paper_code": paper_code,
        "paper_spec_id": paper_spec_id,
        "paper_title": constraints.paper_title,
        "gradeband": scope.gradeband,
        "subject_pack_id": scope.subject_pack_id,
        "weekly_batch_id": None,  # 调用方填入批次 id（如 batch-2026-W30）
        "kp_snapshot_ref": scope.kp_snapshot_ref,
        "seed": constraints.seed,
        "rendered_snapshot_path": str(paper_pdf_path),
        "created_by": created_by,
    }

    return WeeklyBatchResult(
        paper_id=paper_id,
        paper_code=paper_code,
        paper_spec_id=paper_spec_id,
        paper_pdf_path=paper_pdf_path,
        solution_pdf_path=solution_pdf_path,
        paper_row=paper_row,
        paper_item_rows=paper_item_rows,
    )


__all__ = [
    "WeeklyScope",
    "WeeklyConstraints",
    "WeeklyBatchResult",
    "run",
]
