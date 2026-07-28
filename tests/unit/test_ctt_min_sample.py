"""T-W4-047 CTT min_sample 门槛(30) + 小样本区分度警示测试.

覆盖任务卡验收 §1-§5：
  §1 compute_discrimination(responses, key) 在 n<30 时返回 None + 警告日志/标记。
  §2 n≥30 时计算行为与既有 compute_ctt 完全一致（golden 回归）。
  §3 所有既有 CTT 测试继续通过（不退化）——由 make accept 全量套件保证。
  §4 make accept TASK=T-W4-047 全绿（本文件即单元测试主体）。
  §5 不 import 学科包/学段包（A5/X6 静态扫描）。

BRIEF S11（W3 遗留修复）：CTT 区分度增加 min_sample 门槛（默认 30），
n<30 返回 None 并标记警示；不修改历史计算逻辑，仅增强边界判断，向后兼容。
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

import pytest

from src.core.data.ctt import (
    CTT_MIN_SAMPLE_DEFAULT,
    ResponseRecord,
    compute_ctt,
    compute_discrimination,
)


# ────────────────────────────────────────────────────────────────────
# 数据集构造
# ────────────────────────────────────────────────────────────────────


def _dataset_two_items(n_a: int = 30, n_b: int = 10) -> list[ResponseRecord]:
    """两题数据集：item A 有 n_a 条，item B 有 n_b 条；每个学生两题各一条.

    用确定性的 correct 序列（基于 student 索引取模）保证可复现。
    学生 i：A.correct = i % 2，B.correct = (i+1) % 2 —— 两题有协变结构，
    使区分度可计算且非平凡。
    """
    records: list[ResponseRecord] = []
    for i in range(max(n_a, n_b)):
        sid = f"s{i}"
        if i < n_a:
            records.append(
                ResponseRecord(item_version_id="A", student_alias_id=sid, correct=float(i % 2))
            )
        if i < n_b:
            records.append(
                ResponseRecord(item_version_id="B", student_alias_id=sid, correct=float((i + 1) % 2))
            )
    return records


# ────────────────────────────────────────────────────────────────────
# §1 n<30 返回 None + 警告
# ────────────────────────────────────────────────────────────────────


class TestSmallSampleWarning:
    """n<30 时返回 None 并记 warning（小样本警示）。"""

    @pytest.mark.parametrize("n", [0, 1, 5, 15, 29])
    def test_below_threshold_returns_none(self, n: int) -> None:
        """n<30（含 0/1/29）→ 返回 None."""
        records = [
            ResponseRecord(item_version_id="A", student_alias_id=f"s{i}", correct=float(i % 2))
            for i in range(n)
        ]
        assert compute_discrimination(records, "A") is None

    def test_below_threshold_logs_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        """n<30 时记 warning，消息含 item/n/min_sample 标记."""
        records = [
            ResponseRecord(item_version_id="A", student_alias_id=f"s{i}", correct=1.0)
            for i in range(10)
        ]
        with caplog.at_level(logging.WARNING, logger="src.core.data.ctt"):
            result = compute_discrimination(records, "A")
        assert result is None
        # 警示日志存在且携带小样本标记
        msgs = [r.message for r in caplog.records]
        assert any("ctt.discrimination.min_sample" in m for m in msgs)
        assert any("item=A" in m and "n=10" in m and "min_sample=30" in m for m in msgs)

    def test_default_threshold_is_30(self) -> None:
        """默认门槛 = 30（CTT_MIN_SAMPLE_DEFAULT）."""
        assert CTT_MIN_SAMPLE_DEFAULT == 30

    def test_custom_min_sample(self) -> None:
        """min_sample 可配置；n=10 < 自定义门槛 20 → None."""
        records = [
            ResponseRecord(item_version_id="A", student_alias_id=f"s{i}", correct=float(i % 2))
            for i in range(10)
        ]
        assert compute_discrimination(records, "A", min_sample=20) is None

    def test_no_warning_above_threshold(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """n≥30 时不记小样本 warning."""
        records = _dataset_two_items(n_a=30, n_b=10)
        with caplog.at_level(logging.WARNING, logger="src.core.data.ctt"):
            compute_discrimination(records, "A")
        msgs = [r.message for r in caplog.records]
        assert not any("ctt.discrimination.min_sample" in m for m in msgs)


# ────────────────────────────────────────────────────────────────────
# §2 n≥30 与既有 compute_ctt 完全一致（golden 回归）
# ────────────────────────────────────────────────────────────────────


class TestGoldenRegression:
    """n≥30 时 compute_discrimination == compute_ctt 的区分度（既有逻辑不变）."""

    def test_matches_compute_ctt_at_n30(self) -> None:
        """n=30（边界）：与 compute_ctt 结果完全一致."""
        records = _dataset_two_items(n_a=30, n_b=10)
        ctt_stats = {s.item_version_id: s for s in compute_ctt(records)}
        assert compute_discrimination(records, "A") == ctt_stats["A"].discrimination

    def test_matches_compute_ctt_large_n(self) -> None:
        """n=200：与 compute_ctt 结果完全一致（数值回归）."""
        records = _dataset_two_items(n_a=200, n_b=50)
        ctt_stats = {s.item_version_id: s for s in compute_ctt(records)}
        got = compute_discrimination(records, "A")
        assert got == ctt_stats["A"].discrimination
        assert got is not None

    def test_matches_compute_ctt_with_float_partial_credit(self) -> None:
        """部分分给分（correct ∈ [0,1]）下也与 compute_ctt 一致."""
        records = [
            ResponseRecord(item_version_id="A", student_alias_id=f"s{i}", correct=(i % 5) / 4.0)
            for i in range(40)
        ]
        # 加一个第二题让 student_total 有区分度
        records += [
            ResponseRecord(item_version_id="B", student_alias_id=f"s{i}", correct=float(i % 2))
            for i in range(40)
        ]
        ctt_stats = {s.item_version_id: s for s in compute_ctt(records)}
        assert compute_discrimination(records, "A") == ctt_stats["A"].discrimination

    def test_zero_variance_returns_none_consistent_with_ctt(self) -> None:
        """n≥30 但全对（零方差）：与 compute_ctt 一致均返回 None."""
        records = [
            ResponseRecord(item_version_id="A", student_alias_id=f"s{i}", correct=1.0)
            for i in range(40)
        ]
        ctt_stats = {s.item_version_id: s for s in compute_ctt(records)}
        # compute_ctt 因零方差返回 None；compute_discrimination 同样
        assert ctt_stats["A"].discrimination is None
        assert compute_discrimination(records, "A") is None


# ────────────────────────────────────────────────────────────────────
# §3 既有 compute_ctt 不退化（向后兼容）
# ────────────────────────────────────────────────────────────────────


class TestBackwardCompat:
    """compute_ctt 既有行为不变（min_sample 增强是新增函数，不改批处理）."""

    def test_compute_ctt_still_computes_small_n(self) -> None:
        """compute_ctt 对 n=4 仍产出区分度（不受新门槛影响）."""
        # 复用 test_ctt.py 的手算数据集形状：2 题 × 4 学生
        records: list[ResponseRecord] = []
        answers = {
            "A": {"s1": 1.0, "s2": 1.0, "s3": 0.0, "s4": 0.0},
            "B": {"s1": 1.0, "s2": 0.0, "s3": 0.0, "s4": 0.0},
        }
        for item, per in answers.items():
            for student, correct in per.items():
                records.append(
                    ResponseRecord(item_version_id=item, student_alias_id=student, correct=correct)
                )
        stats = {s.item_version_id: s for s in compute_ctt(records)}
        # compute_ctt 仍为 n=4 的题产出区分度（既有行为）
        assert stats["A"].discrimination is not None
        assert stats["A"].sample_size == 4
        # 而新函数对同一 n=4 应用门槛返回 None（增强边界判断）
        assert compute_discrimination(records, "A") is None

    def test_compute_ctt_signature_unchanged(self) -> None:
        """compute_ctt 仍只接受 records 单参数（无 min_sample）."""
        records = _dataset_two_items(n_a=30, n_b=5)
        # 既有签名：compute_ctt(records)
        stats = compute_ctt(records)
        assert {s.item_version_id for s in stats} == {"A", "B"}


# ────────────────────────────────────────────────────────────────────
# §5 不 import 学科包/学段包（A5/X6 静态扫描）
# ────────────────────────────────────────────────────────────────────


def test_no_subject_pack_imports_in_data() -> None:
    """src/core/data/ 不 import 任何学科包/学段包（宪法 A5/A7）."""
    data_dir = (
        Path(__file__).resolve().parent.parent.parent / "src" / "core" / "data"
    )
    assert data_dir.is_dir(), f"目录不存在：{data_dir}"
    pattern = re.compile(
        r"^\s*(?:from\s+(?:packs|subject_)|import\s+(?:packs|subject_))",
        re.MULTILINE,
    )
    violations: list[str] = []
    for py_file in sorted(data_dir.rglob("*.py")):
        if pattern.findall(py_file.read_text(encoding="utf-8")):
            violations.append(str(py_file.relative_to(data_dir)))
    assert not violations, (
        f"src/core/data/ 存在学科包/学段包 import（违反 A5/A7）：{violations}"
    )
