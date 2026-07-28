"""T-W4-013 语篇难度分析器单元测试.

验收对照：
  #2 analyze_difficulty(text, grade_band) 返回字频分布、平均句长、生词率，
     与预设难度区间比对给出偏差报告。
  #4 make accept TASK=T-W4-013 全绿（本文件即单元测试主体）。
  #5 不 import 学科包/学段包。

测试不依赖分词库：字符级分析零依赖、确定。
"""
from __future__ import annotations

import pytest

from src.core.content.difficulty_analyzer import (
    analyze_difficulty,
    grade_band_sentence_length_ceiling,
)
from src.core.content.passage_schema import DifficultyTarget


# ── 验收 #2：字频/句长/生词率 ────────────────────────────────────────

class TestAnalyzeDifficultyBasics:
    """字频分布、平均句长、生词率基础统计."""

    def test_chinese_text_char_freq_and_sentence_length(self):
        """中文语篇：字频统计 + 句长按句末标点切分."""
        text = "春天来了。花儿开了。鸟儿唱歌。"
        report = analyze_difficulty(text, "L")

        # 3 句（按。切分）
        assert report.metrics.total_sentences == 3
        # 实质字符：春天来了(4) + 花儿开了(4) + 鸟儿唱歌(4) = 12 字（标点不计）
        assert report.metrics.total_chars == 12
        # 平均句长 = 12/3 = 4.0
        assert report.metrics.avg_sentence_length == 4.0
        # 字频：了出现2次、儿出现2次，其余1次
        assert report.metrics.char_freq["儿"] == 2
        assert report.metrics.char_freq["了"] == 2
        assert report.metrics.char_freq["春"] == 1
        assert report.metrics.char_freq["花"] == 1

    def test_punctuation_excluded_from_char_count(self):
        """标点与空白不计入字频/总字数."""
        text = "你好，世界！"
        report = analyze_difficulty(text, "M")
        # 实质字符：你好世界 = 4 字（，！不计）
        assert report.metrics.total_chars == 4
        assert "，" not in report.metrics.char_freq
        assert "！" not in report.metrics.char_freq
        assert report.metrics.char_freq["你"] == 1

    def test_english_text_sentence_split(self):
        """英文语篇：按 .!?; 切句."""
        text = "Hello world. How are you? I am fine."
        report = analyze_difficulty(text, "H")
        assert report.metrics.total_sentences == 3
        # 字频含字母，空白不计
        assert "H" in report.metrics.char_freq
        assert " " not in report.metrics.char_freq

    def test_mixed_delimiters(self):
        """中英混合标点切句：。！？；.!?;\\n."""
        text = "第一句。第二句！第三句？第四句；第五句\n第六句"
        report = analyze_difficulty(text, "M")
        assert report.metrics.total_sentences == 6

    def test_empty_text(self):
        """空文本：零统计量，不报错."""
        report = analyze_difficulty("", "L")
        assert report.metrics.total_chars == 0
        assert report.metrics.total_sentences == 0
        assert report.metrics.avg_sentence_length == 0.0
        assert report.metrics.oov_rate == 0.0
        assert report.metrics.char_freq == {}

    def test_whitespace_only_text(self):
        """纯空白文本：零实质字符."""
        report = analyze_difficulty("   \n\t  ", "L")
        assert report.metrics.total_chars == 0
        assert report.metrics.total_sentences == 0


# ── 生词率（OOV）─────────────────────────────────────────────────────

class TestOOVRate:
    """生词率：相对课标词表的字级 OOV 占比."""

    def test_oov_with_vocab_baseline(self):
        """提供课标字表：表外字计入 OOV."""
        text = "苹果香蕉葡萄"  # 6 字
        # 课标字表只含「苹果」
        baseline = {"苹", "果"}
        report = analyze_difficulty(text, "L", vocab_baseline=baseline)
        # OOV = 香蕉葡萄 = 4 字 / 6 字 ≈ 0.6667
        assert report.oov_baseline_available is True
        assert report.metrics.oov_rate == pytest.approx(4 / 6, abs=1e-4)

    def test_oov_all_in_baseline(self):
        """所有字都在课标字表内：OOV=0."""
        text = "苹果"
        baseline = {"苹", "果"}
        report = analyze_difficulty(text, "L", vocab_baseline=baseline)
        assert report.metrics.oov_rate == 0.0
        assert report.oov_baseline_available is True

    def test_oov_no_baseline_returns_zero_and_flag(self):
        """未提供课标字表：oov_rate=0.0，标记无基线."""
        text = "生僻字龘"
        report = analyze_difficulty(text, "L", vocab_baseline=None)
        assert report.metrics.oov_rate == 0.0
        assert report.oov_baseline_available is False

    def test_oov_empty_text_with_baseline(self):
        """空文本 + 有基线：OOV=0，基线可用."""
        report = analyze_difficulty("", "L", vocab_baseline={"你", "好"})
        assert report.metrics.oov_rate == 0.0
        assert report.oov_baseline_available is True


# ── 偏差报告 ─────────────────────────────────────────────────────────

class TestDeviationReport:
    """与预设难度区间比对，给出偏差报告."""

    def test_deviation_within_range(self):
        """实际 oov_rate 在目标区间内：status=within，delta=0."""
        text = "苹果香蕉"  # 4 字
        baseline = {"苹", "果", "香", "蕉"}  # 全在表内 → oov=0
        target = DifficultyTarget(min=0.0, max=0.3)
        report = analyze_difficulty(
            text, "L", vocab_baseline=baseline, difficulty_target=target
        )
        assert len(report.deviations) == 1
        d = report.deviations[0]
        assert d.metric == "oov_rate"
        assert d.status == "within"
        assert d.delta == 0.0
        assert d.actual == 0.0

    def test_deviation_above_range(self):
        """实际 oov_rate 高于目标上限：status=above."""
        text = "苹果香蕉葡萄西瓜"  # 8 字
        baseline = {"苹", "果"}  # 只有2字在表内 → oov=6/8=0.75
        target = DifficultyTarget(min=0.0, max=0.3)
        report = analyze_difficulty(
            text, "L", vocab_baseline=baseline, difficulty_target=target
        )
        d = report.deviations[0]
        assert d.status == "above"
        assert d.actual == pytest.approx(0.75, abs=1e-4)
        assert d.delta == pytest.approx(0.75 - 0.3, abs=1e-4)

    def test_deviation_below_range(self):
        """实际 oov_rate 低于目标下限：status=below（语篇过易）."""
        text = "苹果"  # 2 字，全在表内 → oov=0
        baseline = {"苹", "果"}
        target = DifficultyTarget(min=0.2, max=0.5)
        report = analyze_difficulty(
            text, "L", vocab_baseline=baseline, difficulty_target=target
        )
        d = report.deviations[0]
        assert d.status == "below"
        assert d.delta == pytest.approx(0.0 - 0.2, abs=1e-4)

    def test_no_deviation_without_baseline(self):
        """无 OOV 基线时不产生偏差报告（oov_rate=0 无意义）."""
        text = "任意文本"
        target = DifficultyTarget(min=0.0, max=0.3)
        report = analyze_difficulty(
            text, "L", vocab_baseline=None, difficulty_target=target
        )
        assert report.deviations == []

    def test_no_deviation_without_target(self):
        """无目标区间时不产生偏差报告."""
        text = "苹果"
        baseline = {"苹", "果"}
        report = analyze_difficulty(
            text, "L", vocab_baseline=baseline, difficulty_target=None
        )
        assert report.deviations == []


# ── 学段句长上限 ─────────────────────────────────────────────────────

class TestGradeBandSentenceCeiling:
    """学段句长适龄参考上限."""

    def test_low_band_short_ceiling(self):
        """低段句长上限最短（短句为主）."""
        assert grade_band_sentence_length_ceiling("L") == 15.0

    def test_high_band_long_ceiling(self):
        """高段句长上限最长（可读长句）."""
        assert grade_band_sentence_length_ceiling("H") == 40.0

    def test_unknown_band_defaults(self):
        """未知学段给中段默认值."""
        assert grade_band_sentence_length_ceiling("X") == 25.0


# ── DifficultyMetrics 落 ORM 兼容 ────────────────────────────────────

def test_metrics_serializable_to_jsonb():
    """DifficultyMetrics 可序列化为 dict（落 passage.difficulty_metrics JSONB）."""
    text = "春天来了。花儿开了。"
    report = analyze_difficulty(text, "L")
    metrics_dict = report.metrics.model_dump()
    # 关键字段齐全
    assert "avg_sentence_length" in metrics_dict
    assert "oov_rate" in metrics_dict
    assert "total_chars" in metrics_dict
    assert "total_sentences" in metrics_dict
    assert "char_freq" in metrics_dict
    # char_freq 是 dict（JSONB 友好）
    assert isinstance(metrics_dict["char_freq"], dict)
