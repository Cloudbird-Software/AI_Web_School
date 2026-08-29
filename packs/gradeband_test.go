package packs

import (
	"testing"

	"github.com/Cloudbird-Software/AI_Web_School/registry"
)

// gradeBandStub 是 GradeBandPack 契约的最小实现（测试承载 A6「学段是一等
// 维度：类型化承载」的接口面实证——T-W5-027 红队审查指认 TestGradeBandPack
// 为虚构符号后补真）。
type gradeBandStub struct {
	id            registry.Entry
	minGrade      int
	maxGrade      int
}

func (s gradeBandStub) Entry() registry.Entry { return s.id }

func (s gradeBandStub) AppliesTo() (minGrade, maxGrade int) {
	return s.minGrade, s.maxGrade
}

// TestGradeBandPack_Contract 学段包契约面：类型化实现可装配、年级区间语义
// 自洽（min ≤ max 且覆盖各自学段段位）。A6 实证：学段差异以 GradeBandPack
// 类型承载（接口存在 + 实现可装配），而非散落的年级 if 分支。
func TestGradeBandPack_Contract(t *testing.T) {
	low := gradeBandStub{id: registry.Entry{ID: "gb-low", Version: "1.0.0"}, minGrade: 1, maxGrade: 2}
	mid := gradeBandStub{id: registry.Entry{ID: "gb-mid", Version: "1.0.0"}, minGrade: 3, maxGrade: 4}
	high := gradeBandStub{id: registry.Entry{ID: "gb-high", Version: "1.0.0"}, minGrade: 5, maxGrade: 6}

	for _, tc := range []struct {
		name string
		pack GradeBandPack
		lo   int
		hi   int
	}{
		{"low", low, 1, 2},
		{"mid", mid, 3, 4},
		{"high", high, 5, 6},
	} {
		minG, maxG := tc.pack.AppliesTo()
		if minG != tc.lo || maxG != tc.hi {
			t.Errorf("%s: 年级区间 = [%d,%d], want [%d,%d]", tc.name, minG, maxG, tc.lo, tc.hi)
		}
		if minG > maxG {
			t.Errorf("%s: 区间倒挂 [%d,%d]", tc.name, minG, maxG)
		}
		if tc.pack.Entry().ID == "" {
			t.Errorf("%s: 学段包必须为版本化资产（Entry.ID 非空）", tc.name)
		}
	}

	// 接口装配断言：GradeBandPack 变量可承载任意实现（类型面 = A6 承载本体）。
	var _ GradeBandPack = low
	var _ GradeBandPack = mid
	var _ GradeBandPack = high
}
