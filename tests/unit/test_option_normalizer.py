"""T-W4-049 英语单选 option_value 口径统一单元测试.

验收对照（T-W4-049）：
  §1 normalize_option 将任意输入（字母/释义/混合）统一归一化为标准字母。
  §2 display_option 按展示模式返回字母或释义（低段释义，高段字母）。
  §3 评分器只消费归一化后的字母，展示层变化不影响评分结果。
  §4 既有英语单选测试不退化（本文件只新增，不改既有测试）。
  §5 核心域零特判：归一化逻辑位于学科包内（本测试 import 学科包模块，
     核心域不 import 本模块——另见 test_no_subject_pack_import 类扫描）。

实现策略：学科包目录 subject-english 含连字符，用 importlib 加载
option_normalizer 模块（同 test_english_vocab.py 模式）。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

# ────────────────────────────────────────────────────────────────────
# importlib 加载 option_normalizer（连字符目录无法普通 import）
# ────────────────────────────────────────────────────────────────────

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_PACK_DIR = _PROJECT_ROOT / "src" / "packs" / "subject-english"
_NORMALIZER_PATH = _PACK_DIR / "option_normalizer.py"


def _load_module(mod_name: str, path: Path):
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载 {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


_norm = _load_module("subject_english_option_normalizer_test", _NORMALIZER_PATH)
normalize_option = _norm.normalize_option
display_option = _norm.display_option
normalize_error_bindings = _norm.normalize_error_bindings
STANDARD_LETTERS = _norm.STANDARD_LETTERS

# 测试用选项映射：字母→释义
_OPTIONS_DICT = {"A": "春天", "B": "夏天", "C": "秋天", "D": "冬天"}
_OPTIONS_LIST = [
    {"letter": "A", "meaning": "春天"},
    {"letter": "B", "meaning": "夏天"},
    {"letter": "C", "meaning": "秋天"},
    {"letter": "D", "meaning": "冬天"},
]
_OPTIONS_TUPLES = [("A", "春天"), ("B", "夏天"), ("C", "秋天"), ("D", "冬天")]


# ════════════════════════════════════════════════════════════════════
# §1 normalize_option：字母 / 释义 / 混合输入归一化
# ════════════════════════════════════════════════════════════════════

class TestNormalizeOptionLetter:
    """字母输入直接归一为大写标准字母."""

    @pytest.mark.parametrize("letter", ["A", "B", "C", "D"])
    def test_uppercase_letter(self, letter):
        assert normalize_option(letter) == letter

    @pytest.mark.parametrize("lower,upper", [("a", "A"), ("b", "B"), ("c", "C"), ("d", "D")])
    def test_lowercase_letter(self, lower, upper):
        assert normalize_option(lower) == upper

    def test_letter_with_whitespace(self):
        assert normalize_option("  a  ") == "A"

    def test_letter_ignores_options(self):
        """字母输入直接归一，不查 options."""
        assert normalize_option("B", options=_OPTIONS_DICT) == "B"


class TestNormalizeOptionMeaning:
    """释义输入经 options 反查归一为字母."""

    def test_meaning_via_dict_options(self):
        assert normalize_option("春天", options=_OPTIONS_DICT) == "A"
        assert normalize_option("冬天", options=_OPTIONS_DICT) == "D"

    def test_meaning_via_list_dict_options(self):
        assert normalize_option("夏天", options=_OPTIONS_LIST) == "B"

    def test_meaning_via_tuple_list_options(self):
        assert normalize_option("秋天", options=_OPTIONS_TUPLES) == "C"

    def test_meaning_with_whitespace(self):
        """释义含前后空白时容错反查."""
        assert normalize_option("  春天  ", options=_OPTIONS_DICT) == "A"

    def test_meaning_not_in_options_returns_none(self):
        """释义不在 options 中 → None（不猜测）."""
        assert normalize_option("未知释义", options=_OPTIONS_DICT) is None

    def test_meaning_without_options_returns_none(self):
        """释义输入但无 options → None（无法反查）."""
        assert normalize_option("春天") is None


class TestNormalizeOptionMixedAndInvalid:
    """混合 / 非法输入归一化."""

    def test_non_string_input_returns_none(self):
        assert normalize_option(123) is None
        assert normalize_option(None) is None
        assert normalize_option(["A"]) is None

    def test_multi_char_string_not_letter(self):
        """多字符字符串（非单字母）当释义处理."""
        # "AB" 不是单字母，当释义反查 options（找不到）→ None
        assert normalize_option("AB", options=_OPTIONS_DICT) is None

    def test_letter_outside_standard(self):
        """单字符但非 A-D → None."""
        assert normalize_option("E") is None
        assert normalize_option("Z") is None
        assert normalize_option("1") is None

    def test_empty_string_returns_none(self):
        assert normalize_option("") is None
        assert normalize_option("   ", options=_OPTIONS_DICT) is None


# ════════════════════════════════════════════════════════════════════
# §2 display_option：按学段与模式返回字母或释义
# ════════════════════════════════════════════════════════════════════

class TestDisplayOption:
    """display_option 按模式/学段返回字母或释义."""

    def test_letter_mode_always_returns_letter(self):
        """mode='letter' 始终返回字母（不论学段）."""
        for band in ("L", "M", "H"):
            assert display_option("A", grade_band=band, mode="letter") == "A"

    def test_meaning_mode_returns_meaning(self):
        assert display_option(
            "B", grade_band="M", mode="meaning", meaning="夏天"
        ) == "夏天"

    def test_meaning_mode_falls_back_when_missing(self):
        """mode='meaning' 但 meaning=None → 回退字母（不阻断渲染）."""
        assert display_option("B", grade_band="L", mode="meaning", meaning=None) == "B"

    def test_auto_mode_low_band_returns_meaning(self):
        """auto 模式下 L 段显示释义（低段识字量小）."""
        assert display_option(
            "C", grade_band="L", mode="auto", meaning="秋天"
        ) == "秋天"

    def test_auto_mode_high_band_returns_letter(self):
        """auto 模式下 M/H 段显示字母（标准化答题卡）."""
        assert display_option("C", grade_band="M", mode="auto", meaning="秋天") == "C"
        assert display_option("C", grade_band="H", mode="auto", meaning="秋天") == "C"

    def test_auto_mode_low_band_without_meaning_falls_back(self):
        """auto+L 段但无 meaning → 回退字母."""
        assert display_option("D", grade_band="L", mode="auto", meaning=None) == "D"

    def test_lowercase_letter_normalized(self):
        assert display_option("a", grade_band="M", mode="letter") == "A"

    def test_invalid_letter_raises(self):
        with pytest.raises(ValueError):
            display_option("E", grade_band="M", mode="letter")

    def test_invalid_grade_band_raises(self):
        with pytest.raises(ValueError):
            display_option("A", grade_band="X", mode="letter")

    def test_invalid_mode_raises(self):
        with pytest.raises(ValueError):
            display_option("A", grade_band="M", mode="bogus")


# ════════════════════════════════════════════════════════════════════
# §3 评分器只消费字母：normalize_error_bindings 把释义口径统一为字母
# ════════════════════════════════════════════════════════════════════

class TestNormalizeErrorBindings:
    """error_bindings.option_value 从释义归一为字母（评分器字母口径）."""

    def test_meaning_option_value_normalized_to_letter(self):
        """释义 option_value 经 options 反查归一为字母."""
        bindings = [
            {"option_value": "春天", "error_type_id": "et_spring"},
            {"option_value": "冬天", "error_type_id": "et_winter"},
        ]
        out = normalize_error_bindings(bindings, _OPTIONS_DICT)
        assert out[0]["option_value"] == "A"
        assert out[1]["option_value"] == "D"
        # 其他键保留
        assert out[0]["error_type_id"] == "et_spring"

    def test_letter_option_value_kept(self):
        """已是字母的 option_value 保持不变."""
        bindings = [{"option_value": "A", "error_type_id": "et_x"}]
        out = normalize_error_bindings(bindings, _OPTIONS_DICT)
        assert out[0]["option_value"] == "A"

    def test_unknown_meaning_kept_as_is(self):
        """无法归一的 option_value 保留原值（向后兼容，不丢数据）."""
        bindings = [{"option_value": "未知释义", "error_type_id": "et_x"}]
        out = normalize_error_bindings(bindings, _OPTIONS_DICT)
        assert out[0]["option_value"] == "未知释义"

    def test_input_not_mutated(self):
        """不修改输入列表/字典（纯函数）."""
        bindings = [{"option_value": "春天", "error_type_id": "et_x"}]
        original = dict(bindings[0])
        normalize_error_bindings(bindings, _OPTIONS_DICT)
        assert bindings[0] == original

    def test_empty_bindings(self):
        assert normalize_error_bindings([], _OPTIONS_DICT) == []

    def test_mixed_letter_and_meaning(self):
        """混合口径（字母+释义）统一为字母."""
        bindings = [
            {"option_value": "A", "error_type_id": "et_a"},
            {"option_value": "夏天", "error_type_id": "et_b"},
        ]
        out = normalize_error_bindings(bindings, _OPTIONS_DICT)
        assert [b["option_value"] for b in out] == ["A", "B"]


# ════════════════════════════════════════════════════════════════════
# §3 评分层与展示层解耦：展示变化不影响评分
# ════════════════════════════════════════════════════════════════════

def test_display_change_does_not_affect_scoring_letter():
    """展示层从字母切到释义，归一化后的评分字母不变（解耦）.

    场景：同一题，低段展示释义、高段展示字母，但评分器与 error_bindings
    消费的 option_value 始终是字母 A/B/C/D。
    """
    # 学生作答 selected：低段可能传释义，高段传字母
    selected_low_band = "春天"   # 低段 UI 传释义
    selected_high_band = "A"     # 高段 UI 传字母

    # 归一化后都是字母 A（评分器只看归一化结果）
    assert normalize_option(selected_low_band, options=_OPTIONS_DICT) == "A"
    assert normalize_option(selected_high_band) == "A"

    # 评分器 answer="A"（字母口径），两种展示层输入归一后都能匹配
    answer = "A"
    assert normalize_option(selected_low_band, options=_OPTIONS_DICT) == answer
    assert normalize_option(selected_high_band) == answer


def test_standard_letters_constant():
    """STANDARD_LETTERS 常量为 A/B/C/D."""
    assert STANDARD_LETTERS == ("A", "B", "C", "D")


# ════════════════════════════════════════════════════════════════════
# §5 核心域零特判：归一化逻辑位于学科包内
# ════════════════════════════════════════════════════════════════════

class TestNoSubjectPackImportInCore:
    """§5 核心域禁止 import 学科包（宪法 A5/X6）——本模块属学科包侧.

    扫描 src/core/ 确认无 option_normalizer / subject-english 引用，
    保证归一化逻辑不泄漏到核心域。
    """

    def test_core_does_not_import_option_normalizer(self):
        """核心域 src/core/ 不 import option_normalizer（学科包逻辑隔离）."""
        core_dir = _PROJECT_ROOT / "src" / "core"
        assert core_dir.is_dir()
        violations: list[str] = []
        for py_file in core_dir.rglob("*.py"):
            text_src = py_file.read_text(encoding="utf-8")
            # 核心域不应引用学科包的 option_normalizer
            if "option_normalizer" in text_src and "subject_english" in text_src:
                violations.append(str(py_file.relative_to(_PROJECT_ROOT)))
            if "from src.packs.subject_english" in text_src:
                violations.append(str(py_file.relative_to(_PROJECT_ROOT)))
        assert not violations, (
            f"核心域泄漏学科包归一化逻辑（违反 A5/X6）：{violations}"
        )
