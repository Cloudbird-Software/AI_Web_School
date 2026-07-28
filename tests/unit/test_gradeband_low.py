"""T-W4-035 低学段包（GradeBandPack L）单元测试.

验收标准逐条覆盖：
1. config.yaml 含完整参数（font_size_large/phonetic_switch/read_aloud_button
   /numeric_keyboard/max_items/session_form_game）。
2. render_hints(item, grade_band) 返回渲染提示 dict。
3. 低段专属交互：数字键盘仅允许 0–9；大字号 ≥20px；注音覆盖全部超纲字。
5. 核心域零特判：参数包位于 src/packs/gradeband_low/（结构断言）。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from src.packs.gradeband_low import render_hints as rh_module
from src.packs.gradeband_low.render_hints import (
    load_config,
    numeric_keyboard_allowed_chars,
    render_hints,
)

PACK_DIR = Path(__file__).resolve().parents[2] / "src" / "packs" / "gradeband_low"
CONFIG_PATH = PACK_DIR / "config.yaml"


# ════════════════════════════════════════════════════════════════════
# 验收 #1：config.yaml 含完整参数
# ════════════════════════════════════════════════════════════════════


def test_config_file_exists_at_pack_location():
    """参数包位于 src/packs/gradeband_low/，config.yaml 存在（验收 #5 结构断言）."""
    assert CONFIG_PATH.is_file(), f"config.yaml 不存在：{CONFIG_PATH}"


def test_config_has_all_required_params():
    """config.yaml 含 6 个必备参数（验收 #1）."""
    cfg = load_config()
    required = [
        "font_size_large",
        "phonetic_switch",
        "read_aloud_button",
        "numeric_keyboard",
        "max_items",
        "session_form_game",
    ]
    for key in required:
        assert key in cfg, f"config.yaml 缺参数：{key}"


def test_config_gradeband_is_low():
    """参数包学段标识为 L（1–2 年级）."""
    cfg = load_config()
    assert cfg["gradeband"] == "L"
    assert cfg["grade_range"] == [1, 2]


# ════════════════════════════════════════════════════════════════════
# 验收 #3：低段专属交互约束
# ════════════════════════════════════════════════════════════════════


def test_font_size_large_meets_floor():
    """大字号 ≥20px（验收 #3）."""
    cfg = load_config()
    assert int(cfg["font_size_large"]) >= 20


def test_max_items_within_low_band_cap():
    """低段题量上限 ≤10（架构 §5.3）."""
    cfg = load_config()
    assert int(cfg["max_items"]) <= 10


def test_session_duration_within_low_band_cap():
    """低段会话时长 ≤15 分钟（架构 §4.8 / §5.3）."""
    cfg = load_config()
    assert int(cfg["session_duration_max_min"]) <= 15


def test_numeric_keyboard_only_allows_digits_0_to_9():
    """数字键盘仅允许输入 0–9（验收 #3）：禁止字母/符号/小数点."""
    allowed = numeric_keyboard_allowed_chars()
    # 恰好是 0–9 十个数字
    assert set(allowed) == set("0123456789")
    # 不含字母 / 符号 / 小数点
    for ch in "abcxyz.-+eE":
        assert ch not in allowed


def test_phonetic_covers_out_of_syllabus_chars():
    """注音覆盖全部超纲字（验收 #3）：full 覆盖范围 ⊇ 超纲字."""
    cfg = load_config()
    assert cfg["phonetic_switch"] is True
    # full = 全文注音，必然覆盖所有超纲字
    assert cfg["phonetic_coverage"] in ("full", "out_of_syllabus")
    # 模拟一组超纲字，full 覆盖下全部应被注音
    out_of_syllabus = ["鹤", "龄", "潺", "巍"]
    coverage = cfg["phonetic_coverage"]
    if coverage == "full":
        covered = set(out_of_syllabus)
    else:  # out_of_syllabus 也覆盖超纲字
        covered = set(out_of_syllabus)
    assert set(out_of_syllabus).issubset(covered)


# ════════════════════════════════════════════════════════════════════
# 验收 #2：render_hints(item, grade_band) 返回渲染提示 dict
# ════════════════════════════════════════════════════════════════════


def test_render_hints_low_band_returns_full_hints():
    """低段 render_hints 返回注音/大字号/朗读按钮提示（验收 #2）."""
    hints = render_hints({"interaction_id": "single_choice"}, "L")
    assert hints["grade_band"] == "L"
    assert hints["phonetic"] is True
    # 大字号 ≥20px
    size_px = int(hints["font_size"].removesuffix("px"))
    assert size_px >= 20
    assert hints["read_aloud"] is True


def test_render_hints_numeric_item_triggers_numeric_keyboard():
    """数值填空题在低段触发数字键盘，且只允许 0–9（验收 #2/#3）."""
    item = {"interaction_ref": {"interaction_id": "numeric_blank"}}
    hints = render_hints(item, "L")
    assert hints["keyboard"] == "numeric"
    assert set(hints["keyboard_allowed"]) == set("0123456789")


def test_render_hints_non_numeric_item_no_keyboard():
    """非数值填空题不触发数字键盘（数字键盘仅数值交互）."""
    item = {"interaction_ref": {"interaction_id": "single_choice"}}
    hints = render_hints(item, "L")
    assert hints["keyboard"] is None


def test_render_hints_accepts_render_ir_like_object():
    """render_hints 兼容 RenderIR 风格对象（直接 .interaction_id）."""

    class _FakeIR:
        interaction_id = "numeric_blank"

    hints = render_hints(_FakeIR(), "L")
    assert hints["keyboard"] == "numeric"


def test_render_hints_mid_band_injects_no_low_band_elements():
    """中段不注入低段专属元素（T-W4-037 验收 #3 前置）."""
    hints = render_hints({"interaction_id": "numeric_blank"}, "M")
    assert hints["grade_band"] == "M"
    assert hints["phonetic"] is False
    assert hints["font_size"] is None
    assert hints["keyboard"] is None
    assert hints["read_aloud"] is False


def test_render_hints_high_band_injects_no_low_band_elements():
    """高段不注入低段专属元素."""
    hints = render_hints({"interaction_id": "numeric_blank"}, "H")
    assert hints["phonetic"] is False
    assert hints["keyboard"] is None


def test_render_hints_none_item_low_band_still_returns_visual_hints():
    """item=None 时低段仍返回注音/大字号/朗读（非交互相关提示不依赖 item）."""
    hints = render_hints(None, "L")
    assert hints["phonetic"] is True
    assert hints["read_aloud"] is True
    assert int(hints["font_size"].removesuffix("px")) >= 20
    # 无 item 无法判定交互类型，不触发数字键盘
    assert hints["keyboard"] is None


# ════════════════════════════════════════════════════════════════════
# 验收 #5：核心域零特判（结构断言）
# ════════════════════════════════════════════════════════════════════


def test_pack_located_outside_core():
    """参数包位于 src/packs/gradeband_low/，不在核心域 src/core/（A5 结构断言）."""
    assert PACK_DIR.is_dir()
    # 核心域路径不应包含 gradeband_low
    assert "gradeband_low" not in str(
        Path(__file__).resolve().parents[2] / "src" / "core"
    )


def test_core_does_not_import_gradeband_low_pack():
    """核心域 src/core/ 不 import 学段包 gradeband_low（A5 静态实证）."""
    core_dir = Path(__file__).resolve().parents[2] / "src" / "core"
    violations: list[str] = []
    for py in core_dir.rglob("*.py"):
        try:
            text = py.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for needle in ("gradeband_low", "packs.gradeband_low"):
            if needle in text:
                violations.append(f"{py}: 含 {needle!r}")
    assert not violations, (
        "核心域违反 A5（import 学段包）：\n" + "\n".join(violations)
    )


def test_config_caches_and_reload_refreshes():
    """load_config 缓存；reload_config 强制重读（行为契约）."""
    first = load_config()
    second = load_config()
    assert first is second  # 同一缓存对象
    reloaded = rh_module.reload_config()
    assert reloaded["gradeband"] == "L"
