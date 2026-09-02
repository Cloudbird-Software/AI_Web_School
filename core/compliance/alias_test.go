package compliance

import (
	"errors"
	"testing"
)

// TestIsSimAlias 前缀判定：sim_ 前缀 → true；其他 → false.
func TestIsSimAlias(t *testing.T) {
	cases := []struct {
		alias string
		want  bool
	}{
		{"sim_batch01_001", true},
		{"sim_poc20260831_00001", true},
		{"sim_", true},
		{"real-student-uuid", false},
		{"SIM_batch01", false}, // 大小写敏感（前缀小写）
		{"", false},
		{"sim", false}, // 无下划线
	}
	for _, tc := range cases {
		if got := IsSimAlias(tc.alias); got != tc.want {
			t.Errorf("IsSimAlias(%q) = %v, want %v", tc.alias, got, tc.want)
		}
	}
}

// TestValidateSimAlias 合成学生 alias 必须 sim_ 前缀.
func TestValidateSimAlias(t *testing.T) {
	if err := ValidateSimAlias("sim_batch01_001"); err != nil {
		t.Fatalf("合法合成 alias 应通过: %v", err)
	}
	err := ValidateSimAlias("real-student-uuid")
	if !errors.Is(err, ErrInvalidAliasNamespace) {
		t.Fatalf("合成 alias 缺前缀应 ErrInvalidAliasNamespace, got %v", err)
	}
	err = ValidateSimAlias("")
	if !errors.Is(err, ErrEmptyAlias) {
		t.Fatalf("空合成 alias 应 ErrEmptyAlias, got %v", err)
	}
}

// TestValidateRealAlias 真实学生 alias 禁止 sim_ 前缀.
func TestValidateRealAlias(t *testing.T) {
	if err := ValidateRealAlias("a1b2c3d4-e5f6-7890-abcd-ef1234567890"); err != nil {
		t.Fatalf("合法真实 alias 应通过: %v", err)
	}
	err := ValidateRealAlias("sim_batch01_001")
	if !errors.Is(err, ErrInvalidAliasNamespace) {
		t.Fatalf("真实 alias 用 sim_ 前缀应 ErrInvalidAliasNamespace, got %v", err)
	}
	err = ValidateRealAlias("")
	if !errors.Is(err, ErrEmptyAlias) {
		t.Fatalf("空真实 alias 应 ErrEmptyAlias, got %v", err)
	}
}

// TestClassify 按前缀分路：sim_ → ClassSim，其余非空 → ClassReal，空 → "".
func TestClassify(t *testing.T) {
	cases := []struct {
		alias string
		want  AliasClass
	}{
		{"sim_batch01_001", ClassSim},
		{"a1b2c3d4-e5f6-7890-abcd-ef1234567890", ClassReal},
		{"", ""},
	}
	for _, tc := range cases {
		if got := Classify(tc.alias); got != tc.want {
			t.Errorf("Classify(%q) = %q, want %q", tc.alias, got, tc.want)
		}
	}
}

// TestSimAliasPrefixConst 前缀常量与 DESIGN_NOTES.md §3.1 一致（sim_）.
func TestSimAliasPrefixConst(t *testing.T) {
	if SimAliasPrefix != "sim_" {
		t.Fatalf("SimAliasPrefix = %q, want sim_", SimAliasPrefix)
	}
}
