package datastat

import (
	"errors"
	"math"
	"testing"
)

// 期望得分（冻结实现 elo.expected_score 公式：E = 1/(1+10^(-(Rs-Ri)/400))）.
func TestExpectedScore(t *testing.T) {
	// 两评级相等 → 0.5（精确）
	if got := ExpectedScore(1500, 1500); got != 0.5 {
		t.Errorf("E(1500,1500) = %v，期望精确 0.5", got)
	}
	// E(1600,1500) = 1/(1+10^-0.25) = 0.6400649998028851（Python 交叉验证）
	assertApproxF(t, "E(1600,1500)", ExpectedScore(1600, 1500), 0.6400649998028851, 1e-15)
	// 对称性：E(Rs,Ri) + E(Ri,Rs) = 1
	assertApproxF(t, "对称性", ExpectedScore(1400, 1500)+ExpectedScore(1500, 1400), 1.0, 1e-12)
}

// 增量更新（冻结实现 elo.elo_update：R_s' = R_s + K(S-E)，R_i' = R_i - K(S-E)）.
func TestEloUpdate_HandExamples(t *testing.T) {
	// 手算：E(1500,1500)=0.5，delta = 32·(1-0.5) = 16 → 学生 1516，题目 1484
	s, i, err := EloUpdate(1500, 1500, 1, 32)
	if err != nil {
		t.Fatalf("EloUpdate 失败：%v", err)
	}
	if s != 1516 || i != 1484 {
		t.Errorf("更新 = (%v, %v)，期望精确 (1516, 1484)", s, i)
	}
	// S=0（答错）：delta = 32·(0-0.5) = -16 → 学生 1484，题目 1516（方向相反）
	s, i, err = EloUpdate(1500, 1500, 0, 32)
	if err != nil {
		t.Fatalf("EloUpdate 失败：%v", err)
	}
	if s != 1484 || i != 1516 {
		t.Errorf("更新 = (%v, %v)，期望精确 (1484, 1516)", s, i)
	}
	// E(1600,1500)=0.6400649998：delta = 32·(1-E) = 11.5179200063（Python 交叉验证）
	s, i, err = EloUpdate(1600, 1500, 1, 32)
	if err != nil {
		t.Fatalf("EloUpdate 失败：%v", err)
	}
	assertApproxF(t, "学生新评级", s, 1611.5179200063076, 1e-9)
	assertApproxF(t, "题目新评级", i, 1488.4820799936924, 1e-9)
	// 评级守恒：R_s + R_i 更新前后不变（零和）
	assertApproxF(t, "评级守恒", s+i, 1600+1500, 1e-9)
	// 部分分 S=0.5、E=0.5 → delta=0（不动）
	s, i, err = EloUpdate(1500, 1500, 0.5, 32)
	if err != nil {
		t.Fatalf("EloUpdate 失败：%v", err)
	}
	if s != 1500 || i != 1500 {
		t.Errorf("S=E 时不应漂移，得到 (%v, %v)", s, i)
	}
}

func TestEloUpdate_Errors(t *testing.T) {
	for _, score := range []float64{-0.1, 1.1, math.NaN(), math.Inf(1)} {
		if _, _, err := EloUpdate(1500, 1500, score, 32); !errors.Is(err, ErrScoreOutOfRange) {
			t.Errorf("score=%v 应报 ErrScoreOutOfRange，得到 %v", score, err)
		}
	}
	for _, k := range []float64{0, -1} {
		if _, _, err := EloUpdate(1500, 1500, 1, k); !errors.Is(err, ErrNonPositiveK) {
			t.Errorf("k=%v 应报 ErrNonPositiveK，得到 %v", k, err)
		}
	}
}

// 难度换算（冻结实现 elo.difficulty_to_rating：R_i = base + scale·log10((1-p)/p)）.
func TestDifficultyToRating(t *testing.T) {
	// p=0.5 → base（精确）
	if got := DifficultyToRating(0.5); got != 1500 {
		t.Errorf("rating(0.5) = %v，期望精确 1500", got)
	}
	// p=0.1 → 1500 + 400·log10(9) = 1500 + 381.6970037757…（log10(9) 手算）
	assertApproxF(t, "rating(0.1)", DifficultyToRating(0.1), 1881.69700377573, 1e-9)
	// p=0.9 → 1500 - 381.697…（对称：r(p)+r(1-p) = 2·base）
	assertApproxF(t, "rating(0.9)", DifficultyToRating(0.9), 1118.30299622427, 1e-9)
	assertApproxF(t, "对称性", DifficultyToRating(0.1)+DifficultyToRating(0.9), 3000, 1e-9)
	// p 越小题越难（评级越高）；p→0 越难
	assertApproxF(t, "rating(0→截断)", DifficultyToRating(0), 3899.9998262821205, 1e-6)
	assertApproxF(t, "rating(1→截断)", DifficultyToRating(1), -899.9998262821205, 1e-6)
	// 单调递减：p 越大越易（评级越低）
	prev := math.Inf(1)
	for p := 0.05; p <= 0.951; p += 0.05 {
		r := DifficultyToRating(p)
		if r >= prev {
			t.Fatalf("rating(%v)=%v 不小于前一档 %v：违反单调递减", p, r, prev)
		}
		prev = r
	}
}

func TestEloConstants(t *testing.T) {
	if BaseRating != 1500 || Scale != 400 || DefaultK != 32 {
		t.Errorf("Elo 常量漂移：base=%v scale=%v k=%v", BaseRating, Scale, DefaultK)
	}
}
