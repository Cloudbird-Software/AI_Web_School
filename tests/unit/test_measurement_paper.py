"""T-W4-029 测量卷产出 + 渲染适配单元测试.

对照任务卡验收标准（逐条可执行）：
1. build_measurement_paper(solution, spec_table) 返回完整试卷对象，含细目表映射、
   题序、作答说明
2. 细目表合规校验：产出卷每单元格题数与难度与细目表一致，偏差为 0
3. 渲染适配：测量卷含作答卡区域、题号对齐、禁止标记（如「翻页无效」）
4. make accept TASK=T-W4-029 全绿
5. 不 import 任何学科包/学段包

测试策略：用 CP-SAT solve() 产真实可行解（验收 #1 的 solution 来自 028 已验证的
求解器），再 build → verify → adapt，形成 027→028→029 链路自验。
"""
from __future__ import annotations

from pathlib import Path
import re

import pytest

from src.core.assembly.measurement_paper import (
    ComplianceReport,
    MeasurementPaper,
    build_measurement_paper,
    verify_compliance,
)
from src.core.assembly.solver.constraints import CpSatInfeasible, MeasurementCandidate
from src.core.assembly.solver.cpsat_solver import CpSatSolution, solve
from src.core.assembly.spec_table import SpecTable
from src.core.render.ir import ChoiceBlock, OptionItem, RenderIR
from src.core.render.measurement_adapter import (
    AnswerCardRegion,
    MeasurementRenderIR,
    ProhibitionMarker,
    adapt_measurement_paper,
)


# ────────────────────────────────────────────────────────────────────
# 构造辅助（与 test_cpsat_solver.py 同构，保持测试独立）
# ────────────────────────────────────────────────────────────────────

def _cand(
    vid: str, kp: str, cognitive: str, p: float, *, group: str | None = None,
) -> MeasurementCandidate:
    return MeasurementCandidate(
        item_version_id=vid,
        kp_codes=[kp],
        cognitive_level=cognitive,
        p_correct=p,
        group_id=group,
        template_version_id=f"tpl-{vid}",
    )


def _cell(kp: str, cognitive: str, count: int, *, dmin=0.50, dmax=0.80) -> dict:
    return {
        "content_code": kp,
        "cognitive_level": cognitive,
        "target_count": count,
        "difficulty_min": dmin,
        "difficulty_max": dmax,
    }


def _spec_table_8cells() -> SpecTable:
    """8 单元格共 12 题（4 kp × 2 认知层级）."""
    cells = [
        _cell("math.a", "remember", 2, dmin=0.50, dmax=0.80),
        _cell("math.a", "apply", 1, dmin=0.30, dmax=0.60),
        _cell("math.b", "remember", 2, dmin=0.50, dmax=0.80),
        _cell("math.b", "apply", 1, dmin=0.30, dmax=0.60),
        _cell("math.c", "remember", 2, dmin=0.50, dmax=0.80),
        _cell("math.c", "apply", 1, dmin=0.30, dmax=0.60),
        _cell("math.d", "remember", 2, dmin=0.50, dmax=0.80),
        _cell("math.d", "apply", 1, dmin=0.30, dmax=0.60),
    ]
    return SpecTable(
        spec_table_id="spec-meas-paper-test",
        spec_table_version="1.0.0",
        gradeband="M",
        graph_release="graph-math-2026q1",
        cells=cells,
    )


def _feasible_pool_8cells() -> list[MeasurementCandidate]:
    """每 cell 3 个合格候选（p_correct 落在 [dmin, dmax]）."""
    pool: list[MeasurementCandidate] = []
    for idx, cell in enumerate(_spec_table_8cells().cells):
        mid = (cell.difficulty_min + cell.difficulty_max) / 2
        for j in range(3):
            p = max(0.0, min(1.0, mid + 0.02 * (j - 1)))
            pool.append(_cand(f"item-{idx}-{j}", cell.content_code, cell.cognitive_level, p))
    return pool


def _make_choice_ir(vid: str, labels: tuple[str, ...] = ("A", "B", "C", "D")) -> RenderIR:
    """构造带 ChoiceBlock 的 RenderIR（模拟 item_to_ir 产物）."""
    return RenderIR(
        item_version_id=vid,
        item_id=f"item-{vid}",
        interaction_id="single_choice",
        item_number=None,  # 由 adapter 分配
        blocks=[
            ChoiceBlock(
                mode="single",
                options=[OptionItem(id=l, label=l) for l in labels],
            )
        ],
    )


@pytest.fixture
def feasible_solution() -> CpSatSolution:
    """CP-SAT 可行解（8 cell × 12 题）."""
    result = solve(_spec_table_8cells(), _feasible_pool_8cells(), seed=0)
    assert isinstance(result, CpSatSolution), "前置：可行解应成功"
    return result


# ════════════════════════════════════════════════════════════════════
# 验收 #1：build_measurement_paper 返回完整试卷对象
# ════════════════════════════════════════════════════════════════════

def test_build_returns_paper_with_spec_mapping(feasible_solution):
    """返回 MeasurementPaper，cell_mappings 覆盖 spec_table 全部 cell."""
    st = _spec_table_8cells()
    paper = build_measurement_paper(feasible_solution, st)

    assert isinstance(paper, MeasurementPaper)
    assert paper.spec_table_id == st.spec_table_id
    assert paper.spec_table_version == st.spec_table_version
    assert paper.seed == feasible_solution.seed
    # cell_mappings 按 spec_table.cells 顺序，8 个
    assert len(paper.cell_mappings) == len(st.cells) == 8
    for mapping, cell in zip(paper.cell_mappings, st.cells):
        assert mapping.content_code == cell.content_code
        assert mapping.cognitive_level == cell.cognitive_level
        assert mapping.target_count == cell.target_count
        assert mapping.difficulty_min == cell.difficulty_min
        assert mapping.difficulty_max == cell.difficulty_max


def test_build_paper_has_question_order(feasible_solution):
    """题序：ordered_item_version_ids 按 cell 顺序聚合，总数 == spec total_count."""
    st = _spec_table_8cells()
    paper = build_measurement_paper(feasible_solution, st)

    assert len(paper.ordered_item_version_ids) == st.total_count == 12
    # 题序按 cell 顺序：每 cell 的 item_version_ids 连续聚合
    cursor = 0
    for mapping in paper.cell_mappings:
        seg = paper.ordered_item_version_ids[cursor:cursor + len(mapping.item_version_ids)]
        assert seg == mapping.item_version_ids, "题序应按 cell 顺序聚合"
        cursor += len(mapping.item_version_ids)
    # 无重复
    assert len(set(paper.ordered_item_version_ids)) == len(paper.ordered_item_version_ids)


def test_build_paper_has_answer_instructions_and_digest(feasible_solution):
    """作答说明非空且含题量；selection_digest 来自 solution."""
    st = _spec_table_8cells()
    paper = build_measurement_paper(feasible_solution, st)

    assert paper.answer_instructions, "作答说明不应为空"
    assert str(st.total_count) in paper.answer_instructions, "作答说明应含题量"
    assert paper.selection_digest == feasible_solution.selection_digest
    assert paper.selection_digest, "selection_digest 不应为空"
    # item_p_correct 覆盖全部入选题
    assert set(paper.item_p_correct.keys()) == set(paper.ordered_item_version_ids)


def test_build_rejects_infeasible_solution():
    """不可行解不能组卷（§4.4 铁律：禁止静默放松）."""
    st = _spec_table_8cells()
    # 候选池只够 3 题 → 不可行
    infeasible = solve(st, _feasible_pool_8cells()[:3], seed=0)
    assert isinstance(infeasible, CpSatInfeasible), "前置：应返回不可行报告"
    with pytest.raises(ValueError, match="不可行解"):
        build_measurement_paper(infeasible, st)  # type: ignore[arg-type]


def test_build_accepts_custom_instructions(feasible_solution):
    """自定义作答说明覆盖默认文案."""
    st = _spec_table_8cells()
    custom = "自定义作答说明：请认真作答。"
    paper = build_measurement_paper(feasible_solution, st, answer_instructions=custom)
    assert paper.answer_instructions == custom


# ════════════════════════════════════════════════════════════════════
# 验收 #2：细目表合规校验，偏差为 0
# ════════════════════════════════════════════════════════════════════

def test_compliance_valid_solution_zero_deviation(feasible_solution):
    """CP-SAT 可行解产出卷：合规、偏差 0."""
    st = _spec_table_8cells()
    paper = build_measurement_paper(feasible_solution, st)
    report = verify_compliance(paper, st)

    assert isinstance(report, ComplianceReport)
    assert report.is_compliant is True
    assert report.total_count_deviation == 0
    assert report.violations == []


def test_compliance_detects_count_mismatch(feasible_solution):
    """篡改某 cell 题数 → count_mismatch + 偏差 > 0."""
    st = _spec_table_8cells()
    paper = build_measurement_paper(feasible_solution, st)

    # 篡改首个 cell（target_count=2）：移除一道题
    first = paper.cell_mappings[0]
    assert first.target_count >= 1
    tampered_first = first.model_copy(
        update={
            "item_version_ids": first.item_version_ids[:-1],
            "actual_count": first.actual_count - 1,
            "item_p_corrects": first.item_p_corrects[:-1],
        }
    )
    tampered_paper = paper.model_copy(
        update={"cell_mappings": [tampered_first] + list(paper.cell_mappings[1:])}
    )

    report = verify_compliance(tampered_paper, st)
    assert not report.is_compliant
    assert report.total_count_deviation == 1
    count_violations = [v for v in report.violations if v.kind == "count_mismatch"]
    assert len(count_violations) == 1
    assert count_violations[0].cell_key == f"{first.content_code}/{first.cognitive_level}"


def test_compliance_detects_difficulty_out_of_range(feasible_solution):
    """篡改某题 p_correct 越出 cell 难度区间 → difficulty_out_of_range."""
    st = _spec_table_8cells()
    paper = build_measurement_paper(feasible_solution, st)

    # 篡改首个 cell 的首题 p_correct 为 0.99（cell 区间 [0.50, 0.80]）
    first = paper.cell_mappings[0]
    tampered_p = first.item_p_corrects[:-1] + [0.99]
    tampered_first = first.model_copy(update={"item_p_corrects": tampered_p})
    tampered_paper = paper.model_copy(
        update={"cell_mappings": [tampered_first] + list(paper.cell_mappings[1:])}
    )

    report = verify_compliance(tampered_paper, st)
    assert not report.is_compliant
    diff_violations = [v for v in report.violations if v.kind == "difficulty_out_of_range"]
    assert len(diff_violations) >= 1
    assert "0.99" in diff_violations[0].actual


def test_compliance_detects_missing_cell(feasible_solution):
    """产出卷缺失某 cell → cell_missing + 偏差 = target_count."""
    st = _spec_table_8cells()
    paper = build_measurement_paper(feasible_solution, st)

    # 移除最后一个 cell 的映射
    tampered_paper = paper.model_copy(
        update={"cell_mappings": list(paper.cell_mappings[:-1])}
    )
    report = verify_compliance(tampered_paper, st)
    assert not report.is_compliant
    missing = [v for v in report.violations if v.kind == "cell_missing"]
    assert len(missing) == 1


# ════════════════════════════════════════════════════════════════════
# 验收 #3：渲染适配 — 作答卡 / 题号对齐 / 禁止标记
# ════════════════════════════════════════════════════════════════════

def _build_item_irs(paper: MeasurementPaper) -> dict[str, RenderIR]:
    """为卷内每题构造 ChoiceBlock IR（模拟 item_to_ir 产物）."""
    return {vid: _make_choice_ir(vid) for vid in paper.ordered_item_version_ids}


def test_adapt_produces_measurement_render_ir(feasible_solution):
    """adapt 返回 MeasurementRenderIR，含题序 IR + 作答卡 + 禁止标记."""
    st = _spec_table_8cells()
    paper = build_measurement_paper(feasible_solution, st)
    item_irs = _build_item_irs(paper)

    ir = adapt_measurement_paper(paper, item_irs, paper_title="数学测量卷")

    assert isinstance(ir, MeasurementRenderIR)
    assert ir.paper_title == "数学测量卷"
    assert ir.spec_table_ref == f"{st.spec_table_id}/{st.spec_table_version}"
    assert ir.page_instructions == paper.answer_instructions


def test_adapt_question_number_alignment(feasible_solution):
    """题号对齐：ordered_item_irs 按卷题序分配 1..N."""
    st = _spec_table_8cells()
    paper = build_measurement_paper(feasible_solution, st)
    item_irs = _build_item_irs(paper)

    ir = adapt_measurement_paper(paper, item_irs)

    assert len(ir.ordered_item_irs) == len(paper.ordered_item_version_ids)
    # 题号 1..N 连续
    numbers = [i.item_number for i in ir.ordered_item_irs]
    assert numbers == [str(n) for n in range(1, len(ir.ordered_item_irs) + 1)]
    # item_version_id 与 paper 题序一致
    ordered_vids = [i.item_version_id for i in ir.ordered_item_irs]
    assert ordered_vids == paper.ordered_item_version_ids


def test_adapt_answer_card_region(feasible_solution):
    """作答卡区域：每题一行，含题号 + 选项 label."""
    st = _spec_table_8cells()
    paper = build_measurement_paper(feasible_solution, st)
    item_irs = _build_item_irs(paper)

    ir = adapt_measurement_paper(paper, item_irs)

    assert isinstance(ir.answer_card, AnswerCardRegion)
    assert len(ir.answer_card.rows) == len(paper.ordered_item_version_ids)
    for idx, row in enumerate(ir.answer_card.rows, start=1):
        assert row.item_number == str(idx)
        assert row.item_version_id == paper.ordered_item_version_ids[idx - 1]
        # ChoiceBlock 选项 label A/B/C/D
        assert row.option_labels == ["A", "B", "C", "D"]


def test_adapt_answer_card_empty_for_non_choice(feasible_solution):
    """非选择题（无 ChoiceBlock）作答卡行 option_labels 为空."""
    st = _spec_table_8cells()
    paper = build_measurement_paper(feasible_solution, st)
    # 首题用填空 IR（无 ChoiceBlock）
    first_vid = paper.ordered_item_version_ids[0]
    item_irs = _build_item_irs(paper)
    item_irs[first_vid] = RenderIR(
        item_version_id=first_vid,
        item_id=f"item-{first_vid}",
        interaction_id="text_blank",
        blocks=[],
    )

    ir = adapt_measurement_paper(paper, item_irs)
    first_row = ir.answer_card.rows[0]
    assert first_row.option_labels == [], "填空题作答卡行不应有选项 label"


def test_adapt_prohibition_markers_contain_page_invalid(feasible_solution):
    """禁止标记含「翻页无效」（验收 #3 明确要求）."""
    st = _spec_table_8cells()
    paper = build_measurement_paper(feasible_solution, st)
    item_irs = _build_item_irs(paper)

    ir = adapt_measurement_paper(paper, item_irs)

    assert len(ir.prohibition_markers) >= 1
    texts = [m.text for m in ir.prohibition_markers]
    assert "翻页无效" in texts, "禁止标记必须含「翻页无效」"
    # 每条标记位置合法
    for m in ir.prohibition_markers:
        assert isinstance(m, ProhibitionMarker)
        assert m.position in ("header", "footer", "page_break")


def test_adapt_rejects_missing_item_ir(feasible_solution):
    """item_irs 缺某题 → ValueError（禁止静默丢题）."""
    st = _spec_table_8cells()
    paper = build_measurement_paper(feasible_solution, st)
    item_irs = _build_item_irs(paper)
    # 移除一题的 IR
    missing_vid = paper.ordered_item_version_ids[0]
    del item_irs[missing_vid]

    with pytest.raises(ValueError, match="缺少 item_version_id"):
        adapt_measurement_paper(paper, item_irs)


def test_adapt_rejects_empty_paper():
    """空题序卷 → ValueError."""
    # 构造空题序 paper：cell_mappings 需 ≥1（min_length=1），但 ordered 为空
    from src.core.assembly.measurement_paper import MeasurementCellMapping
    empty_paper = MeasurementPaper(
        spec_table_id="x", spec_table_version="1", seed=0,
        cell_mappings=[MeasurementCellMapping(
            content_code="x", cognitive_level="apply",
            target_count=0, actual_count=0, difficulty_min=0.5, difficulty_max=0.8,
        )],
        ordered_item_version_ids=[],
        answer_instructions="x",
        selection_digest="d",
        item_p_correct={},
    )
    with pytest.raises(ValueError, match="题序为空"):
        adapt_measurement_paper(empty_paper, {})


# ════════════════════════════════════════════════════════════════════
# 验收 #5：宪法 A5/A7 边界——不 import 任何学科包/学段包
# ════════════════════════════════════════════════════════════════════

def test_no_subject_pack_imports_in_measurement_modules():
    """measurement_paper.py / measurement_adapter.py 不 import 学科包/学段包."""
    project_root = Path(__file__).resolve().parent.parent.parent
    targets = [
        project_root / "src" / "core" / "assembly" / "measurement_paper.py",
        project_root / "src" / "core" / "render" / "measurement_adapter.py",
    ]
    pattern = re.compile(
        r"^\s*(?:from\s+(?:packs|subject_)|import\s+(?:packs|subject_))",
        re.MULTILINE,
    )
    violations: list[str] = []
    for py_file in targets:
        assert py_file.is_file(), f"文件不存在：{py_file}"
        text = py_file.read_text(encoding="utf-8")
        if pattern.findall(text):
            violations.append(str(py_file.relative_to(project_root)))
    assert not violations, (
        f"测量卷模块存在学科包/学段包 import（违反 A5/A7）：{violations}"
    )
