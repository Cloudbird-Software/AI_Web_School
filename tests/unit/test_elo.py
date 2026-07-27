"""W3 S8：掌握度 Elo v1 测试（在线轻量增量更新纯函数）.

覆盖：
  §1 expected_score：等评级 0.5；评级差 +100 → ≈0.640（10^(-0.25) 手算值）。
  §2 elo_update：答对学生升/题目降（对称 delta）；答错反向；k 缩放。
  §3 参数校验：score 越界 / k<=0 → ValueError。
  §4 difficulty_to_rating：p=0.5→BASE；p<0.5→高于 BASE（越难越高）；
     与 expected_score 往返一致；p=0/1 截断不炸。
"""
from __future__ import annotations

import pytest

from src.core.data.elo import (
    BASE_RATING,
    DEFAULT_K,
    difficulty_to_rating,
    elo_update,
    expected_score,
)


class TestExpectedScore:
    """Elo 期望得分."""

    def test_equal_ratings_give_half(self) -> None:
        assert expected_score(1500.0, 1500.0) == pytest.approx(0.5)

    def test_plus_100_rating_gap(self) -> None:
        """学生高 100：E = 1/(1+10^(-100/400)) ≈ 0.64006."""
        assert expected_score(1600.0, 1500.0) == pytest.approx(0.640064, rel=1e-5)

    def test_symmetry(self) -> None:
        """E(Rs,Ri) + E(Ri,Rs) = 1."""
        e = expected_score(1700.0, 1400.0)
        assert e + expected_score(1400.0, 1700.0) == pytest.approx(1.0)


class TestEloUpdate:
    """增量更新方向与幅度."""

    def test_correct_raises_student_lowers_item(self) -> None:
        """答对：学生升、题目降，delta 对称."""
        rs0, ri0 = 1500.0, 1500.0
        rs1, ri1 = elo_update(rs0, ri0, 1.0)
        e = expected_score(rs0, ri0)
        assert rs1 == pytest.approx(rs0 + DEFAULT_K * (1 - e))
        assert ri1 == pytest.approx(ri0 - DEFAULT_K * (1 - e))
        assert rs1 > rs0 and ri1 < ri0

    def test_wrong_lowers_student_raises_item(self) -> None:
        """答错：学生降、题目升."""
        rs1, ri1 = elo_update(1500.0, 1500.0, 0.0)
        assert rs1 < 1500.0 and ri1 > 1500.0

    def test_expected_upset_larger_delta(self) -> None:
        """爆冷（弱学生答对难题）比预期内答对幅度大."""
        rs_upset, _ = elo_update(1200.0, 1800.0, 1.0)
        rs_expected, _ = elo_update(1800.0, 1200.0, 1.0)
        assert (rs_upset - 1200.0) > (rs_expected - 1800.0)

    def test_k_scales_delta(self) -> None:
        """k 减半则 delta 减半."""
        rs_full, _ = elo_update(1500.0, 1500.0, 1.0, k=32.0)
        rs_half, _ = elo_update(1500.0, 1500.0, 1.0, k=16.0)
        assert (rs_half - 1500.0) == pytest.approx((rs_full - 1500.0) / 2)

    def test_score_out_of_range_rejected(self) -> None:
        with pytest.raises(ValueError, match="score"):
            elo_update(1500.0, 1500.0, 1.5)
        with pytest.raises(ValueError, match="score"):
            elo_update(1500.0, 1500.0, -0.1)

    def test_non_positive_k_rejected(self) -> None:
        with pytest.raises(ValueError, match="k"):
            elo_update(1500.0, 1500.0, 1.0, k=0.0)


class TestDifficultyToRating:
    """CTT 正确率 → Elo 题目难度评级."""

    def test_p_half_gives_base(self) -> None:
        assert difficulty_to_rating(0.5) == pytest.approx(BASE_RATING)

    def test_harder_item_higher_rating(self) -> None:
        """p=0.25：R_i = 1500 + 400·log10(3) ≈ 1690.85."""
        assert difficulty_to_rating(0.25) == pytest.approx(1690.848, rel=1e-4)

    def test_roundtrip_with_expected_score(self) -> None:
        """平均学生（R_s=BASE）对 R_i=difficulty_to_rating(p) 的期望得分 ≈ p."""
        for p in (0.1, 0.3, 0.5, 0.75, 0.9):
            r_i = difficulty_to_rating(p)
            assert expected_score(BASE_RATING, r_i) == pytest.approx(p, rel=1e-9)

    def test_extreme_p_clipped_no_crash(self) -> None:
        """p=0/1 截断到开区间，不抛 math domain error."""
        assert difficulty_to_rating(1.0) < BASE_RATING
        assert difficulty_to_rating(0.0) > BASE_RATING
