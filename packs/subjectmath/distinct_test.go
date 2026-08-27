package subjectmath

import (
	"encoding/json"
	"fmt"
	"strings"
	"testing"
)

// distinct_test.go：结构互异摘要的规范性——键序无关、层级保序、
// 类型封闭（float 拒绝）、碰撞必被抓。

func TestCanonicalKeyOrderInsensitive(t *testing.T) {
	a := map[string]any{"b": 1, "a": map[string]any{"y": "1", "x": []any{int64(2), "k"}}}
	b := map[string]any{"a": map[string]any{"x": []any{int64(2), "k"}, "y": "1"}, "b": int64(1)}
	da, err1 := DigestAny(a)
	db, err2 := DigestAny(b)
	if err1 != nil || err2 != nil {
		t.Fatalf("digest 失败: %v %v", err1, err2)
	}
	if da != db {
		t.Fatalf("键插入顺序影响摘要：%s vs %s", da, db)
	}
}

func TestDigestDistinguishesValues(t *testing.T) {
	cases := []map[string]any{
		{"blocks": []any{map[string]any{"rendered": "12 × 3 = ？"}}},
		{"blocks": []any{map[string]any{"rendered": "13 × 3 = ？"}}},
		{"blocks": []any{
			map[string]any{"rendered": "A"},
			map[string]any{"rendered": "B"},
		}},
		{"blocks": []any{
			map[string]any{"rendered": "B"}, // 数组保序：换序即不同内容
			map[string]any{"rendered": "A"},
		}},
	}
	seen := map[string]string{}
	for i, c := range cases {
		d, err := DigestAny(c)
		if err != nil {
			t.Fatalf("case %d: %v", i, err)
		}
		if !strings.HasPrefix(d, "sha256:") {
			t.Fatalf("摘要应带 sha256: 前缀：%s", d)
		}
		if prev, dup := seen[d]; dup {
			t.Fatalf("case %d 与 case %s 摘要碰撞", i, prev)
		}
		seen[d] = fmt.Sprint(i)
	}
}

func TestCanonicalRejectsFloat(t *testing.T) {
	if _, err := DigestAny(map[string]any{"x": 1.5}); err == nil {
		t.Fatal("float64 必须被拒绝（摘要确定性红线）")
	}
	if _, err := DigestAny(map[string]any{"x": json.Number("3.14")}); err == nil {
		t.Fatal("非整数 json.Number 必须被拒绝")
	}
	// 整数形态 json.Number 允许，且与同值 int64 等价（deepCopy 往返一致性）
	viaNum, err := DigestAny(map[string]any{"x": json.Number("42")})
	if err != nil {
		t.Fatalf("整数 json.Number 应放行: %v", err)
	}
	viaInt, _ := DigestAny(map[string]any{"x": int64(42)})
	if viaNum != viaInt {
		t.Fatalf("json.Number(42) 与 int64(42) 摘要不一致: %s vs %s", viaNum, viaInt)
	}
}

func TestAssertPairwiseDistinct(t *testing.T) {
	ok := []string{"sha256:a", "sha256:b", "sha256:c"}
	if err := AssertPairwiseDistinct(ok); err != nil {
		t.Fatalf("互异序列不应报错: %v", err)
	}
	err := AssertPairwiseDistinct([]string{"sha256:a", "sha256:b", "sha256:a"})
	if err == nil || !strings.Contains(err.Error(), "H-W6-1") {
		t.Fatalf("碰撞必须显式失败并指向 H-W6-1 口径，得: %v", err)
	}
}

// decString/parseDecString 数值格式回演。
func TestDecimalRoundTrip(t *testing.T) {
	cases := []struct {
		m    int64
		s    int
		want string
	}{
		{198, 1, "19.8"},
		{5, 1, "0.5"},
		{50, 1, "5"},
		{1980, 5 - 5 + 3, "1.98"}, // s=3 尾零截断
		{999, 0, "999"},
	}
	for _, c := range cases {
		scale := c.s
		got := decString(c.m, scale)
		if got != c.want {
			t.Errorf("decString(%d,%d)=%q want %q", c.m, scale, got, c.want)
			continue
		}
		// 回演：解析后再规范化必须逐字节一致（canonical 幂等）。
		m2, s2, err := parseDecString(got)
		if err != nil {
			t.Errorf("parseDecString(%q): %v", got, err)
			continue
		}
		if again := decString(m2, s2); again != got && strings.TrimPrefix(again, "-") != strings.TrimPrefix(got, "-") {
			t.Errorf("回演不幂等：%q → (%d,%d) → %q", got, m2, s2, again)
		}
	}
	if got := decString(-15, 1); got != "-1.5" {
		t.Errorf("decString(-15,1)=%q want -1.5（生成器不产出，仅格式完备性）", got)
	}
	if _, _, err := parseDecString("-3.5"); err == nil {
		t.Error("验证器入口只接受无符号串，负号必须拒")
	}
	if _, _, err := parseDecString("01.2"); err == nil {
		t.Error("前导零非规范串必须拒")
	}
}

func TestTemplateVersionIDStable(t *testing.T) {
	for _, id := range IDs() {
		g, _ := Get(id)
		specBytes := mustJSON(t, g.Spec())
		id1 := mustTemplateVersionID(g.Spec())
		id2 := mustTemplateVersionID(g.Spec())
		if id1 != id2 {
			t.Fatalf("%s 版本号不稳定: %s vs %s", id, id1, id2)
		}
		if !strings.HasPrefix(id1, "sha256:") || len(specBytes) == 0 {
			t.Fatalf("%s 版本号形态异常: %s", id, id1)
		}
	}
}

func mustJSON(t *testing.T, v any) []byte {
	t.Helper()
	b, err := json.Marshal(v)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	return b
}
