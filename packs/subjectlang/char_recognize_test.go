package subjectlang

// char_recognize 的组合枚举回归测试（#150 审计发现的历史缺陷）。

import "testing"

// TestKthComb3CoversFullRange 回归（#150 审计发现）：历史实现的块大小误用
// C(n-1-a,3)，k ≥ n-2 即枚举耗尽——修复后全值域必须产出合法互异三元组合，
// 且全字典序无重复（unrank 双射性）。
func TestKthComb3CoversFullRange(t *testing.T) {
	items := []string{"a", "b", "c", "d", "e", "f"}
	total := comb3(len(items))
	seen := map[[3]string]bool{}
	for k := 0; k < total; k++ {
		x, y, z, err := kthComb3(items, k)
		if err != nil {
			t.Fatalf("k=%d 不得报错（历史缺陷在 k≥%d 触发）: %v", k, len(items)-2, err)
		}
		if x == y || y == z || x == z {
			t.Fatalf("k=%d 组合含重复元素: %s %s %s", k, x, y, z)
		}
		key := [3]string{x, y, z}
		if seen[key] {
			t.Fatalf("k=%d 组合重复（双射破坏）: %v", k, key)
		}
		seen[key] = true
	}
	if len(seen) != total {
		t.Fatalf("覆盖数 = %d, want C(6,3)=%d", len(seen), total)
	}
}
