package ai

import (
	"reflect"
	"strings"
	"testing"
)

// 与冻结实现 pii_filter_test 语义对齐的剥离用例：确定性、可审计、启发式.

func TestRedactStripsAllKindsInOrder(t *testing.T) {
	// 组合片段之间用标点分隔。注意：汉字长连排会被地址规则整段回溯吞掉
	// （[\p{Han}]{2,}+关键字 的贪婪语义，冻结实现同构的已知启发式边界），
	// 姓名类用例单独在 TestRedactNamesNumberedByOccurrence 覆盖。
	in := "家住北京市海淀区中关村路，电话13812345678，邮箱sili@example.com，身份证11010119900307889X"
	got, kinds, err := RegexRedactor{}.Redact(in)
	if err != nil {
		t.Fatalf("Redact 意外失败: %v", err)
	}
	for _, bad := range []string{"13812345678", "11010119900307889X", "sili@example.com", "北京市海淀区"} {
		if strings.Contains(got, bad) {
			t.Fatalf("剥离后仍含敏感片段 %q: %s", bad, got)
		}
	}
	wantKinds := []string{PIIIDCard, PIIPhone, PIIEmail, PIIAddress}
	if !reflect.DeepEqual(kinds, wantKinds) {
		t.Fatalf("kinds = %v, want %v（长格式先剥防子串误切）", kinds, wantKinds)
	}
}

func TestRedactNamesNumberedByOccurrence(t *testing.T) {
	got, kinds, err := RegexRedactor{}.Redact("老师点名表扬了同学李四和家长王五")
	if err != nil {
		t.Fatal(err)
	}
	want := "老师点名表扬了学生A和学生B"
	if got != want {
		t.Fatalf("got %q, want %q", got, want)
	}
	if !reflect.DeepEqual(kinds, []string{PIIName}) {
		t.Fatalf("kinds = %v", kinds)
	}
}

func TestRedactKeepsAliasAndCleanText(t *testing.T) {
	alias := "01J9Z0T4Q6MCW3K9S7XN2P8R5A" // ULID 形态 student_alias_id，D7 允许保留
	got, kinds, err := RegexRedactor{}.Redact("alias=" + alias + " 无个人信息")
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(got, alias) {
		t.Fatalf("student_alias_id 被误剥: %s", got)
	}
	if len(kinds) != 0 {
		t.Fatalf("干净文本不应有剥离记录: %v", kinds)
	}

	clean, kinds2, _ := RegexRedactor{}.Redact("")
	if clean != "" || kinds2 != nil {
		t.Fatalf("空输入应原样通过: %q %v", clean, kinds2)
	}
}

func TestRedactPhoneNotSubsumedByIdCard(t *testing.T) {
	// 纯手机号文本只触发 phone；身份证先剥的次序保证 18 位串不被切成 phone 子串
	got, kinds, _ := RegexRedactor{}.Redact("联系 13912345678")
	if !reflect.DeepEqual(kinds, []string{PIIPhone}) || strings.Contains(got, "13912345678") {
		t.Fatalf("phone 未剥离: %q %v", got, kinds)
	}
}
