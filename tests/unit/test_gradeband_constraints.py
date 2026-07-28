"""T-W4-036 低段组卷 overlay 与闯关会话状态机单元测试.

验收标准逐条覆盖：
1. apply_gradeband_overlay(paper_spec, grade_band) 注入学段约束：
   L 段题量 ≤10、时长 ≤15min、形态=闯关。
2. 闯关形态：game_session.py 实现关卡状态机（未开始/进行中/完成/星级评定），
   支持即时反馈。
3. 约束不可行时（如请求 20 题低段卷）返回明确冲突原因。
5. 不 import 任何学科包/学段包。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from src.core.assembly.gradeband_constraints import (
    GRADEBAND_CONSTRAINTS,
    VALID_GRADEBANDS,
    GradeBandConflictError,
    GradeBandOverlayResult,
    apply_gradeband_overlay,
    build_gradeband_overlay,
)
from src.core.session.game_session import (
    DEFAULT_STAR_THRESHOLDS,
    GameSession,
    GameSessionStateError,
    GameStatus,
    compute_stars,
)

# ════════════════════════════════════════════════════════════════════
# 验收 #1：apply_gradeband_overlay 注入学段约束
# ════════════════════════════════════════════════════════════════════


def test_gradeband_constraints_table_has_three_bands():
    """核心域学段约束政策含 L/M/H 三档."""
    assert set(GRADEBAND_CONSTRAINTS.keys()) == {"L", "M", "H"}
    assert VALID_GRADEBANDS == frozenset({"L", "M", "H"})


def test_apply_gradeband_overlay_low_band_injects_max_items_10():
    """L 段注入题量上限=10（架构 §5.3 低段保护）."""
    result = apply_gradeband_overlay({}, "L")
    assert result.paper_spec["max_items"] == 10
    assert result.paper_spec["gradeband"] == "L"


def test_apply_gradeband_overlay_low_band_injects_time_limit_15():
    """L 段注入会话时长上限=15 分钟（架构 §4.8）."""
    result = apply_gradeband_overlay({}, "L")
    assert result.paper_spec["time_limit_min"] == 15


def test_apply_gradeband_overlay_low_band_injects_session_form_game():
    """L 段注入闯关形态（session_form=game，验收 #1）."""
    result = apply_gradeband_overlay({}, "L")
    assert result.paper_spec["session_form"] == "game"


def test_apply_gradeband_overlay_mid_band_injects_standard_form():
    """M 段注入常规形态、题量上限=20、时长=60min."""
    result = apply_gradeband_overlay({}, "M")
    assert result.paper_spec["session_form"] == "standard"
    assert result.paper_spec["max_items"] == 20
    assert result.paper_spec["time_limit_min"] == 60


def test_apply_gradeband_overlay_high_band_injects_standard_form():
    """H 段注入常规形态、题量上限=30、时长=60min."""
    result = apply_gradeband_overlay({}, "H")
    assert result.paper_spec["session_form"] == "standard"
    assert result.paper_spec["max_items"] == 30
    assert result.paper_spec["time_limit_min"] == 60


def test_apply_gradeband_overlay_returns_overlay_applied_audit_field():
    """返回 overlay_applied 字段供审计（学段约束政策留痕）."""
    result = apply_gradeband_overlay({}, "L")
    assert result.overlay_applied == {
        "max_items": 10,
        "time_limit_min": 15,
        "session_form": "game",
    }


def test_apply_gradeband_overlay_rejects_unknown_band():
    """未知学段抛 ValueError（核心域零特判，学段取值受控）."""
    with pytest.raises(ValueError, match="grade_band"):
        apply_gradeband_overlay({}, "X")


def test_apply_gradeband_overlay_pack_overlay_overrides_defaults():
    """pack config 注入 overlay 覆盖核心默认（核心不 import pack）.

    场景：gradeband_low/config.yaml 自定义 max_items=8 时，
    核心按 pack 注入值生效（核心约束政策只是默认）。
    """
    result = apply_gradeband_overlay(
        {},
        "L",
        overlay={"max_items": 8, "session_duration_max_min": 12},
    )
    assert result.paper_spec["max_items"] == 8
    assert result.paper_spec["time_limit_min"] == 12
    assert result.paper_spec["session_form"] == "game"  # 未覆盖的字段保留默认


def test_apply_gradeband_overlay_pack_overlay_can_switch_off_game():
    """pack overlay 可关闭闯关形态（session_form_game=False → standard）."""
    result = apply_gradeband_overlay(
        {}, "L", overlay={"session_form_game": False}
    )
    assert result.paper_spec["session_form"] == "standard"


def test_build_gradeband_overlay_emits_compile_profile_shape():
    """build_gradeband_overlay 返回 compile_profile 用的 overlay dict 形状."""
    ov = build_gradeband_overlay("L")
    assert ov["overlay_id"] == "gradeband-l"
    assert ov["overlay_version"] == "1.0.0"
    assert ov["item_count_range"] == [1, 10]
    assert ov["time_limit_max_minutes"] == 15
    assert ov["session_form"] == "game"


# ════════════════════════════════════════════════════════════════════
# 验收 #3：约束不可行时返回明确冲突原因
# ════════════════════════════════════════════════════════════════════


def test_apply_gradeband_overlay_low_band_20_items_infeasible():
    """请求 20 题低段卷 → 不可行，conflict 明确说明（验收 #3）."""
    result = apply_gradeband_overlay({"item_count": 20}, "L")
    assert result.feasible is False
    assert result.conflict is not None
    # 冲突原因含学段上限与请求值，便于人类决策
    assert "10" in result.conflict
    assert "20" in result.conflict
    assert "L" in result.conflict


def test_apply_gradeband_overlay_low_band_time_limit_infeasible():
    """请求 30 分钟低段卷 → 不可行（L 段时长 ≤15）."""
    result = apply_gradeband_overlay({"time_limit_min": 30}, "L")
    assert result.feasible is False
    assert result.conflict is not None
    assert "15" in result.conflict
    assert "30" in result.conflict


def test_apply_gradeband_overlay_multiple_conflicts_joined():
    """同时超题量与超时长 → conflict 用 '; ' 拼接多条原因."""
    result = apply_gradeband_overlay(
        {"item_count": 20, "time_limit_min": 30}, "L"
    )
    assert result.feasible is False
    assert ";" in result.conflict


def test_apply_gradeband_overlay_feasible_when_within_caps():
    """请求 8 题 10 分钟低段卷 → 可行."""
    result = apply_gradeband_overlay(
        {"item_count": 8, "time_limit_min": 10}, "L"
    )
    assert result.feasible is True
    assert result.conflict is None


def test_apply_gradeband_overlay_raise_on_conflict_raises():
    """raise_on_conflict=True 时不可行抛 GradeBandConflictError."""
    with pytest.raises(GradeBandConflictError, match="L"):
        apply_gradeband_overlay(
            {"item_count": 20}, "L", raise_on_conflict=True
        )


def test_apply_gradeband_overlay_no_item_count_no_conflict():
    """paper_spec 未声明题量时不报题量冲突（按学段上限注入即可）."""
    result = apply_gradeband_overlay({"time_limit_min": 10}, "L")
    assert result.feasible is True
    assert result.conflict is None
    # 注入仍然完成
    assert result.paper_spec["max_items"] == 10


def test_apply_gradeband_overlay_items_list_count_extracted():
    """paper_spec.items 是 list 时按长度校验题量（兼容多声明形态）."""
    # 10 题在 L 段上限内
    result = apply_gradeband_overlay({"items": ["q"] * 10}, "L")
    assert result.feasible is True
    # 11 题超 L 段上限
    result = apply_gradeband_overlay({"items": ["q"] * 11}, "L")
    assert result.feasible is False


def test_apply_gradeband_overlay_item_count_range_uses_upper_bound():
    """item_count_range=[8, 12] 时按上界 12 校验（L 段超限）."""
    result = apply_gradeband_overlay(
        {"item_count_range": [8, 12]}, "L"
    )
    assert result.feasible is False
    assert "12" in result.conflict


# ════════════════════════════════════════════════════════════════════
# 验收 #2：闯关形态会话状态机
# ════════════════════════════════════════════════════════════════════


def test_game_status_enum_has_three_states():
    """GameStatus 含未开始/进行中/完成三态（验收 #2）."""
    assert GameStatus.NOT_STARTED.value == "not_started"
    assert GameStatus.IN_PROGRESS.value == "in_progress"
    assert GameStatus.COMPLETED.value == "completed"


def test_compute_stars_pure_function_thresholds():
    """compute_stars 纯函数：正确率与阈值算 1/2/3 星（确定性）."""
    # 默认阈值 (0.6, 0.9)
    assert compute_stars(0.0) == 1
    assert compute_stars(0.59) == 1
    assert compute_stars(0.6) == 2
    assert compute_stars(0.89) == 2
    assert compute_stars(0.9) == 3
    assert compute_stars(1.0) == 3
    # 自定义阈值
    assert compute_stars(0.7, (0.7, 0.95)) == 2
    assert compute_stars(0.95, (0.7, 0.95)) == 3


def test_game_session_initial_state_is_not_started():
    """GameSession 初始状态=NOT_STARTED."""
    game = GameSession(total_items=8)
    assert game.status == GameStatus.NOT_STARTED
    assert game.answered == 0
    assert game.correct == 0
    assert game.stars is None


def test_game_session_start_transitions_to_in_progress():
    """start() 后状态迁移到 IN_PROGRESS."""
    game = GameSession(total_items=8)
    game.start()
    assert game.status == GameStatus.IN_PROGRESS


def test_game_session_start_rejects_double_start():
    """非 NOT_STARTED 状态调用 start 抛状态错误."""
    game = GameSession(total_items=8)
    game.start()
    with pytest.raises(GameSessionStateError, match="仅未开始"):
        game.start()


def test_game_session_submit_answer_returns_immediate_feedback():
    """submit_answer 返回即时反馈（对错 + 运行正确率 + 星级预览）."""
    game = GameSession(total_items=4)
    game.start()
    fb = game.submit_answer(item_id="q1", correct=True)
    assert fb["item_id"] == "q1"
    assert fb["correct"] is True
    assert fb["immediate"] is True
    assert fb["running_correct_rate"] == 1.0
    assert fb["stars_preview"] == 3  # 100% → 3 星


def test_game_session_submit_answer_tracks_running_rate():
    """多次作答后即时反馈含累计正确率（即时反馈要求）."""
    game = GameSession(total_items=4)
    game.start()
    game.submit_answer(item_id="q1", correct=True)  # 1/1 = 1.0
    fb2 = game.submit_answer(item_id="q2", correct=False)  # 1/2 = 0.5
    assert fb2["running_correct_rate"] == 0.5
    assert fb2["stars_preview"] == 1  # 0.5 < 0.6 → 1 星


def test_game_session_submit_answer_rejects_when_not_in_progress():
    """非 IN_PROGRESS 状态提交作答抛状态错误."""
    game = GameSession(total_items=4)
    with pytest.raises(GameSessionStateError, match="仅进行中"):
        game.submit_answer(item_id="q1", correct=True)


def test_game_session_finish_requires_all_answered():
    """finish 要求走完全部题；未走完抛状态错误（闯关语义）."""
    game = GameSession(total_items=4)
    game.start()
    game.submit_answer(item_id="q1", correct=True)
    with pytest.raises(GameSessionStateError, match="未走完"):
        game.finish()


def test_game_session_finish_completes_and_assigns_stars():
    """finish() 完成会话并按整体正确率评定 1–3 星."""
    game = GameSession(total_items=4)
    game.start()
    game.submit_answer(item_id="q1", correct=True)
    game.submit_answer(item_id="q2", correct=True)
    game.submit_answer(item_id="q3", correct=True)
    game.submit_answer(item_id="q4", correct=False)
    # 正确率 0.75 → 2 星（默认阈值 0.6/0.9）
    result = game.finish()
    assert game.status == GameStatus.COMPLETED
    assert result["status"] == "completed"
    assert result["stars"] == 2
    assert result["correct_rate"] == 0.75
    assert result["correct"] == 3
    assert result["total"] == 4


def test_game_session_finish_rejects_when_not_in_progress():
    """非 IN_PROGRESS 状态 finish 抛状态错误."""
    game = GameSession(total_items=4)
    with pytest.raises(GameSessionStateError, match="仅进行中"):
        game.finish()


def test_game_session_full_correct_gets_three_stars():
    """全对 → 3 星."""
    game = GameSession(total_items=3)
    game.start()
    for i in range(3):
        game.submit_answer(item_id=f"q{i}", correct=True)
    result = game.finish()
    assert result["stars"] == 3


def test_game_session_all_wrong_gets_one_star():
    """全错 → 1 星（闯关形态不零分，保护低段自尊心）."""
    game = GameSession(total_items=3)
    game.start()
    for i in range(3):
        game.submit_answer(item_id=f"q{i}", correct=False)
    result = game.finish()
    assert result["stars"] == 1


def test_game_session_custom_star_thresholds_via_constructor():
    """学段包可经 star_thresholds 注入覆盖核心默认（核心不 import pack）."""
    # 抬高 3 星阈值到 1.0
    game = GameSession(total_items=2, star_thresholds=(0.5, 1.0))
    game.start()
    game.submit_answer(item_id="q1", correct=True)
    game.submit_answer(item_id="q2", correct=False)
    # 正确率 0.5 → 2 星（达 0.5 但未达 1.0）
    result = game.finish()
    assert result["stars"] == 2


def test_game_session_rejects_non_positive_total():
    """total_items ≤0 抛 ValueError（闯关至少 1 题）."""
    with pytest.raises(ValueError, match="total_items"):
        GameSession(total_items=0)
    with pytest.raises(ValueError, match="total_items"):
        GameSession(total_items=-1)


def test_game_session_rejects_invalid_star_thresholds():
    """star_thresholds 不满足 0≤a≤b≤1 抛 ValueError."""
    with pytest.raises(ValueError, match="star_thresholds"):
        GameSession(total_items=2, star_thresholds=(0.9, 0.6))  # a > b
    with pytest.raises(ValueError, match="star_thresholds"):
        GameSession(total_items=2, star_thresholds=(-0.1, 0.9))  # a < 0
    with pytest.raises(ValueError, match="star_thresholds"):
        GameSession(total_items=2, star_thresholds=(0.6, 1.1))  # b > 1
    with pytest.raises(ValueError, match="star_thresholds"):
        GameSession(total_items=2, star_thresholds=(0.6,))  # 长度 != 2


def test_game_session_feedback_log_records_all_submissions():
    """feedback_log 记录每次即时反馈（审计/回放用）."""
    game = GameSession(total_items=2)
    game.start()
    game.submit_answer(item_id="q1", correct=True, feedback={"hint": "good"})
    game.submit_answer(item_id="q2", correct=False)
    assert len(game.feedback_log) == 2
    assert game.feedback_log[0]["item_id"] == "q1"
    assert game.feedback_log[0]["feedback"] == {"hint": "good"}
    assert game.feedback_log[1]["correct"] is False


def test_game_session_full_flow_practice_session_compose():
    """GameSession 与 PracticeSession 编排组合：完整流程可重复（确定性）.

    场景：8 题，对 6 错 2，正确率 0.75 → 2 星。
    同样输入两次跑结果一致（闯关形态可重放）。
    """
    answers = [(f"q{i}", correct) for i, correct in enumerate(
        [True, True, True, False, True, True, False, True]
    )]

    def _run() -> dict:
        game = GameSession(total_items=8)
        game.start()
        for item_id, correct in answers:
            game.submit_answer(item_id=item_id, correct=correct)
        return game.finish()

    r1 = _run()
    r2 = _run()
    assert r1 == r2
    assert r1["correct"] == 6
    assert r1["total"] == 8
    assert r1["correct_rate"] == 0.75
    assert r1["stars"] == 2


# ════════════════════════════════════════════════════════════════════
# 验收 #5：不 import 任何学科包/学段包（A5 静态实证）
# ════════════════════════════════════════════════════════════════════


def test_gradeband_constraints_module_does_not_import_packs():
    """gradeband_constraints.py 不 import 学科包/学段包（A5 静态实证）."""
    src = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "core"
        / "assembly"
        / "gradeband_constraints.py"
    ).read_text(encoding="utf-8")
    for needle in ("packs.", "gradeband_low", "subject-math", "subject-chinese", "subject-english"):
        assert needle not in src, f"gradeband_constraints.py 含禁用 import: {needle!r}"


def test_game_session_module_does_not_import_packs():
    """game_session.py 不 import 学科包/学段包（A5 静态实证）."""
    src = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "core"
        / "session"
        / "game_session.py"
    ).read_text(encoding="utf-8")
    for needle in ("packs.", "gradeband_low", "subject-math", "subject-chinese", "subject-english"):
        assert needle not in src, f"game_session.py 含禁用 import: {needle!r}"


def test_default_star_thresholds_match_low_band_pack_config():
    """核心默认星级阈值与 gradeband_low/config.yaml 同值（核心不 import pack，但约定同源）."""
    # 默认 (0.6, 0.9)
    assert DEFAULT_STAR_THRESHOLDS == (0.6, 0.9)
